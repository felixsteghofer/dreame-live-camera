"""Transport recovery and cancellation without private media or a real robot."""
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from custom_components.dreame_live_camera import manager as module


class Process:
    def __init__(self):
        self.stdin = SimpleNamespace(write=Mock(), drain=AsyncMock())
        self.stdout = asyncio.StreamReader()
        self.returncode = None

    def terminate(self):
        self.returncode = 0

    def kill(self):
        self.returncode = -9

    async def wait(self):
        return self.returncode


class Receiver:
    def __init__(self, process, mode="live"):
        self.process, self.mode = process, mode
        self.peer, self.datagrams, self.received = "10.0.0.9", 0, 0
        self.cleanup_confirmed = False
        self.started = asyncio.Event()
        self.fail = asyncio.Event()

    async def run(self, output):
        self.started.set()
        if self.mode == "fail":
            raise RuntimeError("private URL must not reach state")
        if self.mode == "live":
            await output(b"synthetic source bytes")
            self.datagrams, self.received = 1, 100
            self.process.stdout.feed_data(b"\xff\xd8synthetic JPEG\xff\xd9")
        await self.fail.wait()
        raise RuntimeError("transport lost")

    async def aclose(self):
        self.cleanup_confirmed = True


class FallbackTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.hass = SimpleNamespace(async_create_background_task=lambda coro, name:asyncio.create_task(coro))
        self.manager = module.LiveCamera(self.hass, None, "test-pin", 60)
        self.manager.active, self.manager.session = True, "ours"
        self.manager.state = AsyncMock(return_value={"status":1, "session":"ours"})
        self.manager.action = AsyncMock(return_value={"code":0})
        self.identity = SimpleNamespace(offer=AsyncMock(return_value="wss://video.aliyuncs.com/?session=test"),
                                        relay=AsyncMock(return_value="rtmp://127.0.0.1/live/synthetic"))
        self.lan_process, self.relay_process = Process(), Process()

    async def asyncTearDown(self):
        await self.manager.async_stop()

    def patch_media(self, lan, relay):
        patches = [
            patch.object(module, "Receiver", return_value=lan),
            patch.object(module, "RelayReceiver", return_value=relay),
            patch.object(module, "async_get_clientsession", return_value=None),
            patch.object(module.asyncio, "create_subprocess_exec", new=AsyncMock(side_effect=[self.lan_process,self.relay_process])),
            patch.object(module, "FIRST_FRAME_TIMEOUT", 0.05),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def run_media(self, lan, relay):
        self.patch_media(lan,relay)
        task = asyncio.create_task(self.manager.media(self.identity, "synthetic", "10.0.0.9"))
        self.manager.tasks.append(task)
        return task

    async def live(self, transport):
        async with asyncio.timeout(1):
            while self.manager.transport != transport or not self.manager.stage.startswith("Live"):
                await asyncio.sleep(0.001)

    async def test_successful_lan_does_not_query_or_open_relay(self):
        lan, relay = Receiver(self.lan_process), Receiver(self.relay_process)
        self.run_media(lan, relay)
        await self.live("direct_lan")
        self.identity.relay.assert_not_awaited()
        self.assertFalse(relay.started.is_set())
        self.assertIsNone(self.manager.fallback_reason)

    async def test_lan_failure_closes_decoder_before_opening_relay(self):
        lan, relay = Receiver(self.lan_process,"fail"), Receiver(self.relay_process)
        async def relay_url(channel):
            self.assertTrue(lan.cleanup_confirmed)
            self.assertEqual(self.lan_process.returncode,0)
            return "rtmp://127.0.0.1/live/synthetic"
        self.identity.relay.side_effect = relay_url
        self.run_media(lan,relay)
        await self.live("cloud_relay")
        self.assertEqual(self.manager.stage,"Live (relay)")
        self.assertEqual(self.manager.fallback_reason,"LAN video unavailable")
        await self.manager.async_stop()
        self.assertTrue(relay.cleanup_confirmed)
        self.assertTrue(self.manager.media_cleanup_ok)
        self.assertTrue(self.manager.cleanup_ok)
        self.assertEqual(self.manager.last_relay_bytes,100)
        self.assertIsNone(self.manager.image)
        self.assertEqual(self.relay_process.returncode,0)
        self.assertFalse(self.manager.media_tasks)

    async def test_no_first_frame_falls_back_even_with_open_lan_socket(self):
        lan, relay = Receiver(self.lan_process,"stall"), Receiver(self.relay_process)
        self.run_media(lan,relay)
        await self.live("cloud_relay")
        self.assertTrue(lan.cleanup_confirmed)

    async def test_lan_loss_after_live_switches_to_relay(self):
        lan, relay = Receiver(self.lan_process), Receiver(self.relay_process)
        self.run_media(lan,relay)
        await self.live("direct_lan")
        lan.fail.set()
        await self.live("cloud_relay")
        self.assertEqual(self.manager.frames,2)

    async def test_decoder_eof_falls_back(self):
        lan, relay = Receiver(self.lan_process,"stall"), Receiver(self.relay_process)
        self.run_media(lan,relay)
        await lan.started.wait()
        self.lan_process.stdout.feed_eof()
        await self.live("cloud_relay")
        self.assertTrue(lan.cleanup_confirmed)

    async def test_stop_during_lan_attempt_does_not_start_relay(self):
        lan, relay = Receiver(self.lan_process,"stall"), Receiver(self.relay_process)
        self.run_media(lan,relay)
        await lan.started.wait()
        await self.manager.async_stop()
        self.identity.relay.assert_not_awaited()
        self.assertTrue(lan.cleanup_confirmed)
        self.assertIsNone(self.manager.receiver)

    async def test_both_transports_fail_and_owned_monitor_is_stopped(self):
        lan, relay = Receiver(self.lan_process,"fail"), Receiver(self.relay_process,"fail")
        task = self.run_media(lan,relay)
        await asyncio.wait_for(task,1)
        self.assertFalse(self.manager.active)
        self.assertTrue(self.manager.cleanup_ok)
        self.assertEqual(self.manager.stage,"Video connection failed")
        self.assertIsNone(self.manager.receiver)
        self.manager.action.assert_awaited_once_with(1,1,{"operType":"monitor","operation":"end"})

    async def test_unavailable_lan_signaling_can_use_relay(self):
        self.identity.offer.side_effect = RuntimeError("Direct LAN unavailable")
        # No LAN decoder was started, so the first process belongs to relay.
        relay = Receiver(self.lan_process)
        self.run_media(Receiver(self.lan_process,"fail"),relay)
        await self.live("cloud_relay")

    async def test_relay_failure_does_not_stop_new_phone_session(self):
        self.manager.state.return_value = {"status":1,"session":"phone"}
        task = self.run_media(Receiver(self.lan_process,"fail"),Receiver(self.relay_process,"fail"))
        await asyncio.wait_for(task,1)
        self.manager.action.assert_not_awaited()
        self.assertTrue(self.manager.cleanup_ok)

    async def test_fallback_preserves_original_session_limit(self):
        limit = asyncio.get_running_loop().call_later(60,lambda:None)
        self.manager.lease = limit
        self.run_media(Receiver(self.lan_process,"fail"),Receiver(self.relay_process))
        await self.live("cloud_relay")
        self.assertIs(self.manager.lease,limit)
        self.assertFalse(limit.cancelled())

    async def test_startup_both_fail_without_lock_deadlock(self):
        self.patch_media(Receiver(self.lan_process,"fail"),Receiver(self.relay_process,"fail"))
        self.manager.active = False
        self.manager.session = None
        self.manager.cloud = SimpleNamespace(
            _country="eu",_stream_key="synthetic",_uuid="synthetic",
            get_device_info=Mock(return_value={"localIp":"10.0.0.9"}),
            _api_call=Mock(return_value={"code":0,"data":"synthetic"}),
        )
        async def call(function,*args): return function(*args)
        async def state(): return {"status":1 if self.manager.session else 0,"session":self.manager.session}
        self.manager.call, self.manager.state = call, state
        self.identity.authenticate = AsyncMock()
        with patch.object(module,"VideoIdentity",return_value=self.identity):
            with self.assertRaisesRegex(RuntimeError,"LAN and relay"):
                await asyncio.wait_for(self.manager.async_start(),1)
        self.assertFalse(self.manager.active)
        self.assertIsNone(self.manager.session)
        self.assertTrue(self.manager.cleanup_ok)
        self.assertFalse(self.manager.tasks)

    async def test_new_session_tries_lan_again(self):
        self.manager.transport = "cloud_relay"
        self.manager.fallback_reason = "LAN video unavailable"
        self.run_media(Receiver(self.lan_process),Receiver(self.relay_process))
        await self.live("direct_lan")
        self.identity.offer.assert_awaited_once()
        self.identity.relay.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
