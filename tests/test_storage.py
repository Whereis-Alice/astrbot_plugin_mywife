"""The ledger preserves history and player state under migration and concurrent writes."""

from __future__ import annotations

import copy
import tempfile
from importlib import import_module
from pathlib import Path
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from test_main import PACKAGE

WifeStore = import_module(f"{PACKAGE}.storage").WifeStore
A = {"id": "A!爱丽丝", "name": "爱丽丝", "source": "A", "source_kind": "work"}
B = {"id": "B!艾拉", "name": "艾拉", "source": "B", "source_kind": "work"}
DAY = "2026-11-26"
NOW = 1_795_689_600.0
KEY = "user:session:" + "a" * 64
OTHER_KEY = "user:session:" + "b" * 64
IDENTITY = {
    "platform": "qq_official",
    "platform_id": "bot",
    "scope": "session",
    "session_id": "chat-1",
    "user_id": "user-1",
    "display_name": "Straße",
}


def choose(previous):
    return copy.deepcopy(B if previous == A["id"] else A)


class StorageTests(IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "ledger.sqlite3"
        self.store = WifeStore(self.path)

    async def apply(self, action, *, key=KEY, identity=IDENTITY, now=NOW, day=DAY):
        return await self.store.change(key, identity, day, now, action, 100, 7, choose)

    async def test_old_records_import_once_and_keep_known_history_without_inventing_timestamps(
        self,
    ):
        legacy = {
            KEY: {
                "date": DAY,
                "wife": A,
                "redraw_count": 5,
                "marriage": {
                    "wife": A,
                    "started_at": NOW - 86400,
                    "expires_at": NOW + 86400,
                    "duration_days": 2,
                },
                "history": [
                    {"date": "2026-11-25", "action": "draw", "character": A},
                    {"date": DAY, "action": "redraw", "character": B},
                    None,
                ],
            },
            "other-plugin-config": {"history": [{"character": A}]},
            "user:" + "c" * 64: {"date": "2026-11-20", "redraw_count": 1, "wife": B},
        }
        saved = copy.deepcopy(legacy)
        scan = AsyncMock(return_value=legacy)
        self.store = WifeStore(self.path, scan)

        outcome, character, _ = await self.apply("draw")
        self.assertEqual((outcome, character), ("married", A))
        player = await self.store.get_player(KEY, DAY, NOW)
        self.assertEqual(player["redraw_count"], 5)
        self.assertEqual(player["scope"], "session")
        self.assertEqual(player["display_name"], "Straße")
        history = (await self.store.history(KEY))["items"]
        self.assertEqual([item["character"] for item in history], [B, A])
        self.assertEqual([item["at"] for item in history], [None, None])
        self.assertEqual(
            await self.store.stats(DAY, NOW),
            {
                "players": 2,
                "today": 1,
                "marriages": 1,
                "draws": 2,
                "today_draws": 1,
            },
        )
        self.assertEqual(legacy, saved)

        reopened = WifeStore(self.path, scan)
        self.assertEqual((await reopened.history(KEY))["items"], history)
        scan.assert_awaited_once()

    async def test_legacy_scan_failure_can_retry_without_marking_completed(self):
        legacy = {KEY: {"date": DAY, "wife": A, "redraw_count": 2}}
        scan = AsyncMock(side_effect=[OSError("temporary failure"), legacy])
        self.store = WifeStore(self.path, scan)
        with self.assertRaises(OSError):
            await self.store.initialize()
        self.assertEqual((await self.store.get_player(KEY, DAY, NOW))["wife"], A)
        self.assertEqual(scan.await_count, 2)

    async def test_more_than_twenty_draws_remain_queryable_after_reopen(self):
        for index in range(31):
            await self.apply("redraw" if index else "draw", now=NOW + index)
        reopened = WifeStore(self.path)
        first = await reopened.history(KEY, action="draws", limit=20)
        second = await reopened.history(KEY, action="draws", limit=20, offset=20)
        self.assertEqual((len(first["items"]), len(second["items"])), (20, 11))
        self.assertEqual(first["total"], 31)
        self.assertEqual(first["items"][0]["at"], NOW + 30)
        self.assertEqual(second["items"][-1]["at"], NOW)
        self.assertEqual(second["items"][-1]["action"], "draw")

    async def test_marriage_divorce_and_scope_filters_remain_distinct(self):
        await self.apply("draw")
        await self.apply("marry", now=NOW + 1)
        self.assertEqual((await self.apply("redraw", now=NOW + 2))[0], "blocked")
        await self.apply("divorce", now=NOW + 3)
        await self.apply("draw", key=OTHER_KEY, identity={**IDENTITY, "display_name": "Bob"})
        await self.apply("redraw", identity={**IDENTITY, "session_id": "chat-2"}, now=NOW + 4)
        history = await self.store.history(KEY)
        self.assertEqual(
            [item["action"] for item in history["items"]], ["redraw", "divorce", "marry", "draw"]
        )
        self.assertEqual(
            [item["session_id"] for item in history["items"]],
            ["chat-2", "chat-1", "chat-1", "chat-1"],
        )
        self.assertEqual((await self.store.history(KEY, action="draws"))["total"], 2)
        self.assertEqual((await self.store.history(query="艾拉"))["total"], 1)
        self.assertEqual((await self.store.players(day=DAY, now=NOW, query="STRASSE"))["total"], 1)
        self.assertEqual((await self.store.history(query="alice' OR 1=1"))["total"], 0)

    async def test_admin_reset_preserves_roles_relationship_and_history(self):
        await self.apply("draw")
        await self.apply("redraw", now=NOW + 1)
        await self.apply("marry", now=NOW + 2)
        before = await self.store.get_player(KEY, DAY, NOW + 2)
        history = await self.store.history(KEY)
        after = await self.store.admin_reset_count(KEY, DAY, NOW + 3)
        self.assertEqual(after["redraw_count"], 0)
        self.assertEqual(after["wife"], before["wife"])
        self.assertEqual(after["marriage"], before["marriage"])
        self.assertEqual(await self.store.history(KEY), history)
        self.assertIsNone(await self.store.admin_reset_count(OTHER_KEY, DAY, NOW + 3))
        self.assertEqual((await self.store.stats(DAY, NOW + 3))["players"], 1)

    async def test_failed_history_insert_rolls_back_player_state(self):
        await self.apply("draw")
        initial = await self.store.get_player(KEY, DAY, NOW)
        with patch.object(self.store, "_insert_history", side_effect=OSError("disk error")):
            with self.assertRaises(OSError):
                await self.apply("redraw", now=NOW + 1)
        self.assertEqual(await self.store.get_player(KEY, DAY, NOW + 1), initial)
        self.assertEqual((await self.store.history(KEY))["total"], 1)
