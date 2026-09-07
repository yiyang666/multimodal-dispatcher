import asyncio
import base64
import copy
import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
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
    capabilities: tuple[str, ...] = ()


def load_models(path: str) -> dict[str, Model]:
    raw = yaml.safe_load(Path(path).read_text()) or {}
    models: dict[str, Model] = {}
    for model_id, value in raw.get("models", {}).items():
        models[model_id] = Model(
            id=model_id,
            kind=value["kind"],
            enabled=bool(value.get("enabled", False)),
            container=value["container"],
            base_url=value["base_url"].rstrip("/"),
            readiness_path=value.get("readiness_path", "/health"),
            sleep_supported=bool(value.get("sleep_supported", False)),
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
        self.idle_timeout = max(0.0, float(os.environ.get("IDLE_TIMEOUT_SECONDS", "600")))
        default_check = min(30.0, max(1.0, self.idle_timeout / 4)) if self.idle_timeout else 30.0
        self.idle_check_interval = max(
            1.0, float(os.environ.get("IDLE_CHECK_INTERVAL_SECONDS", str(default_check)))
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

    async def idle_reaper(self) -> None:
        while True:
            await asyncio.sleep(self.idle_check_interval)
            if not self.idle_timeout:
                continue
            async with self.lock:
                if self.active is None or self.inflight:
                    continue
                idle_for = time.monotonic() - self.last_activity
                if idle_for < self.idle_timeout:
                    continue
                model = self.models[self.active]
                logger.info("Releasing idle model %s after %.1f seconds", model.id, idle_for)
                await self.sleep_or_stop(model)
                self.active = None

    async def ready(self, model: Model) -> bool:
        try:
            response = await self.http.get(model.base_url + model.readiness_path, timeout=5)
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
        if model.kind == "vllm" and model.sleep_supported:
            try:
                response = await self.http.post(model.base_url + "/sleep?level=1", timeout=30)
                if response.is_success:
                    return
            except httpx.HTTPError:
                pass
        if model.kind == "comfyui":
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
            try:
                await self.http.post(model.base_url + "/wake_up", timeout=60)
            except httpx.HTTPError:
                pass
        await self.wait_ready(model)

    async def activate(self, model_id: str, reserve: bool = False) -> Model:
        model = self.models.get(model_id)
        if model is None:
            raise HTTPException(404, f"Unknown model: {model_id}")
        if not model.enabled:
            raise HTTPException(409, f"Model {model_id} is planned but not configured")
        async with self.lock:
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
                    self.release()
                raise

    def release(self) -> None:
        self.inflight = max(0, self.inflight - 1)
        self.last_activity = time.monotonic()

    async def status(self) -> dict[str, Any]:
        rows = []
        for model in self.models.values():
            info = await self.docker.container(model.container)
            rows.append({
                "id": model.id,
                "kind": model.kind,
                "enabled": model.enabled,
                "container": model.container,
                "running": bool(info and info["State"]["Running"]),
                "ready": await self.ready(model) if model.enabled else False,
                "active": model.id == self.active,
            })
        return {
            "active": self.active,
            "inflight": self.inflight,
            "idle_seconds": round(time.monotonic() - self.last_activity, 1),
            "idle_timeout_seconds": self.idle_timeout,
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


def require_admin(token: str | None) -> None:
    expected = os.environ.get("DISPATCHER_ADMIN_TOKEN")
    if not expected or token != expected:
        raise HTTPException(401, "Invalid dispatcher admin token")


async def proxy(request: Request, model: Model, suffix: str) -> Response:
    released = False

    def release_once() -> None:
        nonlocal released
        if not released:
            request.app.state.scheduler.release()
            released = True

    try:
        body = await request.body()
        headers = {k: v for k, v in request.headers.items() if k.lower() not in {"host", "content-length"}}
        upstream = request.app.state.proxy.build_request(
            request.method, model.base_url + suffix, content=body, headers=headers
        )
        response = await request.app.state.proxy.send(upstream, stream=True)
        passthrough = {k: v for k, v in response.headers.items() if k.lower() in {"content-type", "cache-control"}}
        if "text/event-stream" not in response.headers.get("content-type", ""):
            try:
                content = await response.aread()
            finally:
                await response.aclose()
                release_once()
            return Response(content=content, status_code=response.status_code, headers=passthrough)
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

    return StreamingResponse(stream(), status_code=response.status_code, headers=passthrough)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/v1/models")
async def models() -> dict[str, list[dict[str, str]]]:
    scheduler: Scheduler = app.state.scheduler
    return {"data": [{"id": model.id, "object": "model", "owned_by": "local"} for model in scheduler.models.values() if model.enabled]}


@app.post("/v1/chat/completions")
async def chat_completions(request: Request) -> Response:
    payload = await request.json()
    model_id = payload.get("model")
    if not isinstance(model_id, str):
        raise HTTPException(400, "model is required")
    scheduler: Scheduler = request.app.state.scheduler
    model = await scheduler.activate(model_id, reserve=True)
    if model.kind != "vllm":
        scheduler.release()
        raise HTTPException(400, f"{model_id} is not a chat model")
    return await proxy(request, model, "/v1/chat/completions")


def _load_comfy_workflow() -> dict[str, Any]:
    # Qwen-Image 文生图 API 工作流模板；部署时挂载到 /app/workflows
    path = Path(os.environ.get("COMFY_T2I_WORKFLOW", "/app/workflows/qwen_image_t2i_api.json"))
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


async def _comfy_generate(model: Model, payload: dict[str, Any]) -> dict[str, Any]:
    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise HTTPException(400, "prompt is required")

    width, height = _parse_image_size(payload.get("size"))
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

    workflow = copy.deepcopy(_load_comfy_workflow())
    # 约定节点：6=正提示词，7=负提示词，3=KSampler，58=EmptySD3LatentImage
    workflow["6"]["inputs"]["text"] = prompt
    workflow["7"]["inputs"]["text"] = negative
    workflow["3"]["inputs"]["seed"] = seed
    workflow["3"]["inputs"]["steps"] = steps
    workflow["3"]["inputs"]["cfg"] = cfg
    workflow["58"]["inputs"]["width"] = width
    workflow["58"]["inputs"]["height"] = height

    client: httpx.AsyncClient = app.state.proxy
    queue = await client.post(model.base_url + "/prompt", json={"prompt": workflow})
    if queue.status_code >= 400:
        raise HTTPException(502, f"ComfyUI prompt rejected: {queue.text[:500]}")
    prompt_id = queue.json().get("prompt_id")
    if not prompt_id:
        raise HTTPException(502, "ComfyUI did not return prompt_id")

    deadline = asyncio.get_running_loop().time() + float(os.environ.get("COMFY_T2I_TIMEOUT", "600"))
    outputs: dict[str, Any] | None = None
    while asyncio.get_running_loop().time() < deadline:
        history = await client.get(model.base_url + f"/history/{prompt_id}")
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
            view = await client.get(model.base_url + "/view", params=params)
            view.raise_for_status()
            images.append({
                "b64_json": base64.b64encode(view.content).decode("ascii"),
            })
    if not images:
        raise HTTPException(502, "ComfyUI finished without image outputs")
    return {
        "created": int(time.time()),
        "data": images,
        "model": model.id,
        "meta": {"seed": seed, "steps": steps, "cfg_scale": cfg, "size": f"{width}x{height}"},
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
        scheduler.release()


@app.get("/admin/status")
async def admin_status(x_dispatcher_token: str | None = Header(default=None)) -> dict[str, Any]:
    require_admin(x_dispatcher_token)
    return await app.state.scheduler.status()


@app.post("/admin/activate/{model_id}")
async def admin_activate(model_id: str, x_dispatcher_token: str | None = Header(default=None)) -> dict[str, str]:
    require_admin(x_dispatcher_token)
    model = await app.state.scheduler.activate(model_id)
    return {"active": model.id}
