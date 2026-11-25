"""Session isolation and reset behavior against the real SQLite ledger."""

from __future__ import annotations

import asyncio
import copy
import sqlite3
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock, patch

from test_main import Context, Event, Plugin, module

NOW = datetime(2026, 11, 26, 12, tzinfo=UTC).timestamp()
A = {
    "id": "作品甲!角色甲",
    "name": "角色甲",
    "source": "作品甲",
    "source_kind": "work",
    "image_path": "img1/作品甲!角色甲.jpg",
}
B = {
    "id": "作品乙!角色乙",
    "name": "角色乙",
    "source": "作品乙",
    "source_kind": "work",
    "image_path": "img1/作品乙!角色乙.jpg",
}


def current_event(text="jrlp", *, session="chat-a", user="user-a", admin=False):
    event = Event(text, group="group-a", user=user)
    event.unified_msg_origin = f"instance-a:GroupMessage:{session}"
    event.is_admin = lambda: admin
    return event


def plain_text(event):
    return "\n".join(
        component.text
        for result in event.sent
        for component in result.chain
        if hasattr(component, "text")
    )


class SessionResetTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="mywife-session-reset-")
        self.data = Path(self.temporary.name)
        self.path_patch = patch.object(module.StarTools, "get_data_dir", return_value=self.data)
        self.clock_patch = patch.object(module.time, "time", return_value=NOW)
        self.path_patch.start()
        self.clock_patch.start()
        self.plugin = Plugin(Context(), {"reply_mode": "template", "enable_images": False})
        self.plugin._choose = lambda previous: copy.deepcopy(B if previous == A["id"] else A)
        self.day = datetime.fromtimestamp(NOW, self.plugin.timezone).date().isoformat()

    async def asyncTearDown(self):
        await self.plugin.terminate()
        self.clock_patch.stop()
        self.path_patch.stop()
        self.temporary.cleanup()

    async def state(self, event):
        return await self.plugin.store.get_player(self.plugin._state_key(event), self.day, NOW)

    async def history(self, event, action=""):
        return (await self.plugin.store.history(self.plugin._state_key(event), action=action))[
            "items"
        ]

    def seed(self, event, *, married=False, count=3):
        state = {
            "date": self.day,
            "wife": copy.deepcopy(B),
            "redraw_count": count,
            "marriage": None,
            "history": [
                {"date": self.day, "action": "draw", "character": copy.deepcopy(A)},
                {"date": self.day, "action": "redraw", "character": copy.deepcopy(B)},
            ],
        }
        if married:
            state["marriage"] = {
                "wife": copy.deepcopy(B),
                "started_at": NOW - 60,
                "expires_at": NOW + 7 * 86400,
                "duration_days": 7,
            }
        self.plugin.legacy_kv[self.plugin._state_key(event)] = state
        return copy.deepcopy(state)

    async def test_sessions_and_shared_umo_senders_have_separate_ledgers(self):
        first = current_event()
        second = current_event(session="chat-b")
        other_person = current_event(user="user-b")
        await self.plugin._business(first, "draw")
        await self.plugin._business(first, "redraw")
        await self.plugin._business(first, "marry")
        await self.plugin._business(second, "draw")
        await self.plugin._business(other_person, "draw")
        first_state = await self.state(first)
        self.assertEqual(first_state["wife"], B)
        self.assertEqual(first_state["redraw_count"], 1)
        self.assertEqual(first_state["marriage"]["wife"], B)
        self.assertEqual(
            [i["action"] for i in await self.history(first)], ["marry", "redraw", "draw"]
        )
        for event in (second, other_person):
            independent = await self.state(event)
            self.assertEqual(independent["wife"], A)
            self.assertEqual(independent["redraw_count"], 0)
            self.assertIsNone(independent["marriage"])
            text, _ = await self.plugin._history_reply(event)
            self.assertIn(A["name"], text)
            self.assertNotIn(B["name"], text)

    async def test_legacy_keys_match_v1_and_read_the_imported_role(self):
        cases = (
            (
                "group",
                "aiocqhttp",
                "bot-a",
                "user:235aef6fcf4614da1c6c962af9f2d33744e0271f3dc9c832d1860198902af408",
            ),
            (
                "user",
                "aiocqhttp",
                "bot-a",
                "user:afadb889a33a4c1854ec0ac2b9cc26e4289a4c01cc16923c611d06456cead085",
            ),
            (
                "group",
                "qq_official",
                "unknown_selfid",
                "user:e94cc7a3cd36504c47dae9ade37bf8de62f11fe8046bcf3f9b2dd75f7fd4faf3",
            ),
            (
                "user",
                "qq_official",
                "appid",
                "user:7a9dae3b7eeecf4c2cf86978830eede2452b2572527af5e390cbc7fec3c2ac21",
            ),
        )
        for scope, platform, bot, expected in cases:
            self.plugin.config["state_scope"] = scope
            event = Event(platform=platform, bot=bot)
            self.assertEqual(self.plugin._state_key(event), expected)
            self.seed(event)
        self.plugin._choose = Mock(side_effect=AssertionError("Must reuse the migrated role"))
        for scope, platform, bot, _ in cases:
            self.plugin.config["state_scope"] = scope
            event = Event(platform=platform, bot=bot)
            text, character, _ = await self.plugin._business(event, "draw")
            self.assertIn(B["name"], text)
            self.assertEqual(character, B)
            self.assertEqual((await self.state(event))["redraw_count"], 3)
            self.assertEqual(len(await self.history(event)), 2)
        self.plugin._choose.assert_not_called()

    async def test_denial_does_not_initialize_or_change_data(self):
        event = current_event("重置次数")
        self.seed(event)
        with patch.object(self.plugin.store, "reset_count") as reset:
            await self.plugin._dispatch(event)
        reset.assert_not_awaited()
        self.assertIn("管理员", plain_text(event))

    async def test_reset_preserves_history_and_marriage_and_only_touches_current_player(self):
        event = current_event("jrlp 重置次数", admin=True)
        other = current_event(session="chat-b")
        before = self.seed(event, married=True)
        self.seed(other)
        await self.plugin.store.initialize()
        history = await self.history(event)
        await self.plugin.jrlp(event)
        await self.plugin.on_message(event)
        self.assertEqual(len(event.sent), 1)
        state = await self.state(event)
        self.assertEqual(state["redraw_count"], 0)
        self.assertEqual(state["wife"], before["wife"])
        self.assertEqual(state["marriage"], before["marriage"])
        self.assertEqual(await self.history(event), history)
        self.assertEqual((await self.state(other))["redraw_count"], 3)
        response, _, _ = await self.plugin._business(event, "redraw")
        self.assertIn("不能", response)
        self.assertEqual(await self.history(event), history)

    async def test_reset_save_failure_never_reports_success(self):
        event = current_event("jrlp 重置次数", admin=True)
        self.seed(event)
        await self.plugin.store.initialize()
        with patch.object(self.plugin.store, "_save_player", side_effect=sqlite3.OperationalError):
            await self.plugin._dispatch(event)
        self.assertEqual((await self.state(event))["redraw_count"], 3)
        self.assertIn("未能完成", plain_text(event))

    async def test_concurrent_web_reset_and_command_redraw_preserve_the_new_draw(self):
        reset_event = current_event(admin=True)
        self.seed(reset_event, count=1)
        await self.plugin.store.initialize()
        await asyncio.gather(
            self.plugin.store.admin_reset_count(self.plugin._state_key(reset_event), self.day, NOW),
            self.plugin._business(current_event("hlp"), "redraw"),
        )
        state = await self.state(reset_event)
        self.assertIn(state["redraw_count"], (0, 1))
        self.assertEqual(state["wife"], A)
        self.assertEqual(
            [i["action"] for i in await self.history(reset_event)],
            [
                "redraw",
                "redraw",
                "draw",
            ],
        )

    async def test_history_pages_preserve_older_draws_but_skip_relationships(self):
        first = current_event()
        self.plugin.config["daily_hlp_limit"] = 40
        await self.plugin._business(first, "draw")
        for _ in range(21):
            await self.plugin._business(first, "redraw")
        await self.plugin._business(first, "marry")
        message = current_event("jrlp 历史 3")
        await self.plugin._dispatch(message)
        text = plain_text(message)
        self.assertIn("第 3/3 页（共 22 条）", text)
        self.assertEqual(text.count("• "), 2)
        self.assertIn("抽取", text)
        invalid = current_event("jrlp 历史 -1")
        await self.plugin._dispatch(invalid)
        self.assertIn("正整数", plain_text(invalid))
