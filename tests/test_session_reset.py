"""Session-scoped state and administrator-only counter reset integration tests."""

from __future__ import annotations

import asyncio
import copy
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

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


def current_event(
    text="jrlp", *, session="chat-a", user="user-a", platform="aiocqhttp", bot="bot-a", admin=False
):
    event = Event(text, group="group-a", user=user, platform=platform, bot=bot)
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
        # Omit state_scope so the production default is part of the contract.
        self.plugin = Plugin(Context(), {"reply_mode": "template", "enable_images": False})
        self.plugin._choose = lambda previous: copy.deepcopy(B if previous == A["id"] else A)
        self.day = datetime.fromtimestamp(NOW, self.plugin.timezone).date().isoformat()

    async def asyncTearDown(self):
        await self.plugin.terminate()
        self.clock_patch.stop()
        self.path_patch.stop()
        self.temporary.cleanup()

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
        key = self.plugin._state_key(event)
        self.plugin.kv[key] = copy.deepcopy(state)
        return key, copy.deepcopy(state)

    async def test_default_scope_is_session_and_different_umos_have_different_keys(self):
        first, second = current_event(session="chat-a"), current_event(session="chat-b")
        self.assertNotEqual(self.plugin._state_key(first), self.plugin._state_key(second))

    async def test_same_user_and_group_have_independent_wife_counter_marriage_and_history(self):
        first, second = current_event(session="chat-a"), current_event(session="chat-b")
        await self.plugin._business(first, "draw")
        await self.plugin._business(first, "redraw")
        await self.plugin._business(first, "marry")
        await self.plugin._business(second, "draw")
        first_state = self.plugin.kv[self.plugin._state_key(first)]
        second_state = self.plugin.kv[self.plugin._state_key(second)]
        self.assertEqual(first_state["wife"], B)
        self.assertEqual(first_state["redraw_count"], 1)
        self.assertEqual(first_state["marriage"]["wife"], B)
        self.assertEqual(len(first_state["history"]), 2)
        self.assertEqual(second_state["wife"], A)
        self.assertEqual(second_state["redraw_count"], 0)
        self.assertIsNone(second_state["marriage"])
        self.assertEqual(len(second_state["history"]), 1)
        saved_first = copy.deepcopy(first_state)
        history = await self.plugin._history_text(second)
        self.assertIn(A["name"], history)
        self.assertNotIn(B["name"], history)
        self.assertEqual(self.plugin.kv[self.plugin._state_key(first)], saved_first)

    async def test_shared_group_umo_without_unique_session_still_separates_users(self):
        first = current_event(session="group-a", user="user-a")
        second = current_event(session="group-a", user="user-b")
        self.assertEqual(first.unified_msg_origin, second.unified_msg_origin)
        await self.plugin._business(first, "draw")
        await self.plugin._business(first, "redraw")
        await self.plugin._business(second, "draw")
        first_state = self.plugin.kv[self.plugin._state_key(first)]
        second_state = self.plugin.kv[self.plugin._state_key(second)]
        self.assertEqual(first_state["wife"], B)
        self.assertEqual(first_state["redraw_count"], 1)
        self.assertEqual(second_state["wife"], A)
        self.assertEqual(second_state["redraw_count"], 0)
        self.assertEqual((len(first_state["history"]), len(second_state["history"])), (2, 1))

    async def test_official_session_uses_stable_adapter_bot_identity(self):
        first = current_event(platform="qq_official", bot="unknown_selfid")
        second = current_event(platform="qq_official", bot="appid")
        self.assertEqual(self.plugin._state_key(first), self.plugin._state_key(second))
        second.get_platform_id = lambda: "instance-b"
        self.assertNotEqual(self.plugin._state_key(first), self.plugin._state_key(second))

    async def test_legacy_group_and_user_keys_still_read_saved_v1_records(self):
        # Golden hashes from v1.0.0: scope changes must not rename existing keys.
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
            with self.subTest(scope=scope, platform=platform):
                self.plugin.config["state_scope"] = scope
                event = current_event(platform=platform, bot=bot)
                self.assertEqual(self.plugin._state_key(event), expected)
                _, state = self.seed(event)
                self.plugin._choose = Mock(
                    side_effect=AssertionError("Existing daily wife must be reused")
                )
                text, character = await self.plugin._business(event, "draw")
                self.assertIn(B["name"], text)
                self.assertEqual(character, B)
                self.assertEqual(self.plugin.kv[expected], state)

    async def test_non_admin_reset_denied_without_changing_any_saved_record(self):
        event = current_event("重置次数")
        self.seed(event)
        self.seed(current_event(session="chat-b", user="user-b"))
        saved = copy.deepcopy(self.plugin.kv)
        self.plugin.put_kv_data = AsyncMock(wraps=self.plugin.put_kv_data)
        await self.plugin._dispatch(event)
        self.assertEqual(self.plugin.kv, saved)
        self.plugin.put_kv_data.assert_not_awaited()
        self.assertIn("管理员", plain_text(event))

    async def test_admin_reset_only_changes_current_counter_preserves_other_state(self):
        event = current_event("jrlp 重置次数", admin=True)
        key, before = self.seed(event)
        other_session, saved_session = self.seed(current_event(session="chat-b"))
        other_user, saved_user = self.seed(current_event(user="user-b"))
        text = await self.plugin._reset_count_text(event)
        expected = copy.deepcopy(before)
        expected["redraw_count"] = 0
        self.assertEqual(self.plugin.kv[key], expected)
        self.assertEqual(self.plugin.kv[other_session], saved_session)
        self.assertEqual(self.plugin.kv[other_user], saved_user)
        self.assertIn("重置", text)

    async def test_admin_reset_is_limited_to_current_legacy_scope(self):
        event = current_event(admin=True)
        for scope in ("group", "user"):
            with self.subTest(scope=scope):
                self.plugin.config["state_scope"] = scope
                key, before = self.seed(event)
                await self.plugin._reset_count_text(event)
                expected = copy.deepcopy(before)
                expected["redraw_count"] = 0
                self.assertEqual(self.plugin.kv[key], expected)

    async def test_married_reset_preserves_marriage_and_redraw_is_still_blocked(self):
        event = current_event(admin=True)
        key, before = self.seed(event, married=True)
        text = await self.plugin._reset_count_text(event)
        self.assertEqual(self.plugin.kv[key]["marriage"], before["marriage"])
        self.assertEqual(self.plugin.kv[key]["wife"], before["wife"])
        self.assertEqual(self.plugin.kv[key]["history"], before["history"])
        self.assertIn("婚", text)
        self.assertIn("不能", text)
        self.assertNotIn("还可以更换", text)
        response, _ = await self.plugin._business(event, "redraw")
        self.assertIn("不能", response)
        self.assertEqual(self.plugin.kv[key]["redraw_count"], 0)
        self.assertEqual(self.plugin.kv[key]["history"], before["history"])

    async def test_reset_save_failure_never_reports_success_or_mutates_kv(self):
        event = current_event("jrlp 重置次数", admin=True)
        self.seed(event)
        saved = copy.deepcopy(self.plugin.kv)
        self.plugin.fail_save = True
        await self.plugin._dispatch(event)
        self.assertEqual(self.plugin.kv, saved)
        self.assertIn("未能完成", plain_text(event))
        self.assertNotIn("已重置", plain_text(event))

    async def test_command_and_no_prefix_listener_reset_one_event_only_once(self):
        event = current_event("jrlp 重置次数", admin=True)
        key, before = self.seed(event)
        self.plugin.put_kv_data = AsyncMock(wraps=self.plugin.put_kv_data)
        await self.plugin.jrlp(event)
        await self.plugin.on_message(event)
        self.assertEqual(self.plugin.put_kv_data.await_count, 1)
        self.assertEqual(len(event.sent), 1)
        self.assertEqual(self.plugin.kv[key]["redraw_count"], 0)
        self.assertEqual(self.plugin.kv[key]["history"], before["history"])

    async def test_reset_aliases_are_exact_and_do_not_intercept_normal_sentences(self):
        for command in (
            "jrlp 重置次数",
            "今日老婆 重置次数",
            "重置次数",
            "重置换",
            "/重置次数",
            "jrlp reset",
        ):
            with self.subTest(command=command):
                self.assertEqual(module.parse_command(command), ("reset_count", ""))
        for message in ("我想重置次数", "重置次数了吗", "重置换个老婆"):
            self.assertIsNone(module.parse_command(message))

    async def test_reset_and_redraw_share_the_same_lock_without_lost_update(self):
        event = current_event(admin=True)
        key, before = self.seed(event)
        save_started, allow_save = asyncio.Event(), asyncio.Event()
        original_save = self.plugin.put_kv_data
        blocked = False

        async def save_with_barrier(record_key, state):
            nonlocal blocked
            if state["redraw_count"] == 0 and not blocked:
                blocked = True
                save_started.set()
                await allow_save.wait()
            await original_save(record_key, state)

        self.plugin.put_kv_data = save_with_barrier
        self.plugin.get_kv_data = AsyncMock(wraps=self.plugin.get_kv_data)
        resetting = asyncio.create_task(self.plugin._reset_count_text(event))
        redrawing = None
        try:
            await asyncio.wait_for(save_started.wait(), timeout=2)
            redrawing = asyncio.create_task(self.plugin._business(current_event("hlp"), "redraw"))
            await asyncio.sleep(0)
            self.assertFalse(redrawing.done())
            self.assertEqual(self.plugin.get_kv_data.await_count, 1)
            allow_save.set()
            await asyncio.gather(resetting, redrawing)
        finally:
            allow_save.set()
            await asyncio.gather(
                *(task for task in (resetting, redrawing) if task is not None),
                return_exceptions=True,
            )
        state = self.plugin.kv[key]
        self.assertEqual(state["redraw_count"], 1)
        self.assertEqual(state["wife"], A)
        self.assertEqual(state["history"][:-1], before["history"])
        self.assertEqual(state["history"][-1]["character"], A)
        self.assertEqual(state["history"][-1]["action"], "redraw")

    async def test_reset_without_daily_wife_does_not_draw_or_create_history(self):
        event = current_event(admin=True)
        self.plugin._choose = Mock(side_effect=AssertionError("Reset must not draw a character"))
        await self.plugin._reset_count_text(event)
        state = self.plugin.kv[self.plugin._state_key(event)]
        self.assertIsNone(state["wife"])
        self.assertEqual(state["redraw_count"], 0)
        self.assertEqual(state["history"], [])
        self.plugin._choose.assert_not_called()


if __name__ == "__main__":
    unittest.main()
