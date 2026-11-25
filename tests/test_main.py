"""Integration tests use actual AstrBot 4.29.4 message classes and decorators."""

from __future__ import annotations

import asyncio
import copy
import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import At, Image, Plain

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "mywife_test_plugin"
package = types.ModuleType(PACKAGE)
package.__path__ = [str(ROOT)]
sys.modules[PACKAGE] = package
spec = importlib.util.spec_from_file_location(PACKAGE + ".main", ROOT / "main.py")
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


class Context:
    def get_config(self, *_):
        return {"timezone": "Asia/Shanghai"}


class Event:
    chain_result = AstrMessageEvent.chain_result

    def __init__(
        self, text="jrlp", platform="aiocqhttp", group="group-a", user="user-a", bot="bot-a"
    ):
        self.message_str, self.platform, self.group, self.user, self.bot = (
            text,
            platform,
            group,
            user,
            bot,
        )
        self.sent, self.stopped = [], False
        self.unified_msg_origin = f"{platform}:{group}:{user}"

    def get_platform_name(self):
        return self.platform

    def get_platform_id(self):
        return "instance-a"

    def get_self_id(self):
        return self.bot

    def get_group_id(self):
        return self.group

    def get_sender_id(self):
        return self.user

    def get_sender_name(self):
        return "Alice"

    def get_messages(self):
        return []

    def is_admin(self):
        return False

    def stop_event(self):
        self.stopped = True

    async def send(self, result):
        self.sent.append(result)


class Plugin(module.MyWifePlugin):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.kv = {}
        self.fail_save = False

    async def get_kv_data(self, key, default):
        return copy.deepcopy(self.kv.get(key, default))

    async def put_kv_data(self, key, value):
        if self.fail_save:
            raise OSError("Simulated unavailable database")
        self.kv[key] = copy.deepcopy(value)


class MainTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data = Path(self.temp.name)
        self.patcher = patch.object(module.StarTools, "get_data_dir", return_value=self.data)
        self.patcher.start()
        self.plugin = Plugin(Context(), {"reply_mode": "template", "enable_images": False})

    async def asyncTearDown(self):
        await self.plugin.terminate()
        self.patcher.stop()
        self.temp.cleanup()

    async def test_command_and_no_prefix_listener_handle_one_event_once(self):
        event = Event()
        await self.plugin.jrlp(event)
        await self.plugin.on_message(event)
        self.assertEqual(len(event.sent), 1)
        self.assertTrue(event.stopped)
        self.assertEqual(len(next(iter(self.plugin.kv.values()))["history"]), 1)

    async def test_concurrent_draws_have_single_persistent_result(self):
        events = [Event() for _ in range(12)]
        await asyncio.gather(*(self.plugin._dispatch(event) for event in events))
        state = next(iter(self.plugin.kv.values()))
        self.assertEqual(len(state["history"]), 1)
        self.assertEqual(state["redraw_count"], 0)
        self.assertTrue(
            all(state["wife"]["name"] in event.sent[0].chain[0].text for event in events)
        )

    async def test_platform_bot_group_and_user_state_isolation(self):
        keys = {
            self.plugin._state_key(event)
            for event in (
                Event(),
                Event(platform="qq_official"),
                Event(group="group-b"),
                Event(user="user-b"),
                Event(bot="bot-b"),
            )
        }
        self.assertEqual(len(keys), 5)
        self.plugin.config["state_scope"] = "user"
        self.assertEqual(
            self.plugin._state_key(Event()), self.plugin._state_key(Event(group="group-b"))
        )

    async def test_official_adapter_pseudo_mention_is_the_current_bot(self):
        event = Event(platform="qq_official", bot="unknown_selfid")
        event.get_messages = lambda: [At(qq="qq_official")]
        await self.plugin._dispatch(event)
        self.assertEqual(len(event.sent), 1)
        event = Event(platform="qq_official")
        event.get_messages = lambda: [At(qq="another_user")]
        await self.plugin._dispatch(event)
        self.assertEqual(event.sent, [])

    async def test_save_failure_does_not_send_a_success_or_commit_history(self):
        event = Event()
        self.plugin.fail_save = True
        await self.plugin._dispatch(event)
        self.assertEqual(self.plugin.kv, {})
        self.assertIn("未能完成", event.sent[0].chain[0].text)

    async def test_qq_user_scope_has_stable_identity_across_adapter_pseudo_self_ids(self):
        self.plugin.config["state_scope"] = "user"
        private = Event(platform="qq_official", bot="unknown_selfid", group="")
        group = Event(platform="qq_official", bot="qq_official", group="group-a")
        channel = Event(platform="qq_official", bot="appid", group="channel-a")
        self.assertEqual(self.plugin._state_key(private), self.plugin._state_key(group))
        self.assertEqual(self.plugin._state_key(private), self.plugin._state_key(channel))
        other_bot = Event(platform="qq_official", bot="appid", group="group-a")
        other_bot.get_platform_id = lambda: "other-instance"
        self.assertNotEqual(self.plugin._state_key(private), self.plugin._state_key(other_bot))

    async def test_qq_markdown_text_and_image_are_one_plain_markdown_payload(self):
        event = Event(platform="qq_official")
        self.plugin.config["enable_images"] = True
        self.plugin.images.get_image = AsyncMock(return_value=self.data / "image.png")
        self.plugin.images.image_dimensions = AsyncMock(return_value=(1000, 2000))
        self.plugin.qq_images.get_image_url = AsyncMock(
            return_value="qqbot-media://image/v1/mywife-native-image"
        )
        await self.plugin._send(event, "Alice 的角色", {"image_path": "img1/A!B.jpg"})
        self.assertEqual(len(event.sent), 1)
        result = event.sent[0]
        self.assertTrue(result.use_markdown_)
        self.assertEqual(len(result.chain), 1)
        self.assertIsInstance(result.chain[0], Plain)
        self.assertIn("![角色图片 #480px #960px]", result.chain[0].text)
        self.assertIn("Alice 的角色", result.chain[0].text)
        self.assertIn("qqbot-media://image/v1/", result.chain[0].text)
        self.assertNotIn("raw.githubusercontent.com", result.chain[0].text)

    async def test_failed_native_upload_falls_back_to_one_normal_text_image_chain(self):
        event = Event(platform="qq_official")
        self.plugin.config["enable_images"] = True
        self.plugin.images.get_image = AsyncMock(return_value=self.data / "image.png")
        self.plugin.images.image_dimensions = AsyncMock(return_value=(8, 8))
        self.plugin.qq_images.get_image_url = AsyncMock(return_value=None)
        await self.plugin._send(event, "角色结果", {"image_path": "img1/A!B.jpg"})
        self.assertEqual(len(event.sent), 1)
        self.assertFalse(event.sent[0].use_markdown_)
        self.assertIsInstance(event.sent[0].chain[0], Plain)
        self.assertIsInstance(event.sent[0].chain[1], Image)
        self.assertNotIn("http", event.sent[0].chain[0].text)

    async def test_native_failure_does_not_show_markdown_escape_slashes(self):
        event = Event(platform="qq_official")
        self.plugin.config["enable_images"] = True
        self.plugin.images.get_image = AsyncMock(return_value=self.data / "image.png")
        self.plugin.images.image_dimensions = AsyncMock(return_value=(8, 8))
        self.plugin.qq_images.get_image_url = AsyncMock(return_value=None)
        await self.plugin._send(
            event, r"Alice\_Isla\[星\]，老婆是角色\*甲", {"image_path": "img1/A!B.jpg"}
        )
        self.assertEqual(event.sent[0].chain[0].text, "Alice_Isla[星]，老婆是角色*甲")

    async def test_normal_platform_keeps_one_text_and_image_chain(self):
        event = Event()
        self.plugin.config["enable_images"] = True
        self.plugin.images.get_image = AsyncMock(return_value=self.data / "image.png")
        await self.plugin._send(event, "result", {"image_path": "img1/A!B.jpg"})
        self.assertEqual(len(event.sent), 1)
        self.assertIsInstance(event.sent[0].chain[0], Plain)
        self.assertIsInstance(event.sent[0].chain[1], Image)

    async def test_image_failure_keeps_business_text(self):
        event = Event(platform="qq_official")
        self.plugin.config["enable_images"] = True
        self.plugin.images.get_image = AsyncMock(return_value=None)
        await self.plugin._send(event, "original result", {"image_path": "img1/A!B.jpg"})
        self.assertEqual(len(event.sent), 1)
        self.assertIn("original result", event.sent[0].chain[0].text)

    async def test_force_refresh_requires_admin(self):
        self.plugin.catalog.refresh = AsyncMock()
        event = Event("jrlp force-download")
        await self.plugin._dispatch(event)
        self.plugin.catalog.refresh.assert_not_awaited()
        self.assertIn("管理员", event.sent[0].chain[0].text)


if __name__ == "__main__":
    unittest.main()
