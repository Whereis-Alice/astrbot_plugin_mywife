"""Transactional player state, history, and migration from AstrBot plugin KV."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import sqlite3
from collections.abc import Awaitable, Callable, Iterator, Mapping
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any

from .domain import change_state, prepare_state

LEGACY_KEY = re.compile(r"user:(?:session:)?[0-9a-f]{64}")
IDENTITY_FIELDS = ("platform", "platform_id", "scope", "session_id", "user_id", "display_name")
GROUP_FIELDS = ("group_key", "group_id", "group_name", "conversation_type")
GROUP_DISPLAY_FIELDS = (*GROUP_FIELDS, "group_alias", "reported_group_name")
DRAW_ACTIONS = ("draw", "redraw")
EVENT_ACTIONS = (*DRAW_ACTIONS, "marry", "divorce")
_SCHEMA = """
CREATE TABLE IF NOT EXISTS player (
    key TEXT PRIMARY KEY,
    platform TEXT NOT NULL DEFAULT '',
    platform_id TEXT NOT NULL DEFAULT '',
    scope TEXT NOT NULL DEFAULT '',
    session_id TEXT NOT NULL DEFAULT '',
    user_id TEXT NOT NULL DEFAULT '',
    display_name TEXT NOT NULL DEFAULT '',
    day TEXT NOT NULL,
    wife TEXT,
    marriage TEXT,
    marriage_expires_at REAL,
    redraw_count INTEGER NOT NULL,
    updated_at REAL,
    draws INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS history (
    id INTEGER PRIMARY KEY,
    player_key TEXT NOT NULL REFERENCES player(key),
    day TEXT NOT NULL,
    at REAL,
    action TEXT NOT NULL CHECK(action IN ('draw', 'redraw', 'marry', 'divorce')),
    character TEXT NOT NULL,
    character_name TEXT NOT NULL,
    character_source TEXT NOT NULL,
    identity TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS store_meta (
    key TEXT PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS group_chat (
    group_key TEXT PRIMARY KEY,
    group_id TEXT NOT NULL,
    group_name TEXT NOT NULL,
    conversation_type TEXT NOT NULL,
    platform TEXT NOT NULL,
    platform_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS group_alias (
    group_key TEXT PRIMARY KEY REFERENCES group_chat(group_key),
    name TEXT NOT NULL
);
CREATE VIEW IF NOT EXISTS group_directory AS
    SELECT g.group_key, g.group_id, COALESCE(a.name, g.group_name) AS group_name,
           g.group_name AS reported_group_name, COALESCE(a.name, '') AS group_alias,
           g.conversation_type, g.platform, g.platform_id
    FROM group_chat g LEFT JOIN group_alias a ON a.group_key = g.group_key;
CREATE INDEX IF NOT EXISTS history_player_day ON history(player_key, day DESC, id DESC);
CREATE INDEX IF NOT EXISTS history_day ON history(day DESC, id DESC);
CREATE INDEX IF NOT EXISTS player_activity ON player(updated_at DESC, key);
CREATE INDEX IF NOT EXISTS player_day ON player(day);
CREATE INDEX IF NOT EXISTS player_marriage ON player(marriage_expires_at);
"""


def _json(value: Any) -> str | None:
    return None if value is None else json.dumps(value, ensure_ascii=False, allow_nan=False)


def _legacy_time(value: Any) -> float | None:
    try:
        timestamp = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return timestamp if math.isfinite(timestamp) else None


def _identity_fields(identity: Mapping[str, Any] | None) -> dict[str, str]:
    return {field: str((identity or {}).get(field) or "")[:512] for field in IDENTITY_FIELDS}


def _verified_legacy_qq_identity(player: sqlite3.Row) -> dict[str, str]:
    identity = _identity_fields(dict(player))
    platform, instance = identity["platform"], identity["platform_id"]
    session, user = identity["session_id"], identity["user_id"]
    if (
        platform != "qq_official"
        or identity["scope"] != "group"
        or not instance
        or not user
        or not re.fullmatch(r"[a-zA-Z0-9]+", session)
    ):
        return {}
    # A verified legacy key binds these records to one group (or private chat).
    data = json.dumps([platform, instance, "qq_official", session, user], ensure_ascii=False)
    key = "user:" + hashlib.sha256(data.encode()).hexdigest()
    if player["key"] != key:
        return {}
    identity["display_name"] = ""
    return identity


def _conversation(identity: Mapping[str, Any]) -> dict[str, str]:
    platform = str(identity.get("platform") or "")
    platform_id = str(identity.get("platform_id") or "")
    group_id = str(identity.get("group_id") or "").strip()[:512]
    group_name = str(identity.get("group_name") or "").strip()[:512]
    kind = identity.get("conversation_type")
    if not group_id and not kind:
        scope = identity.get("scope")
        session = str(identity.get("session_id") or "")
        if scope == "group" and session:
            if session == "private":
                kind = "private"
            else:
                group_id = session
        elif scope == "session" and platform_id:
            bot, _, tail = session.partition(":")
            message_type, _, address = tail.partition(":")
            if bot == platform_id and address:
                if message_type == "FriendMessage":
                    kind = "private"
                elif message_type == "GroupMessage":
                    if platform in ("qq_official", "qq_official_webhook") and re.fullmatch(
                        r"[a-zA-Z0-9]+", address
                    ):
                        group_id = address
                    elif platform == "aiocqhttp":
                        prefix = f"{identity.get('user_id')}_"
                        if address.startswith(prefix):
                            address = address[len(prefix) :]
                        if re.fullmatch(r"[0-9]+", address):
                            group_id = address

    if group_id:
        data = json.dumps([platform, platform_id, group_id], ensure_ascii=False)
        return {
            "group_key": hashlib.sha256(data.encode("utf-8")).hexdigest(),
            "group_id": group_id,
            "group_name": group_name,
            "conversation_type": "group",
            "platform": platform,
            "platform_id": platform_id,
        }
    kind = "private" if kind == "private" else "unknown"
    return {
        "group_key": kind,
        "group_id": "",
        "group_name": "",
        "conversation_type": kind,
        "platform": "",
        "platform_id": "",
    }


def _search_clause(fields: tuple[str, ...], query: str) -> tuple[str, tuple[str, ...]]:
    query = query.strip().casefold()[:100]
    if not query:
        return "", ()
    return "(" + " OR ".join(f"instr(casefold({field}), ?) > 0" for field in fields) + ")", (
        query,
    ) * len(fields)


def _page(limit: int, offset: int) -> tuple[int, int]:
    return max(1, min(250, int(limit))), max(0, min(2**63 - 1, int(offset)))


class WifeStore:
    """SQLite is authoritative after import; legacy preferences are left intact."""

    def __init__(
        self,
        path: Path,
        legacy_scan: Callable[[], Awaitable[Mapping[str, Any]]] | None = None,
    ) -> None:
        self.path = Path(path)
        self._legacy_scan = legacy_scan
        self._initializing = asyncio.Lock()
        self._initialized = False

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        db.create_function("casefold", 1, lambda value: str(value or "").casefold())
        return db

    @contextmanager
    def _transaction(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield db

    def _setup(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db:
            db.executescript(_SCHEMA)
        with self._transaction(write=True) as db:
            self._migrate_groups(db)
            row = db.execute(
                "SELECT 1 FROM store_meta WHERE key = 'legacy_kv_import_v1'"
            ).fetchone()
        return row is None

    @classmethod
    def _migrate_groups(cls, db: sqlite3.Connection) -> None:
        if db.execute("SELECT 1 FROM store_meta WHERE key = 'history_groups_v1'").fetchone():
            return
        db.execute("ALTER TABLE player ADD COLUMN group_key TEXT NOT NULL DEFAULT ''")
        db.execute("ALTER TABLE history ADD COLUMN group_key TEXT NOT NULL DEFAULT ''")
        for row in db.execute(
            """
            SELECT h.id AS history_id, h.identity, p.* FROM history h
            JOIN player p ON p.key = h.player_key ORDER BY h.id
            """
        ):
            identity = json.loads(row["identity"])
            recovered = _verified_legacy_qq_identity(row) if not any(identity.values()) else {}
            group = _conversation(recovered or identity)
            cls._save_group(db, group)
            db.execute(
                "UPDATE history SET group_key = ?, identity = COALESCE(?, identity) WHERE id = ?",
                (group["group_key"], _json(recovered or None), row["history_id"]),
            )
        for row in db.execute("SELECT * FROM player ORDER BY updated_at, key"):
            group = _conversation(dict(row))
            cls._save_group(db, group)
            db.execute(
                "UPDATE player SET group_key = ? WHERE key = ?", (group["group_key"], row["key"])
            )
        db.execute("CREATE INDEX history_group_day ON history(group_key, day DESC, id DESC)")
        db.execute("CREATE INDEX history_group_player ON history(group_key, player_key)")
        db.execute("CREATE INDEX player_group ON player(group_key, key)")
        db.execute("INSERT INTO store_meta(key) VALUES ('history_groups_v1')")

    @staticmethod
    def _save_group(db: sqlite3.Connection, group: dict[str, str]) -> None:
        db.execute(
            """
            INSERT INTO group_chat(group_key, group_id, group_name, conversation_type, platform, platform_id)
            VALUES (:group_key, :group_id, :group_name, :conversation_type, :platform, :platform_id)
            ON CONFLICT(group_key) DO UPDATE SET group_name = excluded.group_name
            WHERE excluded.group_name <> '' AND excluded.group_name <> group_chat.group_name
            """,
            group,
        )

    async def initialize(self) -> None:
        """Create the store and import legacy data once, retrying on failure."""
        if self._initialized:
            return
        async with self._initializing:
            if self._initialized:
                return
            needs_import = await asyncio.to_thread(self._setup)
            if needs_import and self._legacy_scan is not None:
                records = await self._legacy_scan()
                await asyncio.to_thread(self._import_legacy, records)
            self._initialized = True

    def _import_legacy(self, records: Mapping[str, Any]) -> None:
        with self._transaction(write=True) as db:
            if db.execute("SELECT 1 FROM store_meta WHERE key = 'legacy_kv_import_v1'").fetchone():
                return
            for key, raw in records.items():
                if not isinstance(key, str) or not LEGACY_KEY.fullmatch(key):
                    continue
                if not isinstance(raw, dict) or self._get_row(db, key) is not None:
                    continue
                day = raw.get("date") if isinstance(raw.get("date"), str) else ""
                state = prepare_state(raw, day, 0)
                raw_history = raw.get("history")
                history = [
                    item
                    for item in (raw_history if isinstance(raw_history, list) else [])
                    if isinstance(item, dict)
                    and item.get("action") in EVENT_ACTIONS
                    and isinstance(item.get("character"), dict)
                ]
                scope = "session" if key.startswith("user:session:") else ""
                identity = _identity_fields({"scope": scope})
                group = _conversation(identity)
                self._save_group(db, group)
                self._save_player(
                    db,
                    key,
                    identity,
                    group["group_key"],
                    state,
                    None,
                    sum(item["action"] in DRAW_ACTIONS for item in history),
                )
                for item in history:
                    event_day = item.get("date")
                    self._insert_history(
                        db,
                        key,
                        event_day if isinstance(event_day, str) else day,
                        _legacy_time(item.get("at")),
                        item["action"],
                        item["character"],
                        identity,
                        group,
                    )
            db.execute("INSERT INTO store_meta(key) VALUES ('legacy_kv_import_v1')")

    @staticmethod
    def _get_row(db: sqlite3.Connection, key: str) -> sqlite3.Row | None:
        return db.execute(
            """
            SELECT p.*, g.group_id, g.group_name, g.conversation_type, g.group_alias,
                   g.reported_group_name
            FROM player p JOIN group_directory g ON g.group_key = p.group_key WHERE p.key = ?
            """,
            (key,),
        ).fetchone()

    @staticmethod
    def _player_state(row: sqlite3.Row | None, day: str, now: float) -> dict[str, Any]:
        raw = (
            {
                "date": row["day"],
                "wife": json.loads(row["wife"]) if row["wife"] else None,
                "marriage": json.loads(row["marriage"]) if row["marriage"] else None,
                "redraw_count": row["redraw_count"],
            }
            if row is not None
            else {}
        )
        return prepare_state(raw, day, now)

    @classmethod
    def _player_item(cls, row: sqlite3.Row, day: str, now: float) -> dict[str, Any]:
        state = cls._player_state(row, day, now)
        state.pop("history")
        return {
            "key": row["key"],
            **{field: row[field] for field in (*IDENTITY_FIELDS, *GROUP_DISPLAY_FIELDS)},
            **state,
            "updated_at": row["updated_at"],
            "draws": row["draws"],
        }

    @staticmethod
    def _save_player(
        db: sqlite3.Connection,
        key: str,
        identity: dict[str, str],
        group_key: str,
        state: dict[str, Any],
        updated_at: float | None,
        draws: int,
    ) -> None:
        db.execute(
            """
            INSERT INTO player(
                key, platform, platform_id, scope, session_id, user_id, display_name,
                day, wife, marriage, marriage_expires_at, redraw_count, updated_at, draws, group_key
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                platform=excluded.platform, platform_id=excluded.platform_id,
                scope=excluded.scope, session_id=excluded.session_id, user_id=excluded.user_id,
                display_name=excluded.display_name, day=excluded.day, wife=excluded.wife,
                marriage=excluded.marriage, marriage_expires_at=excluded.marriage_expires_at,
                redraw_count=excluded.redraw_count, updated_at=excluded.updated_at,
                draws=excluded.draws, group_key=excluded.group_key
            """,
            (
                key,
                *(identity[field] for field in IDENTITY_FIELDS),
                state["date"],
                _json(state["wife"]),
                _json(state["marriage"]),
                state["marriage"]["expires_at"] if state["marriage"] else None,
                state["redraw_count"],
                updated_at,
                draws,
                group_key,
            ),
        )

    @staticmethod
    def _insert_history(
        db: sqlite3.Connection,
        key: str,
        day: str,
        at: float | None,
        action: str,
        character: dict[str, Any],
        identity: dict[str, str],
        group: dict[str, str],
    ) -> None:
        db.execute(
            """
            INSERT INTO history(
                player_key, day, at, action, character, character_name, character_source, identity, group_key
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                key,
                day,
                at,
                action,
                _json(character),
                str(character.get("name") or ""),
                str(character.get("source") or ""),
                _json({**identity, **{field: group[field] for field in GROUP_FIELDS}}),
                group["group_key"],
            ),
        )

    def _change(
        self,
        key: str,
        identity: Mapping[str, Any],
        day: str,
        now: float,
        action: str,
        limit: int,
        duration: int,
        choose: Callable[[str | None], dict[str, Any] | None],
    ) -> tuple[str, dict[str, Any] | None, dict[str, int]]:
        with self._transaction(write=True) as db:
            row = self._get_row(db, key)
            state = self._player_state(row, day, now)
            scenario, character, values = change_state(state, action, now, limit, duration, choose)
            draws = (row["draws"] if row else 0) + (scenario in DRAW_ACTIONS)
            group = _conversation(identity)
            identity = _identity_fields(identity)
            self._save_group(db, group)
            state_changed = row is not None and (
                row["day"] != day
                or (row["marriage"] and not state["marriage"])
                or any(row[field] != identity[field] for field in IDENTITY_FIELDS)
                or row["group_key"] != group["group_key"]
            )
            if scenario in EVENT_ACTIONS or state_changed:
                self._save_player(db, key, identity, group["group_key"], state, now, draws)
            if scenario in EVENT_ACTIONS and character is not None:
                self._insert_history(db, key, day, now, scenario, character, identity, group)
            return scenario, character, values

    async def change(
        self,
        key: str,
        identity: Mapping[str, Any],
        day: str,
        now: float,
        action: str,
        limit: int,
        duration: int,
        choose: Callable[[str | None], dict[str, Any] | None],
    ) -> tuple[str, dict[str, Any] | None, dict[str, int]]:
        """Apply a game action and commit its history in the same transaction."""
        if action not in EVENT_ACTIONS:
            raise ValueError("Unknown game action")
        await self.initialize()
        return await asyncio.to_thread(
            self._change, key, identity, day, now, action, limit, duration, choose
        )

    def _reset_count(
        self, key: str, identity: Mapping[str, Any] | None, day: str, now: float
    ) -> dict[str, Any] | None:
        with self._transaction(write=True) as db:
            row = self._get_row(db, key)
            if row is None and identity is None:
                return None
            current_identity = (
                {field: row[field] for field in IDENTITY_FIELDS}
                if identity is None and row is not None
                else _identity_fields(identity)
            )
            if identity is None:
                group_key = row["group_key"]
            else:
                group = _conversation(identity)
                self._save_group(db, group)
                group_key = group["group_key"]
            state = self._player_state(row, day, now)
            state["redraw_count"] = 0
            self._save_player(
                db, key, current_identity, group_key, state, now, row["draws"] if row else 0
            )
            saved = self._get_row(db, key)
            return self._player_item(saved, day, now) if saved is not None else None

    async def reset_count(
        self, key: str, identity: Mapping[str, Any], day: str, now: float
    ) -> dict[str, Any]:
        """Reset the current scope's quota and preserve its current role and history."""
        await self.initialize()
        player = await asyncio.to_thread(self._reset_count, key, identity, day, now)
        if player is None:
            raise RuntimeError("Reset did not save a player")
        return player

    async def admin_reset_count(self, key: str, day: str, now: float) -> dict[str, Any] | None:
        """Reset an existing player from management without creating unknown records."""
        await self.initialize()
        return await asyncio.to_thread(self._reset_count, key, None, day, now)

    def _get_player(self, key: str, day: str, now: float) -> dict[str, Any] | None:
        with self._transaction() as db:
            row = self._get_row(db, key)
            return self._player_item(row, day, now) if row is not None else None

    async def get_player(self, key: str, day: str, now: float) -> dict[str, Any] | None:
        await self.initialize()
        return await asyncio.to_thread(self._get_player, key, day, now)

    def _groups(self, query: str, limit: int, offset: int) -> dict[str, Any]:
        search, params = _search_clause(("g.group_id", "g.group_name", "g.platform_id"), query)
        where = """
            WHERE (EXISTS(SELECT 1 FROM player WHERE group_key = g.group_key)
                   OR EXISTS(SELECT 1 FROM history WHERE group_key = g.group_key))
        """
        if search:
            where += f" AND {search}"
        with self._transaction() as db:
            total = db.execute(
                f"SELECT COUNT(*) FROM group_directory g {where}", params
            ).fetchone()[0]
            rows = db.execute(
                f"""
                SELECT g.*,
                       (SELECT COUNT(*) FROM history WHERE group_key = g.group_key) AS history_count,
                       (SELECT COUNT(*) FROM (
                            SELECT key FROM player WHERE group_key = g.group_key
                            UNION
                            SELECT player_key FROM history WHERE group_key = g.group_key
                       )) AS player_count
                FROM group_directory g {where}
                ORDER BY g.conversation_type, casefold(g.group_name), g.group_id, g.group_key
                LIMIT ? OFFSET ?
                """,
                (*params, limit, offset),
            ).fetchall()
            return {"items": [dict(row) for row in rows], "total": total}

    async def groups(self, *, query: str = "", limit: int = 100, offset: int = 0) -> dict[str, Any]:
        """List recorded conversations and their player and history counts."""
        await self.initialize()
        return await asyncio.to_thread(self._groups, query, *_page(limit, offset))

    def _set_group_alias(self, group_key: str, alias: str) -> dict[str, Any] | None:
        with self._transaction(write=True) as db:
            row = db.execute(
                "SELECT group_key FROM group_chat WHERE group_key = ? AND conversation_type = 'group'",
                (group_key,),
            ).fetchone()
            if row is None:
                return None
            if alias:
                db.execute(
                    """
                    INSERT INTO group_alias(group_key, name) VALUES (?, ?)
                    ON CONFLICT(group_key) DO UPDATE SET name = excluded.name
                    """,
                    (group_key, alias),
                )
            else:
                db.execute("DELETE FROM group_alias WHERE group_key = ?", (group_key,))
            return dict(
                db.execute(
                    "SELECT * FROM group_directory WHERE group_key = ?", (group_key,)
                ).fetchone()
            )

    async def set_group_alias(self, group_key: str, alias: str) -> dict[str, Any] | None:
        """Set a display name for a known group or clear the override."""
        await self.initialize()
        return await asyncio.to_thread(self._set_group_alias, group_key, alias.strip()[:100])

    def _players(
        self, day: str, now: float, query: str, group_key: str, limit: int, offset: int
    ) -> dict[str, Any]:
        search, terms = _search_clause(
            ("p.key", *(f"p.{field}" for field in IDENTITY_FIELDS), "g.group_id", "g.group_name"),
            query,
        )
        conditions = [search] if search else []
        params = terms
        source = "player p JOIN group_directory g ON g.group_key = p.group_key"
        if group_key:
            source = """
                group_directory g JOIN player p ON p.key IN (
                    SELECT key FROM player WHERE group_key = g.group_key
                    UNION
                    SELECT player_key FROM history WHERE group_key = g.group_key
                )
            """
            conditions.append("g.group_key = ?")
            params = (*params, group_key)
        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        with self._transaction() as db:
            total = db.execute(f"SELECT COUNT(*) FROM {source}{where}", params).fetchone()[0]
            rows = db.execute(
                f"""
                SELECT g.group_key, g.group_id, g.group_name, g.conversation_type,
                       g.group_alias, g.reported_group_name, p.*
                FROM {source}{where} ORDER BY p.updated_at DESC, p.key LIMIT ? OFFSET ?
                """,
                (*params, limit, offset),
            ).fetchall()
            return {"items": [self._player_item(row, day, now) for row in rows], "total": total}

    async def players(
        self,
        *,
        day: str,
        now: float,
        query: str = "",
        group_key: str = "",
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Return a page of players and its total from a single read snapshot."""
        await self.initialize()
        return await asyncio.to_thread(
            self._players, day, now, query, group_key, *_page(limit, offset)
        )

    def _history(
        self, key: str | None, query: str, action: str, group_key: str, limit: int, offset: int
    ) -> dict[str, Any]:
        conditions = []
        params = []
        if key is not None:
            conditions.append("h.player_key = ?")
            params.append(key)
        if group_key:
            conditions.append("h.group_key = ?")
            params.append(group_key)
        if action == "draws":
            conditions.append("h.action IN ('draw', 'redraw')")
        elif action:
            conditions.append("h.action = ?")
            params.append(action)
        search, terms = _search_clause(
            (
                "h.player_key",
                "h.character_name",
                "h.character_source",
                "p.user_id",
                "p.display_name",
                "g.group_id",
                "g.group_name",
                "json_extract(h.identity, '$.display_name')",
                "json_extract(h.identity, '$.session_id')",
            ),
            query,
        )
        if search:
            conditions.append(search)
            params.extend(terms)
        source = """
            history h JOIN player p ON p.key = h.player_key
            JOIN group_directory g ON g.group_key = h.group_key
        """
        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        with self._transaction() as db:
            total = db.execute(f"SELECT COUNT(*) FROM {source}{where}", params).fetchone()[0]
            rows = db.execute(
                f"""
                SELECT h.id, h.player_key AS key, h.day AS date, h.at, h.action, h.character,
                       h.identity, h.group_key, g.group_id, g.group_name, g.conversation_type,
                       g.group_alias, g.reported_group_name
                FROM {source}{where} ORDER BY h.day DESC, h.id DESC LIMIT ? OFFSET ?
                """,
                (*params, limit, offset),
            ).fetchall()
            items = []
            for row in rows:
                item = dict(row)
                item["character"] = json.loads(item["character"])
                items.append({**json.loads(item.pop("identity")), **item})
            return {"items": items, "total": total}

    async def history(
        self,
        key: str | None = None,
        *,
        query: str = "",
        action: str = "",
        group_key: str = "",
        limit: int = 20,
        offset: int = 0,
    ) -> dict[str, Any]:
        """List recorded draws or relationship changes, newest first."""
        if action and action not in (*EVENT_ACTIONS, "draws"):
            raise ValueError("Unknown history action")
        await self.initialize()
        return await asyncio.to_thread(
            self._history, key, query, action, group_key, *_page(limit, offset)
        )

    def _stats(self, day: str, now: float) -> dict[str, int]:
        with self._transaction() as db:
            row = db.execute(
                """
                SELECT
                  (SELECT COUNT(*) FROM player) AS players,
                  (SELECT COUNT(DISTINCT player_key) FROM history
                   WHERE day = ? AND action IN ('draw', 'redraw')) AS today,
                  (SELECT COUNT(*) FROM player WHERE marriage_expires_at > ?) AS marriages,
                  (SELECT COALESCE(SUM(draws), 0) FROM player) AS draws,
                  (SELECT COUNT(*) FROM history
                   WHERE day = ? AND action IN ('draw', 'redraw')) AS today_draws
                """,
                (day, now, day),
            ).fetchone()
            return dict(row)

    async def stats(self, day: str, now: float) -> dict[str, int]:
        await self.initialize()
        return await asyncio.to_thread(self._stats, day, now)
