"""Integration tests use actual AstrBot 4.29.4 message classes and decorators."""

from __future__ import annotations

import asyncio
import importlib.util
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import At, Image, Plain
from PIL import Image as PILImage

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
        self.legacy_kv = {}

    async def _legacy_records(self):
        return self.legacy_kv


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
        self.assertEqual((await self.plugin.store.history())["total"], 1)

    async def test_concurrent_draws_have_single_persistent_result(self):
        events = [Event() for _ in range(12)]
        await asyncio.gather(*(self.plugin._dispatch(event) for event in events))
        day, now = self.plugin._local_now()
        state = await self.plugin.store.get_player(self.plugin._state_key(events[0]), day, now)
        self.assertEqual((await self.plugin.store.history())["total"], 1)
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
        with patch.object(
            self.plugin.store, "_insert_history", side_effect=sqlite3.OperationalError
        ):
            await self.plugin._dispatch(event)
        self.assertEqual((await self.plugin.store.history())["total"], 0)
        day, now = self.plugin._local_now()
        self.assertIsNone(
            await self.plugin.store.get_player(self.plugin._state_key(event), day, now)
        )
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

    def make_portrait(self):
        path = self.data / "image.png"
        with PILImage.new("RGB", (360, 720), (120, 100, 70)) as original:
            original.save(path)
        self.plugin.config["enable_images"] = True
        self.plugin.images.get_image = AsyncMock(return_value=path)
        return path

    async def test_qq_markdown_text_and_image_are_one_plain_markdown_payload(self):
        event = Event(platform="qq_official")
        original = self.make_portrait()
        original_bytes = original.read_bytes()
        self.plugin.qq_images.get_image_url = AsyncMock(
            return_value="qqbot-media://image/v1/mywife-native-image"
        )
        await self.plugin._send(event, "Alice 的角色", {"image_path": "img1/A!B.jpg"})
        self.assertEqual(len(event.sent), 1)
        result = event.sent[0]
        self.assertTrue(result.use_markdown_)
        self.assertEqual(len(result.chain), 1)
        self.assertIsInstance(result.chain[0], Plain)
        self.assertIn("![角色图片 #100px #200px]", result.chain[0].text)
        self.assertIn("Alice 的角色", result.chain[0].text)
        self.assertIn("qqbot-media://image/v1/", result.chain[0].text)
        self.assertNotIn("raw.githubusercontent.com", result.chain[0].text)
        self.plugin.qq_images.get_image_url.assert_awaited_once_with(event, original)
        self.assertEqual(original.read_bytes(), original_bytes)
        self.assertFalse((self.data / "qq_previews").exists())

    async def test_failed_native_upload_keeps_markdown_buttons_and_original_link(self):
        event = Event(platform="qq_official")
        original = self.make_portrait()
        actions = (("换老婆", "hlp"),)
        self.plugin.qq_images.get_image_url = AsyncMock(return_value=None)
        with patch.object(module, "send_qq_markdown", return_value=True) as send:
            await self.plugin._send(event, "角色结果", {"image_path": "img1/A!B.jpg"}, actions)
        self.plugin.qq_images.get_image_url.assert_awaited_once_with(event, original)
        send.assert_awaited_once()
        _, body, buttons = send.await_args.args
        self.assertNotIn("![", body)
        self.assertIn(
            "[查看原图](https://fastly.jsdelivr.net/gh/monbed/wife@main/img1/A%21B.jpg)", body
        )
        self.assertEqual(buttons, actions)
        self.assertEqual(event.sent, [])
        self.assertFalse((self.data / "qq_previews").exists())

    async def test_rejected_native_image_can_keep_markdown_buttons_and_original_link(self):
        event = Event(platform="qq_official")
        self.make_portrait()
        self.plugin.qq_images.get_image_url = AsyncMock(
            return_value="qqbot-media://image/v1/mywife-native-image"
        )
        actions = (("换老婆", "hlp"),)
        with patch.object(module, "send_qq_markdown", side_effect=[False, True]) as send:
            await self.plugin._send(event, "角色结果", {"image_path": "img1/A!B.jpg"}, actions)
        full, text_only = (call.args[1] for call in send.await_args_list)
        self.assertIn("![角色图片 #100px #200px](qqbot-media://", full)
        self.assertNotIn("![", text_only)
        self.assertIn("[查看原图](https://fastly.jsdelivr.net/", text_only)
        self.assertEqual(send.await_args.args[2], actions)
        self.assertEqual(event.sent, [])

    async def test_failed_markdown_falls_back_to_one_normal_text_image_chain(self):
        event = Event(platform="qq_official")
        original = self.make_portrait()
        self.plugin.qq_images.get_image_url = AsyncMock(return_value=None)
        with patch.object(module, "send_qq_markdown", return_value=False):
            await self.plugin._send(event, "角色结果", {"image_path": "img1/A!B.jpg"})
        self.assertEqual(len(event.sent), 1)
        self.assertFalse(event.sent[0].use_markdown_)
        self.assertIsInstance(event.sent[0].chain[0], Plain)
        self.assertIsInstance(event.sent[0].chain[1], Image)
        self.assertNotIn("http", event.sent[0].chain[0].text)
        self.plugin.qq_images.get_image_url.assert_awaited_once_with(event, original)
        fallback = Path(event.sent[0].chain[1].path)
        self.assertNotEqual(fallback, original)
        with PILImage.open(fallback) as preview:
            self.assertEqual(preview.size, (100, 200))

    async def test_native_failure_does_not_show_markdown_escape_slashes(self):
        event = Event(platform="qq_official")
        self.make_portrait()
        self.plugin.qq_images.get_image_url = AsyncMock(return_value=None)
        with patch.object(module, "send_qq_markdown", return_value=False):
            await self.plugin._send(
                event, r"Alice\_Isla\[星\]，老婆是角色\*甲", {"image_path": "img1/A!B.jpg"}
            )
        self.assertEqual(event.sent[0].chain[0].text, "Alice_Isla[星]，老婆是角色*甲")

    async def test_normal_platform_keeps_one_text_and_image_chain(self):
        event = Event()
        original = self.make_portrait()
        await self.plugin._send(event, "result", {"image_path": "img1/A!B.jpg"})
        self.assertEqual(len(event.sent), 1)
        self.assertIsInstance(event.sent[0].chain[0], Plain)
        self.assertIsInstance(event.sent[0].chain[1], Image)
        self.assertEqual(Path(event.sent[0].chain[1].path), original)

    async def test_qq_preview_failure_keeps_text_and_never_sends_tall_original(self):
        event = Event(platform="qq_official")
        original = self.make_portrait()
        self.plugin.qq_images.get_image_url = AsyncMock(return_value=None)
        with (
            patch.object(module, "send_qq_markdown", return_value=False),
            patch.object(module, "make_qq_preview", side_effect=OSError),
        ):
            await self.plugin._send(event, "Alice", {"image_path": "img1/A!B.jpg"})
        self.plugin.qq_images.get_image_url.assert_awaited_once_with(event, original)
        self.assertEqual(len(event.sent), 1)
        self.assertEqual(len(event.sent[0].chain), 1)
        body = event.sent[0].chain[0]
        self.assertIsInstance(body, Plain)
        self.assertIn("Alice", body.text)
        self.assertNotIn("![", body.text)

    async def test_image_failure_keeps_business_text(self):
        event = Event(platform="qq_official")
        self.plugin.config["enable_images"] = True
        self.plugin.images.get_image = AsyncMock(return_value=None)
        await self.plugin._send(event, "original result", {"image_path": "img1/A!B.jpg"})
        self.assertEqual(len(event.sent), 1)
        self.assertIn("original result", event.sent[0].chain[0].text)

    async def test_official_markdown_rejection_falls_back_to_plain_picture_and_commands(self):
        event = Event(platform="qq_official")
        original = self.make_portrait()
        self.plugin.qq_images.get_image_url = AsyncMock(return_value="qqbot-media://image/v1/image")
        with patch.object(module, "send_qq_markdown", return_value=False) as send:
            await self.plugin._send(
                event, "Alice", {"image_path": "img1/A!B.jpg"}, (("历史", "jrlp 历史"),)
            )
        self.assertEqual(send.await_count, 2)
        native, text_only = (call.args[1] for call in send.await_args_list)
        self.assertIn("qqbot-media://", native)
        self.assertNotIn("![", text_only)
        result = event.sent[0]
        self.assertFalse(result.use_markdown_)
        self.assertEqual(len(result.chain), 2)
        self.assertIn("jrlp 历史", result.chain[0].text)
        self.plugin.qq_images.get_image_url.assert_awaited_once_with(event, original)
        self.assertNotEqual(Path(result.chain[1].path), original)
        with PILImage.open(result.chain[1].path) as preview:
            self.assertEqual(preview.size, (100, 200))

    async def test_official_button_setting_and_exhausted_redraws(self):
        event = Event(platform="qq_official")
        self.plugin.config["daily_hlp_limit"] = 1
        with patch.object(module, "send_qq_markdown", return_value=True) as send:
            await self.plugin._dispatch(event)
            self.assertEqual(send.await_args.args[2][1], ("换老婆", "hlp"))
            await self.plugin._dispatch(Event("hlp", platform="qq_official"))
            await self.plugin._dispatch(Event("jrlp", platform="qq_official"))
            self.assertEqual(
                [label for label, _ in send.await_args.args[2]],
                [
                    "结婚",
                    "换老婆",
                    "历史",
                ],
            )
            await self.plugin._dispatch(Event("hlp", platform="qq_official"))
            self.assertIn("用完", send.await_args.args[1])
            self.plugin.config["qq_official_buttons"] = False
            await self.plugin._dispatch(Event("jrlp", platform="qq_official"))
            self.assertEqual(send.await_args.args[2], ())

    async def test_force_refresh_requires_admin(self):
        self.plugin.catalog.refresh = AsyncMock()
        event = Event("jrlp force-download")
        await self.plugin._dispatch(event)
        self.plugin.catalog.refresh.assert_not_awaited()
        self.assertIn("管理员", event.sent[0].chain[0].text)


if __name__ == "__main__":
    unittest.main()
