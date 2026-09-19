import asyncio
import base64
import copy
import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager, suppress
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import httpx
import yaml
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

logger = logging.getLogger("uvicorn.error")


@dataclass(frozen=True)
class Model:
    id: str
    kind: str
    enabled: bool
    container: str
    base_url: str
    readiness_path: str
    sleep_supported: bool = False
    # 对外稳定 ID 与后端实际模型名解耦（例如 Ollama tag）。
    upstream_model: str | None = None
    # stop | vllm_sleep | comfy_free | ollama_unload；省略时按 kind 推导。
    release: str = "stop"
    # Comfy 工作流：按模型绑定不同 API template（缺省走全局 env / Qwen）
    t2i_workflow: str | None = None
    i2i_workflow: str | None = None
    video_workflow: str | None = None
    i2v_video_workflow: str | None = None
    # 空闲释放阈值（秒）：None=用全局 IDLE_TIMEOUT_SECONDS；0=永不自动空闲释放
    idle_timeout_seconds: float | None = None
    capabilities: tuple[str, ...] = ()


def load_models(path: str) -> dict[str, Model]:
    raw = yaml.safe_load(Path(path).read_text()) or {}
    models: dict[str, Model] = {}
    for model_id, value in raw.get("models", {}).items():
        idle_raw = value.get("idle_timeout_seconds")
        kind = value["kind"]
        default_release = (
            "ollama_unload"
            if kind == "ollama"
            else "vllm_sleep"
            if kind == "vllm" and value.get("sleep_supported")
            else "comfy_free"
            if kind == "comfyui"
            else "stop"
        )
        models[model_id] = Model(
            id=model_id,
            kind=kind,
            enabled=bool(value.get("enabled", False)),
            container=value["container"],
            base_url=value["base_url"].rstrip("/"),
            readiness_path=value.get("readiness_path", "/health"),
            sleep_supported=bool(value.get("sleep_supported", False)),
            upstream_model=value.get("upstream_model"),
            release=str(value.get("release") or default_release),
            t2i_workflow=value.get("t2i_workflow"),
            i2i_workflow=value.get("i2i_workflow"),
            video_workflow=value.get("video_workflow"),
            i2v_video_workflow=value.get("i2v_video_workflow"),
            idle_timeout_seconds=(
                None if idle_raw is None else max(0.0, float(idle_raw))
            ),
            capabilities=tuple(value.get("capabilities", [])),
        )
    return models


class Docker:
    def __init__(self) -> None:
        self.client = httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(uds="/var/run/docker.sock"),
            base_url="http://docker",
            timeout=30,
        )

    async def close(self) -> None:
        await self.client.aclose()

    async def container(self, name: str) -> dict[str, Any] | None:
        response = await self.client.get(f"/containers/{name}/json")
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()

    async def start(self, name: str) -> None:
        response = await self.client.post(f"/containers/{name}/start")
        if response.status_code not in (204, 304):
            response.raise_for_status()

    async def stop(self, name: str) -> None:
        response = await self.client.post(f"/containers/{name}/stop?t=30")
        if response.status_code not in (204, 304, 404):
            response.raise_for_status()


class Scheduler:
    def __init__(self, models: dict[str, Model], docker: Docker) -> None:
        self.models = models
        self.docker = docker
        self.lock = asyncio.Lock()
        self.active: str | None = None
        self.inflight = 0
        self.last_activity = time.monotonic()
        self.idle_timeout = max(
            0.0, float(os.environ.get("IDLE_TIMEOUT_SECONDS", "600"))
        )
        default_check = (
            min(30.0, max(1.0, self.idle_timeout / 4)) if self.idle_timeout else 30.0
        )
        self.idle_check_interval = max(
            1.0,
            float(os.environ.get("IDLE_CHECK_INTERVAL_SECONDS", str(default_check))),
        )
        self.switch_wait_timeout = max(
            1.0, float(os.environ.get("SWITCH_WAIT_TIMEOUT_SECONDS", "900"))
        )
        self.reaper_task: asyncio.Task[None] | None = None
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(600, connect=10))

    async def start(self) -> None:
        # Recover the active marker after a Dispatcher-only restart so an
        # already-loaded backend is still covered by the idle policy.
        for model in self.models.values():
            if not model.enabled:
                continue
            info = await self.docker.container(model.container)
            if info and info["State"]["Running"] and await self.ready(model):
                self.active = model.id
                self.last_activity = time.monotonic()
                break
        self.reaper_task = asyncio.create_task(self.idle_reaper(), name="idle-reaper")

    async def close(self) -> None:
        if self.reaper_task:
            self.reaper_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.reaper_task
        await self.http.aclose()

    def effective_idle_timeout(self, model: Model) -> float:
        # 按模型覆盖全局空闲阈值；None 用全局，0 表示该模型不自动空闲释放
        if model.idle_timeout_seconds is None:
            return self.idle_timeout
        return max(0.0, float(model.idle_timeout_seconds))

    async def idle_reaper(self) -> None:
        while True:
            await asyncio.sleep(self.idle_check_interval)
            async with self.lock:
                if self.active is None or self.inflight:
                    continue
                model = self.models[self.active]
                timeout = self.effective_idle_timeout(model)
                if not timeout:
                    continue
                idle_for = time.monotonic() - self.last_activity
                if idle_for < timeout:
                    continue
                logger.info(
                    "Releasing idle model %s after %.1f seconds (timeout=%.1f)",
                    model.id,
                    idle_for,
                    timeout,
                )
                await self.sleep_or_stop(model)
                self.active = None

    async def is_sleeping(self, model: Model) -> bool:
        # vLLM sleep 后 /v1/models 仍可能 200，必须查 /is_sleeping
        if not (model.kind == "vllm" and model.sleep_supported):
            return False
        try:
            response = await self.http.get(
                model.base_url + "/is_sleeping", timeout=5
            )
            if response.is_success:
                return bool(response.json().get("is_sleeping"))
        except (httpx.HTTPError, ValueError, TypeError):
            return False
        return False

    async def ready(self, model: Model) -> bool:
        try:
            if await self.is_sleeping(model):
                return False
            response = await self.http.get(
                model.base_url + model.readiness_path, timeout=5
            )
            return response.is_success
        except httpx.HTTPError:
            return False

    async def wait_ready(self, model: Model, seconds: int = 900) -> None:
        deadline = asyncio.get_running_loop().time() + seconds
        while asyncio.get_running_loop().time() < deadline:
            if await self.ready(model):
                return
            await asyncio.sleep(2)
        raise HTTPException(503, f"{model.id} did not become ready within {seconds}s")

    async def sleep_or_stop(self, model: Model) -> None:
        if model.release == "vllm_sleep":
            try:
                response = await self.http.post(
                    model.base_url + "/sleep?level=1", timeout=120
                )
                if response.is_success:
                    return
            except httpx.HTTPError:
                pass
        if model.release == "ollama_unload":
            name = model.upstream_model or model.id
            try:
                response = await self.http.post(
                    model.base_url + "/api/generate",
                    json={"model": name, "keep_alive": 0},
                    timeout=120,
                )
                if response.is_success:
                    logger.info("Unloaded Ollama model %s", name)
                    return
            except httpx.HTTPError as exc:
                logger.warning("Ollama unload failed for %s: %s", name, exc)
        if model.release == "comfy_free":
            try:
                response = await self.http.post(
                    model.base_url + "/free",
                    json={"unload_models": True, "free_memory": True},
                )
                if response.is_success:
                    return
            except httpx.HTTPError:
                pass
        await self.docker.stop(model.container)

    async def wake_or_start(self, model: Model) -> None:
        info = await self.docker.container(model.container)
        if info is None:
            raise HTTPException(503, f"Container {model.container} is not created")
        if not info["State"]["Running"]:
            await self.docker.start(model.container)
        elif model.kind == "vllm" and model.sleep_supported:
            # 容器 Running 但可能已 sleep；仅在 is_sleeping 时唤醒
            try:
                if await self.is_sleeping(model):
                    await self.http.post(model.base_url + "/wake_up", timeout=120)
            except httpx.HTTPError:
                pass
        await self.wait_ready(model)
        if model.release == "ollama_unload":
            name = model.upstream_model or model.id
            try:
                response = await self.http.post(
                    model.base_url + "/api/generate",
                    json={"model": name, "keep_alive": -1},
                    timeout=600,
                )
                response.raise_for_status()
                logger.info("Preloaded Ollama model %s", name)
            except httpx.HTTPError as exc:
                logger.warning("Ollama preload failed for %s: %s", name, exc)

    async def activate(self, model_id: str, reserve: bool = False) -> Model:
        model = self.models.get(model_id)
        if model is None:
            raise HTTPException(404, f"Unknown model: {model_id}")
        if not model.enabled:
            raise HTTPException(409, f"Model {model_id} is planned but not configured")
        async with self.lock:
            # Same-model requests may run concurrently. A cross-model request
            # must wait until the current workload finishes before unloading
            # its backend and reclaiming GPU memory.
            if self.active is not None and self.active != model_id:
                deadline = asyncio.get_running_loop().time() + self.switch_wait_timeout
                while self.inflight:
                    if asyncio.get_running_loop().time() >= deadline:
                        raise HTTPException(
                            503,
                            f"Timed out waiting for active model {self.active} to become idle",
                        )
                    await asyncio.sleep(0.25)
            if reserve:
                self.inflight += 1
            self.last_activity = time.monotonic()
            try:
                if self.active == model_id and await self.ready(model):
                    return model
                # Reconcile actual Docker state on every switch.  The in-memory
                # active marker is intentionally disposable, so a Dispatcher
                # restart must not leave an already-running model consuming GPU
                # memory while a different backend starts.
                for other in self.models.values():
                    if other.id == model_id or not other.enabled:
                        continue
                    info = await self.docker.container(other.container)
                    if info and info["State"]["Running"]:
                        await self.sleep_or_stop(other)
                await self.wake_or_start(model)
                self.active = model_id
                return model
            except BaseException:
                if reserve:
                    self.finish_request()
                raise

    def finish_request(self) -> None:
        self.inflight = max(0, self.inflight - 1)
        self.last_activity = time.monotonic()

    async def deactivate(self, model_id: str | None = None) -> str | None:
        async with self.lock:
            if self.inflight:
                raise HTTPException(
                    409, "Cannot release a model while requests are in flight"
                )
            selected = model_id or self.active
            if selected is None:
                return None
            model = self.models.get(selected)
            if model is None:
                raise HTTPException(404, f"Unknown model: {selected}")
            info = await self.docker.container(model.container)
            if info and info["State"]["Running"]:
                await self.sleep_or_stop(model)
            if self.active == selected:
                self.active = None
            self.last_activity = time.monotonic()
            return selected

    async def status(self) -> dict[str, Any]:
        rows = []
        for model in self.models.values():
            info = await self.docker.container(model.container)
            rows.append(
                {
                    "id": model.id,
                    "kind": model.kind,
                    "enabled": model.enabled,
                    "container": model.container,
                    "running": bool(info and info["State"]["Running"]),
                    "ready": await self.ready(model) if model.enabled else False,
                    "active": model.id == self.active,
                    "idle_timeout_seconds": self.effective_idle_timeout(model),
                }
            )
        effective = (
            self.effective_idle_timeout(self.models[self.active])
            if self.active and self.active in self.models
            else self.idle_timeout
        )
        return {
            "active": self.active,
            "inflight": self.inflight,
            "idle_seconds": round(time.monotonic() - self.last_activity, 1),
            "idle_timeout_seconds": effective,
            "global_idle_timeout_seconds": self.idle_timeout,
            "models": rows,
        }


@asynccontextmanager
async def lifespan(app: FastAPI):
    models = load_models(os.environ.get("MODELS_FILE", "/app/config/models.yaml"))
    docker = Docker()
    app.state.scheduler = Scheduler(models, docker)
    app.state.proxy = httpx.AsyncClient(timeout=httpx.Timeout(600, connect=10))
    try:
        await app.state.scheduler.start()
        yield
    finally:
        await app.state.proxy.aclose()
        await app.state.scheduler.close()
        await docker.close()


app = FastAPI(title="6000 Ada Multimodal Dispatcher", lifespan=lifespan)


@dataclass
class VideoJob:
    id: str
    model: str
    status: str = "queued"
    created: int = field(default_factory=lambda: int(time.time()))
    error: str | None = None
    output: dict[str, str] | None = None


video_jobs: dict[str, VideoJob] = {}


def require_admin(token: str | None) -> None:
    expected = os.environ.get("DISPATCHER_ADMIN_TOKEN")
    if not expected or token != expected:
        raise HTTPException(401, "Invalid dispatcher admin token")


async def proxy(
    request: Request,
    model: Model,
    suffix: str,
    payload: dict[str, Any] | None = None,
) -> Response:
    released = False

    def release_once() -> None:
        nonlocal released
        if not released:
            request.app.state.scheduler.finish_request()
            released = True

    try:
        body = (
            json.dumps(payload).encode("utf-8")
            if payload is not None
            else await request.body()
        )
        headers = {
            k: v
            for k, v in request.headers.items()
            if k.lower() not in {"host", "content-length"}
        }
        if payload is not None:
            headers["content-type"] = "application/json"
        upstream = request.app.state.proxy.build_request(
            request.method, model.base_url + suffix, content=body, headers=headers
        )
        response = await request.app.state.proxy.send(upstream, stream=True)
        passthrough = {
            k: v
            for k, v in response.headers.items()
            if k.lower() in {"content-type", "cache-control"}
        }
        if "text/event-stream" not in response.headers.get("content-type", ""):
            try:
                content = await response.aread()
            finally:
                await response.aclose()
                release_once()
            return Response(
                content=content, status_code=response.status_code, headers=passthrough
            )
    except BaseException:
        release_once()
        raise

    async def stream():
        try:
            async for chunk in response.aiter_raw():
                yield chunk
        finally:
            await response.aclose()
            release_once()

    return StreamingResponse(
        stream(), status_code=response.status_code, headers=passthrough
    )


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/v1/models")
async def models() -> dict[str, list[dict[str, str]]]:
    scheduler: Scheduler = app.state.scheduler
    return {
        "data": [
            {"id": model.id, "object": "model", "owned_by": "local"}
            for model in scheduler.models.values()
            if model.enabled
        ]
    }


@app.post("/v1/chat/completions")
async def chat_completions(request: Request) -> Response:
    payload = await request.json()
    model_id = payload.get("model")
    if not isinstance(model_id, str):
        raise HTTPException(400, "model is required")
    scheduler: Scheduler = request.app.state.scheduler
    model = await scheduler.activate(model_id, reserve=True)
    if model.kind not in {"vllm", "ollama"}:
        scheduler.finish_request()
        raise HTTPException(400, f"{model_id} is not a chat model")
    if model.upstream_model:
        payload["model"] = model.upstream_model
    return await proxy(request, model, "/v1/chat/completions", payload=payload)


def _load_comfy_workflow(
    env_key: str, default_path: str, override_path: str | None = None
) -> dict[str, Any]:
    # ComfyUI API 工作流模板；优先模型级路径，其次 env，最后默认
    path = Path(override_path or os.environ.get(env_key, default_path))
    if not path.is_file():
        raise HTTPException(503, f"ComfyUI workflow template missing: {path}")
    return json.loads(path.read_text())


def _parse_image_size(size: str | None) -> tuple[int, int]:
    raw = (size or "1024x1024").lower().replace("*", "x")
    if "x" not in raw:
        raise HTTPException(400, "size must look like 1024x1024")
    width_s, height_s = raw.split("x", 1)
    try:
        width, height = int(width_s), int(height_s)
    except ValueError as exc:
        raise HTTPException(400, "size must look like 1024x1024") from exc
    if not (64 <= width <= 2048 and 64 <= height <= 2048):
        raise HTTPException(400, "size out of supported range")
    return width, height


def _parse_sampler_knobs(payload: dict[str, Any]) -> tuple[int, int, float, str]:
    seed = payload.get("seed")
    if seed is None:
        seed = uuid.uuid4().int % (2**31)
    try:
        seed = int(seed)
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, "seed must be an integer") from exc
    steps = int(payload.get("steps") or os.environ.get("COMFY_T2I_STEPS", "30"))
    cfg = float(payload.get("cfg_scale") or os.environ.get("COMFY_T2I_CFG", "4"))
    negative = payload.get("negative_prompt") or " "
    if not isinstance(negative, str):
        raise HTTPException(400, "negative_prompt must be a string")
    return seed, steps, cfg, negative


async def _comfy_wait_images(
    client: httpx.AsyncClient, base_url: str, prompt_id: str
) -> list[dict[str, str]]:
    deadline = asyncio.get_running_loop().time() + float(
        os.environ.get("COMFY_T2I_TIMEOUT", "600")
    )
    outputs: dict[str, Any] | None = None
    while asyncio.get_running_loop().time() < deadline:
        history = await client.get(base_url + f"/history/{prompt_id}")
        history.raise_for_status()
        item = history.json().get(prompt_id)
        if item and item.get("outputs"):
            outputs = item["outputs"]
            break
        await asyncio.sleep(1.5)
    if outputs is None:
        raise HTTPException(504, "ComfyUI generation timed out")

    images: list[dict[str, str]] = []
    for node_out in outputs.values():
        for image in node_out.get("images", []):
            params = {
                "filename": image["filename"],
                "subfolder": image.get("subfolder", ""),
                "type": image.get("type", "output"),
            }
            view = await client.get(base_url + "/view", params=params)
            view.raise_for_status()
            images.append(
                {
                    "b64_json": base64.b64encode(view.content).decode("ascii"),
                }
            )
    if not images:
        raise HTTPException(502, "ComfyUI finished without image outputs")
    return images


async def _comfy_queue(
    client: httpx.AsyncClient, base_url: str, workflow: dict[str, Any]
) -> str:
    queue = await client.post(base_url + "/prompt", json={"prompt": workflow})
    if queue.status_code >= 400:
        raise HTTPException(502, f"ComfyUI prompt rejected: {queue.text[:500]}")
    prompt_id = queue.json().get("prompt_id")
    if not prompt_id:
        raise HTTPException(502, "ComfyUI did not return prompt_id")
    return prompt_id


def _replace_workflow_values(value: Any, replacements: dict[str, Any]) -> Any:
    """Replace exact {{name}} values without coupling the API to Comfy node IDs."""
    if isinstance(value, dict):
        return {key: _replace_workflow_values(item, replacements) for key, item in value.items()}
    if isinstance(value, list):
        return [_replace_workflow_values(item, replacements) for item in value]
    if isinstance(value, str) and value in replacements:
        return replacements[value]
    return value


async def _comfy_wait_video(
    client: httpx.AsyncClient, base_url: str, prompt_id: str
) -> dict[str, str]:
    deadline = asyncio.get_running_loop().time() + float(
        os.environ.get("COMFY_VIDEO_TIMEOUT", "3600")
    )
    while asyncio.get_running_loop().time() < deadline:
        history = await client.get(base_url + f"/history/{prompt_id}")
        history.raise_for_status()
        item = history.json().get(prompt_id)
        if item:
            status = item.get("status", {})
            if status.get("status_str") == "error":
                raise RuntimeError("ComfyUI video workflow failed")
            for node_out in item.get("outputs", {}).values():
                for key in ("videos", "gifs", "images"):
                    files = node_out.get(key, [])
                    if files:
                        output = files[0]
                        return {
                            "filename": output["filename"],
                            "subfolder": output.get("subfolder", ""),
                            "type": output.get("type", "output"),
                        }
        await asyncio.sleep(2)
    raise TimeoutError("ComfyUI video generation timed out")


async def _run_video_job(
    request: Request,
    model: Model,
    job: VideoJob,
    workflow: dict[str, Any],
) -> None:
    job.status = "running"
    try:
        prompt_id = await _comfy_queue(request.app.state.proxy, model.base_url, workflow)
        job.output = await _comfy_wait_video(
            request.app.state.proxy, model.base_url, prompt_id
        )
        job.status = "completed"
    except Exception as exc:
        logger.exception("Video job %s failed", job.id)
        job.status = "failed"
        job.error = str(exc)
    finally:
        request.app.state.scheduler.finish_request()


@app.post("/v1/videos/generations", status_code=202)
async def video_generation(request: Request) -> dict[str, Any]:
    payload = await request.json()
    model_id = payload.get("model")
    prompt = payload.get("prompt")
    if not isinstance(model_id, str) or not isinstance(prompt, str) or not prompt.strip():
        raise HTTPException(400, "model and prompt are required")

    scheduler: Scheduler = request.app.state.scheduler
    model = await scheduler.activate(model_id, reserve=True)
    if model.kind != "comfyui" or "video_generation" not in model.capabilities:
        scheduler.finish_request()
        raise HTTPException(400, f"{model_id} is not a video generation model")
    input_image = payload.get("input_image")
    if input_image is not None:
        if not isinstance(input_image, str) or not input_image.strip():
            scheduler.finish_request()
            raise HTTPException(400, "input_image must be a non-empty string")
        input_path = Path(input_image)
        if input_path.is_absolute() or ".." in input_path.parts:
            scheduler.finish_request()
            raise HTTPException(400, "input_image must be relative to the ComfyUI input directory")
        input_image = input_path.as_posix()

    workflow_path = model.i2v_video_workflow if input_image else model.video_workflow
    if not workflow_path:
        scheduler.finish_request()
        mode = "i2v_video_workflow" if input_image else "video_workflow"
        raise HTTPException(503, f"{model_id} has no {mode} configured")

    try:
        workflow = _load_comfy_workflow("COMFY_VIDEO_WORKFLOW", workflow_path)
        width, height = _parse_image_size(payload.get("size") or "832x480")
        steps = max(1, min(100, int(payload.get("steps", 20))))
        replacements = {
            "{{prompt}}": prompt,
            "{{negative_prompt}}": payload.get("negative_prompt") or "",
            "{{width}}": width,
            "{{height}}": height,
            "{{frames}}": max(1, min(257, int(payload.get("frames", 81)))),
            "{{fps}}": max(1, min(60, int(payload.get("fps", 16)))),
            "{{steps}}": steps,
            "{{switch_step}}": max(1, steps // 2),
            "{{cfg_scale}}": float(payload.get("cfg_scale", 5.0)),
            "{{seed}}": int(payload.get("seed", uuid.uuid4().int % (2**31))),
            "{{input_image}}": input_image or "",
        }
        workflow = _replace_workflow_values(workflow, replacements)
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        scheduler.finish_request()
        raise HTTPException(400, f"Invalid video workflow parameters: {exc}") from exc

    job = VideoJob(id=f"video-{uuid.uuid4().hex}", model=model.id)
    video_jobs[job.id] = job
    asyncio.create_task(_run_video_job(request, model, job, workflow))
    return asdict(job)


@app.get("/v1/videos/generations/{job_id}")
async def video_generation_status(job_id: str) -> dict[str, Any]:
    job = video_jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "Unknown video job")
    result = asdict(job)
    if job.status == "completed":
        result["content_url"] = f"/v1/videos/generations/{job.id}/content"
    return result


@app.get("/v1/videos/generations/{job_id}/content")
async def video_generation_content(job_id: str, request: Request) -> Response:
    job = video_jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "Unknown video job")
    if job.status != "completed" or not job.output:
        raise HTTPException(409, f"Video job is {job.status}")
    model = request.app.state.scheduler.models[job.model]
    response = await request.app.state.proxy.get(
        model.base_url + "/view", params=job.output
    )
    response.raise_for_status()
    return Response(
        content=response.content,
        media_type=response.headers.get("content-type", "video/mp4"),
    )


def _decode_image_bytes(payload: dict[str, Any]) -> bytes:
    # 支持 data URL / 纯 base64 / 远端 http(s) URL（由调用方解析后传入 bytes 字段）
    raw = payload.get("image") or payload.get("image_b64") or payload.get("b64_json")
    if isinstance(raw, str) and raw.strip():
        text = raw.strip()
        if text.startswith("data:") and "," in text:
            text = text.split(",", 1)[1]
        try:
            return base64.b64decode(text, validate=False)
        except Exception as exc:
            raise HTTPException(400, "image must be valid base64") from exc
    raise HTTPException(400, "image (base64) is required for edits")


async def _comfy_upload_image(
    client: httpx.AsyncClient, base_url: str, content: bytes, filename: str
) -> str:
    files = {"image": (filename, content, "application/octet-stream")}
    data = {"overwrite": "true"}
    upload = await client.post(base_url + "/upload/image", files=files, data=data)
    if upload.status_code >= 400:
        raise HTTPException(502, f"ComfyUI upload rejected: {upload.text[:500]}")
    name = upload.json().get("name")
    if not isinstance(name, str) or not name:
        raise HTTPException(502, "ComfyUI upload did not return image name")
    return name


async def _comfy_generate(model: Model, payload: dict[str, Any]) -> dict[str, Any]:
    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise HTTPException(400, "prompt is required")

    width, height = _parse_image_size(payload.get("size"))
    seed, steps, cfg, negative = _parse_sampler_knobs(payload)

    workflow = copy.deepcopy(
        _load_comfy_workflow(
            "COMFY_T2I_WORKFLOW",
            "/app/workflows/qwen_image_t2i_api.json",
            model.t2i_workflow,
        )
    )
    # 约定节点：6=正提示词，7=负提示词，3=KSampler，58=EmptyLatent/EmptySD3Latent
    workflow["6"]["inputs"]["text"] = prompt
    workflow["7"]["inputs"]["text"] = negative
    workflow["3"]["inputs"]["seed"] = seed
    workflow["3"]["inputs"]["steps"] = steps
    workflow["3"]["inputs"]["cfg"] = cfg
    workflow["58"]["inputs"]["width"] = width
    workflow["58"]["inputs"]["height"] = height

    client: httpx.AsyncClient = app.state.proxy
    prompt_id = await _comfy_queue(client, model.base_url, workflow)
    images = await _comfy_wait_images(client, model.base_url, prompt_id)
    return {
        "created": int(time.time()),
        "data": images,
        "model": model.id,
        "meta": {
            "seed": seed,
            "steps": steps,
            "cfg_scale": cfg,
            "size": f"{width}x{height}",
            "mode": "t2i",
        },
    }


async def _comfy_edit(model: Model, payload: dict[str, Any]) -> dict[str, Any]:
    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise HTTPException(400, "prompt is required")

    width, height = _parse_image_size(payload.get("size") or "1024x1024")
    seed, steps, cfg, negative = _parse_sampler_knobs(payload)
    denoise = float(
        payload.get("denoise")
        or payload.get("strength")
        or os.environ.get("COMFY_I2I_DENOISE", "0.75")
    )
    if not (0.05 <= denoise <= 1.0):
        raise HTTPException(400, "denoise/strength must be between 0.05 and 1.0")

    image_bytes = _decode_image_bytes(payload)
    client: httpx.AsyncClient = app.state.proxy
    uploaded = await _comfy_upload_image(
        client,
        model.base_url,
        image_bytes,
        filename=f"edit_{uuid.uuid4().hex}.png",
    )

    workflow = copy.deepcopy(
        _load_comfy_workflow(
            "COMFY_I2I_WORKFLOW",
            "/app/workflows/qwen_image_i2i_api.json",
            model.i2i_workflow,
        )
    )
    # 约定节点：10=LoadImage，11=ImageScale，6/7=提示词，3=KSampler
    workflow["10"]["inputs"]["image"] = uploaded
    workflow["11"]["inputs"]["width"] = width
    workflow["11"]["inputs"]["height"] = height
    workflow["6"]["inputs"]["text"] = prompt
    workflow["7"]["inputs"]["text"] = negative
    workflow["3"]["inputs"]["seed"] = seed
    workflow["3"]["inputs"]["steps"] = steps
    workflow["3"]["inputs"]["cfg"] = cfg
    workflow["3"]["inputs"]["denoise"] = denoise

    prompt_id = await _comfy_queue(client, model.base_url, workflow)
    images = await _comfy_wait_images(client, model.base_url, prompt_id)
    return {
        "created": int(time.time()),
        "data": images,
        "model": model.id,
        "meta": {
            "seed": seed,
            "steps": steps,
            "cfg_scale": cfg,
            "size": f"{width}x{height}",
            "denoise": denoise,
            "mode": "i2i",
            "source": uploaded,
        },
    }


@app.post("/v1/images/generations")
async def images(request: Request) -> JSONResponse:
    payload = await request.json()
    model_id = payload.get("model", "image-6000ada")
    scheduler: Scheduler = request.app.state.scheduler
    model = await scheduler.activate(model_id, reserve=True)
    try:
        if model.kind != "comfyui":
            raise HTTPException(400, f"{model_id} is not a ComfyUI backend")
        result = await _comfy_generate(model, payload)
        return JSONResponse(result)
    finally:
        scheduler.finish_request()


@app.post("/v1/images/edits")
async def image_edits(request: Request) -> JSONResponse:
    # OpenAI-ish 图生图：JSON body，image 为 base64（MCP / 本地客户端主通路）
    payload = await request.json()
    model_id = payload.get("model", "image-6000ada")
    scheduler: Scheduler = request.app.state.scheduler
    model = await scheduler.activate(model_id, reserve=True)
    try:
        if model.kind != "comfyui":
            raise HTTPException(400, f"{model_id} is not a ComfyUI backend")
        result = await _comfy_edit(model, payload)
        return JSONResponse(result)
    finally:
        scheduler.finish_request()


@app.get("/admin/status")
async def admin_status(
    x_dispatcher_token: str | None = Header(default=None),
) -> dict[str, Any]:
    require_admin(x_dispatcher_token)
    return await app.state.scheduler.status()


@app.post("/admin/activate/{model_id}")
async def admin_activate(
    model_id: str, x_dispatcher_token: str | None = Header(default=None)
) -> dict[str, str]:
    require_admin(x_dispatcher_token)
    model = await app.state.scheduler.activate(model_id)
    return {"active": model.id}


@app.post("/admin/release")
async def admin_release(
    x_dispatcher_token: str | None = Header(default=None),
) -> dict[str, str | None]:
    require_admin(x_dispatcher_token)
    released = await app.state.scheduler.deactivate()
    return {"released": released, "active": app.state.scheduler.active}


@app.post("/admin/release/{model_id}")
async def admin_release_model(
    model_id: str,
    x_dispatcher_token: str | None = Header(default=None),
) -> dict[str, str | None]:
    require_admin(x_dispatcher_token)
    released = await app.state.scheduler.deactivate(model_id)
    return {"released": released, "active": app.state.scheduler.active}
