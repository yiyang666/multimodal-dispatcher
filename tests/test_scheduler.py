import asyncio
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from fastapi import HTTPException

from dispatcher.main import Model, Scheduler, _replace_workflow_values, load_models


class FakeDocker:
    def __init__(self) -> None:
        self.running = {"agent": False, "image": True}

    async def container(self, name: str):
        return {"State": {"Running": self.running[name]}}


class SchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.env = patch.dict(
            os.environ,
            {
                "IDLE_TIMEOUT_SECONDS": "600",
                "IDLE_CHECK_INTERVAL_SECONDS": "30",
                "SWITCH_WAIT_TIMEOUT_SECONDS": "2",
            },
        )
        self.env.start()
        self.agent = Model("agent", "vllm", True, "agent", "http://agent", "/health")
        self.image = Model("image", "comfyui", True, "image", "http://image", "/health")
        self.scheduler = Scheduler(
            {"agent": self.agent, "image": self.image},
            FakeDocker(),
        )
        self.scheduler.ready = AsyncMock(return_value=True)
        self.scheduler.wake_or_start = AsyncMock()
        self.scheduler.sleep_or_stop = AsyncMock()

    async def asyncTearDown(self) -> None:
        await self.scheduler.close()
        self.env.stop()

    async def test_cross_model_switch_waits_for_inflight_request(self) -> None:
        self.scheduler.active = "image"
        self.scheduler.inflight = 1

        activation = asyncio.create_task(self.scheduler.activate("agent", reserve=True))
        await asyncio.sleep(0.05)
        self.assertFalse(activation.done())

        self.scheduler.finish_request()
        model = await activation

        self.assertEqual(model.id, "agent")
        self.assertEqual(self.scheduler.active, "agent")
        self.assertEqual(self.scheduler.inflight, 1)
        self.scheduler.sleep_or_stop.assert_awaited_once_with(self.image)
        self.scheduler.finish_request()

    async def test_manual_release_rejects_inflight_work(self) -> None:
        self.scheduler.active = "image"
        self.scheduler.inflight = 1

        with self.assertRaises(HTTPException) as raised:
            await self.scheduler.deactivate()
        self.assertEqual(raised.exception.status_code, 409)

        self.scheduler.finish_request()
        released = await self.scheduler.deactivate()
        self.assertEqual(released, "image")
        self.assertIsNone(self.scheduler.active)
        self.scheduler.sleep_or_stop.assert_awaited_once_with(self.image)

    async def test_release_is_idempotent_without_active_model(self) -> None:
        self.assertIsNone(await self.scheduler.deactivate())

    async def test_ollama_release_unloads_without_stopping_container(self) -> None:
        ollama = Model(
            "public-id",
            "ollama",
            True,
            "ollama",
            "http://ollama:11434",
            "/api/tags",
            upstream_model="actual:tag",
            release="ollama_unload",
        )
        response = AsyncMock()
        response.is_success = True
        self.scheduler.http.post = AsyncMock(return_value=response)
        self.scheduler.docker.stop = AsyncMock()

        await Scheduler.sleep_or_stop(self.scheduler, ollama)

        self.scheduler.http.post.assert_awaited_once_with(
            "http://ollama:11434/api/generate",
            json={"model": "actual:tag", "keep_alive": 0},
            timeout=120,
        )
        self.scheduler.docker.stop.assert_not_awaited()


class ModelConfigTests(unittest.TestCase):
    def test_ollama_defaults_to_unload_release(self) -> None:
        with self.subTest("load sample config"):
            models = load_models(
                str(Path(__file__).parents[1] / "config" / "models.example.yaml")
            )
        model = models["local-rp-example"]
        self.assertEqual(model.kind, "ollama")
        self.assertEqual(model.release, "ollama_unload")
        self.assertEqual(model.upstream_model, "example-model:latest")

    def test_video_workflow_placeholders_preserve_types(self) -> None:
        workflow = {
            "prompt": "{{prompt}}",
            "width": "{{width}}",
            "nested": ["unchanged", "{{frames}}"],
        }
        replaced = _replace_workflow_values(
            workflow,
            {"{{prompt}}": "a test clip", "{{width}}": 832, "{{frames}}": 81},
        )
        self.assertEqual(replaced["prompt"], "a test clip")
        self.assertEqual(replaced["width"], 832)
        self.assertEqual(replaced["nested"], ["unchanged", 81])
        self.assertEqual(workflow["width"], "{{width}}")

    def test_video_model_loads_image_to_video_workflow(self) -> None:
        models = load_models(
            str(Path(__file__).parents[1] / "config" / "models.example.yaml")
        )
        model = models["video-6000ada"]
        self.assertEqual(
            model.i2v_video_workflow,
            "/app/workflows/wan22_5b_i2v_api.json",
        )


if __name__ == "__main__":
    unittest.main()
