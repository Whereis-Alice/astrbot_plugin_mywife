"""Group filters preserve each draw's originating conversation across upgrades."""

import hashlib
import json
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch

from test_storage import DAY, IDENTITY, KEY, NOW, OTHER_KEY, A, WifeStore, choose


class GroupStorageTests(IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "ledger.sqlite3"
        self.store = WifeStore(self.path)

    async def test_shared_player_history_and_aliases_are_group_specific(self):
        alice = {**IDENTITY, "scope": "user", "session_id": ""}
        first = {**alice, "group_id": "group1", "group_name": "秋叶原"}
        second = {**alice, "group_id": "group2", "group_name": "咖啡馆"}
        another_bot = {**second, "platform_id": "other", "group_name": "音乐教室"}
        await self.store.change(KEY, first, DAY, NOW, "draw", 100, 7, choose)
        await self.store.change(KEY, second, DAY, NOW + 1, "redraw", 100, 7, choose)
        await self.store.change(OTHER_KEY, another_bot, DAY, NOW + 1, "draw", 100, 7, choose)
        groups = await self.store.groups(query="馆")
        self.assertEqual(groups["total"], 1)
        cafe = groups["items"][0]
        self.assertEqual((cafe["player_count"], cafe["history_count"]), (1, 1))
        self.assertEqual((await self.store.groups(query="group2"))["total"], 2)

        first_key = (await self.store.groups(query="秋叶原"))["items"][0]["group_key"]
        players = await self.store.players(day=DAY, now=NOW, group_key=first_key)
        self.assertEqual((players["total"], players["items"][0]["group_name"]), (1, "秋叶原"))
        history = await self.store.history(KEY, group_key=first_key, action="draws")
        self.assertEqual((history["total"], history["items"][0]["action"]), (1, "draw"))
        self.assertEqual((await self.store.history(query="秋叶原"))["total"], 1)

        alias = await self.store.set_group_alias(first_key, "  动画群  ")
        self.assertEqual((alias["group_name"], alias["reported_group_name"]), ("动画群", "秋叶原"))
        await self.store.change(
            KEY, {**first, "group_name": ""}, DAY, NOW + 2, "redraw", 100, 7, choose
        )
        reopened = WifeStore(self.path)
        aliased_history = await reopened.history(query="动画群")
        self.assertEqual(aliased_history["total"], 2)
        self.assertEqual(aliased_history["items"][0]["group_alias"], "动画群")
        aliased_players = await reopened.players(day=DAY, now=NOW, group_key=first_key)
        self.assertEqual(aliased_players["items"][0]["reported_group_name"], "秋叶原")
        restored = await reopened.set_group_alias(first_key, "")
        self.assertEqual(restored["group_name"], "秋叶原")
        self.assertIsNone(await reopened.set_group_alias("unknown", "未知群别名"))

    async def test_upgrading_uses_historical_identity_for_each_record(self):
        # Preserve the table shape from before group columns were added.
        with patch.object(WifeStore, "_migrate_groups"):
            await self.store.initialize()
        older_identities = (
            {**IDENTITY, "scope": "user", "session_id": ""},
            {**IDENTITY, "session_id": "bot:GroupMessage:ABC123"},
            {**IDENTITY, "scope": "group", "session_id": "private"},
            {
                **IDENTITY,
                "platform": "aiocqhttp",
                "session_id": "bot:GroupMessage:456_123",
                "user_id": "456",
            },
        )
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute(
                """
                INSERT INTO player(key, platform, platform_id, scope, session_id, day, redraw_count)
                VALUES (?, 'qq_official', 'bot', 'session', 'bot:GroupMessage:ABC123', ?, 0)
                """,
                (KEY, DAY),
            )
            db.executemany(
                """
                INSERT INTO history(player_key, day, action, character, character_name,
                                    character_source, identity)
                VALUES (?, ?, 'draw', ?, ?, ?, ?)
                """,
                [
                    (KEY, DAY, json.dumps(A), A["name"], A["source"], json.dumps(i))
                    for i in older_identities
                ],
            )
        reopened = WifeStore(self.path)
        history = await reopened.history(KEY)
        self.assertEqual(
            [(item["conversation_type"], item["group_id"]) for item in history["items"]],
            [("group", "123"), ("private", ""), ("group", "ABC123"), ("unknown", "")],
        )
        self.assertEqual((await reopened.groups())["total"], 4)
        self.assertEqual(await WifeStore(self.path).history(KEY), history)

    async def test_legacy_qq_group_is_recovered_only_with_matching_state_key(self):
        with patch.object(WifeStore, "_migrate_groups"):
            await self.store.initialize()
        encoded = json.dumps(["qq_official", "bot", "qq_official", "ABC123", "user"])
        verified = "user:" + hashlib.sha256(encoded.encode()).hexdigest()
        with closing(sqlite3.connect(self.path)) as db, db:
            for key in (verified, OTHER_KEY):
                db.execute(
                    """
                    INSERT INTO player(key, platform, platform_id, scope, session_id, user_id,
                                       day, redraw_count)
                    VALUES (?, 'qq_official', 'bot', 'group', 'ABC123', 'user', ?, 0)
                    """,
                    (key, DAY),
                )
                db.execute(
                    """
                    INSERT INTO history(player_key, day, action, character, character_name,
                                        character_source, identity)
                    VALUES (?, ?, 'draw', ?, ?, ?, '{}')
                    """,
                    (key, DAY, json.dumps(A), A["name"], A["source"]),
                )
        reopened = WifeStore(self.path)
        records = (await reopened.history())["items"]
        self.assertEqual(
            [(r["key"], r["conversation_type"], r["group_id"]) for r in records],
            [(OTHER_KEY, "unknown", ""), (verified, "group", "ABC123")],
        )
