import asyncio
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from fastapi import HTTPException

from dispatcher.main import Model, Scheduler


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


if __name__ == "__main__":
    unittest.main()
