"""Lifecycle tests run against HA's own Python environment before loading."""
import asyncio
import inspect
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

try:
    from custom_components.dreame_live_camera.manager import LiveCamera
except ModuleNotFoundError:
    LiveCamera = None


@unittest.skipIf(LiveCamera is None, "Run these tests in the HA Core container")
class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_shutdown_listener_is_async_and_awaits_cleanup(self):
        from custom_components.dreame_live_camera import async_setup_entry

        manager = SimpleNamespace(async_stop=AsyncMock())
        cloud = SimpleNamespace(logged_in=True)
        device = SimpleNamespace(_protocol=SimpleNamespace(cloud=cloud))
        hass = SimpleNamespace(
            data={"dreame_vacuum": {"vacuum-entry": SimpleNamespace(_device=device)}},
            bus=SimpleNamespace(async_listen_once=Mock()),
            config_entries=SimpleNamespace(async_forward_entry_setups=AsyncMock()),
        )
        entry = SimpleNamespace(
            data={"vacuum_entity_id": "vacuum.test"}, options={},
            async_on_unload=Mock(), add_update_listener=Mock(),
        )
        vacuum = SimpleNamespace(platform="dreame_vacuum", config_entry_id="vacuum-entry")
        with patch("custom_components.dreame_live_camera.er.async_get") as registry, patch(
            "custom_components.dreame_live_camera.LiveCamera", return_value=manager
        ):
            registry.return_value.async_get.return_value = vacuum
            self.assertTrue(await async_setup_entry(hass, entry))

        handler = hass.bus.async_listen_once.call_args.args[1]
        self.assertTrue(inspect.iscoroutinefunction(handler))
        await handler(None)
        manager.async_stop.assert_awaited_once()

    def manager(self):
        manager = LiveCamera(None, None, "test-pin", 60)
        manager.state = AsyncMock(return_value={"session":"ours", "status":1})
        manager.action = AsyncMock(return_value={"code":0})
        return manager

    async def test_stop_our_session_clears_images_and_timers(self):
        manager = self.manager()
        manager.session, manager.active, manager.image = "ours", True, b"private-image"
        manager.lease = asyncio.get_running_loop().call_later(3600, lambda:None)
        manager.tasks = [asyncio.create_task(asyncio.sleep(3600))]
        await manager.async_stop()
        manager.action.assert_awaited_once_with(1, 1, {"operType":"monitor", "operation":"end"})
        self.assertFalse(manager.active)
        self.assertTrue(manager.cleanup_ok)
        self.assertIsNone(manager.image)
        self.assertIsNone(manager.lease)
        await manager.async_stop()
        self.assertEqual(manager.action.await_count, 1)

    async def test_receiver_stop_retains_safe_transport_evidence(self):
        manager = self.manager()
        receiver = SimpleNamespace(aclose=AsyncMock(), peer="10.0.0.9", datagrams=42, cleanup_confirmed=True)
        manager.receiver = receiver
        await manager.async_stop()
        receiver.aclose.assert_awaited_once()
        self.assertEqual(manager.last_peer, "10.0.0.9")
        self.assertEqual(manager.last_datagrams, 42)
        self.assertTrue(manager.media_cleanup_ok)
        self.assertIsNone(manager.receiver)

    async def test_do_not_stop_phone_session(self):
        manager = self.manager()
        manager.session = "ours"
        manager.state.return_value = {"session":"phone", "status":1}
        await manager.async_stop()
        manager.action.assert_not_awaited()
        self.assertTrue(manager.cleanup_ok)

    async def test_failed_cleanup_is_reported(self):
        manager = self.manager()
        manager.session = "ours"
        manager.action.return_value = {"code":-1}
        await manager.async_stop()
        self.assertFalse(manager.cleanup_ok)
        self.assertEqual(manager.stage, "Camera cleanup could not be confirmed")

    async def test_open_viewer_cancels_idle_timer(self):
        manager = self.manager()
        manager.active = True
        handle = asyncio.get_running_loop().call_later(3600, lambda:None)
        manager.grace = handle
        key = manager.viewer_open()
        self.assertTrue(handle.cancelled())
        self.assertEqual(len(manager.viewers), 1)
        manager.viewer_close(key)
        self.assertEqual(len(manager.viewers), 0)
        await manager.async_stop()

    async def test_camera_preview_does_not_start_hardware(self):
        from custom_components.dreame_live_camera.camera import DreameLiveCamera
        manager = self.manager()
        manager.async_start = AsyncMock()
        camera = DreameLiveCamera(SimpleNamespace(runtime_data=manager, unique_id="test", title="Test Live Camera"))
        self.assertIsNone(await camera.async_camera_image())
        manager.async_start.assert_not_awaited()
        manager.image = b"cached-frame"
        self.assertEqual(await camera.async_camera_image(), b"cached-frame")
        manager.async_start.assert_not_awaited()

    async def test_pin_is_not_prefilled_in_options(self):
        from custom_components.dreame_live_camera.config_flow import schema, valid_pin
        form = schema({"pin":"321987", "max_duration":60}, False)
        self.assertNotIn("321987", str(form.schema))
        self.assertTrue(valid_pin("1234"))
        self.assertFalse(valid_pin("not-a-pin"))


if __name__ == "__main__":
    unittest.main()
