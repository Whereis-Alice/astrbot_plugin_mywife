"""Transactional player state, history, and migration from AstrBot plugin KV."""

from __future__ import annotations

import asyncio
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
            row = db.execute(
                "SELECT 1 FROM store_meta WHERE key = 'legacy_kv_import_v1'"
            ).fetchone()
        return row is None

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
                self._save_player(
                    db,
                    key,
                    identity,
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
                    )
            db.execute("INSERT INTO store_meta(key) VALUES ('legacy_kv_import_v1')")

    @staticmethod
    def _get_row(db: sqlite3.Connection, key: str) -> sqlite3.Row | None:
        return db.execute("SELECT * FROM player WHERE key = ?", (key,)).fetchone()

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
            **{field: row[field] for field in IDENTITY_FIELDS},
            **state,
            "updated_at": row["updated_at"],
            "draws": row["draws"],
        }

    @staticmethod
    def _save_player(
        db: sqlite3.Connection,
        key: str,
        identity: dict[str, str],
        state: dict[str, Any],
        updated_at: float | None,
        draws: int,
    ) -> None:
        db.execute(
            """
            INSERT INTO player(
                key, platform, platform_id, scope, session_id, user_id, display_name,
                day, wife, marriage, marriage_expires_at, redraw_count, updated_at, draws
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                platform=excluded.platform, platform_id=excluded.platform_id,
                scope=excluded.scope, session_id=excluded.session_id, user_id=excluded.user_id,
                display_name=excluded.display_name, day=excluded.day, wife=excluded.wife,
                marriage=excluded.marriage, marriage_expires_at=excluded.marriage_expires_at,
                redraw_count=excluded.redraw_count, updated_at=excluded.updated_at,
                draws=excluded.draws
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
    ) -> None:
        db.execute(
            """
            INSERT INTO history(
                player_key, day, at, action, character, character_name, character_source, identity
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                key,
                day,
                at,
                action,
                _json(character),
                str(character.get("name") or ""),
                str(character.get("source") or ""),
                _json(identity),
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
            values["can_redraw"] = bool(
                state["wife"] and not state["marriage"] and state["redraw_count"] < limit
            )
            draws = (row["draws"] if row else 0) + (scenario in DRAW_ACTIONS)
            identity = _identity_fields(identity)
            state_changed = row is not None and (
                row["day"] != day
                or (row["marriage"] and not state["marriage"])
                or any(row[field] != identity[field] for field in IDENTITY_FIELDS)
            )
            if scenario in EVENT_ACTIONS or state_changed:
                self._save_player(db, key, identity, state, now, draws)
            if scenario in EVENT_ACTIONS and character is not None:
                self._insert_history(db, key, day, now, scenario, character, identity)
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
            state = self._player_state(row, day, now)
            state["redraw_count"] = 0
            self._save_player(db, key, current_identity, state, now, row["draws"] if row else 0)
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

    def _players(self, day: str, now: float, query: str, limit: int, offset: int) -> dict[str, Any]:
        clause, params = _search_clause(("key", *IDENTITY_FIELDS), query)
        where = f" WHERE {clause}" if clause else ""
        with self._transaction() as db:
            total = db.execute(f"SELECT COUNT(*) FROM player{where}", params).fetchone()[0]
            rows = db.execute(
                f"SELECT * FROM player{where} ORDER BY updated_at DESC, key LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
            return {"items": [self._player_item(row, day, now) for row in rows], "total": total}

    async def players(
        self, *, day: str, now: float, query: str = "", limit: int = 50, offset: int = 0
    ) -> dict[str, Any]:
        """Return a page of players and its total from a single read snapshot."""
        await self.initialize()
        return await asyncio.to_thread(self._players, day, now, query, *_page(limit, offset))

    def _history(
        self, key: str | None, query: str, action: str, limit: int, offset: int
    ) -> dict[str, Any]:
        conditions = []
        params = []
        if key is not None:
            conditions.append("h.player_key = ?")
            params.append(key)
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
                "json_extract(h.identity, '$.display_name')",
                "json_extract(h.identity, '$.session_id')",
            ),
            query,
        )
        if search:
            conditions.append(search)
            params.extend(terms)
        source = "history h JOIN player p ON p.key = h.player_key"
        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        with self._transaction() as db:
            total = db.execute(f"SELECT COUNT(*) FROM {source}{where}", params).fetchone()[0]
            rows = db.execute(
                f"""
                SELECT h.id, h.player_key AS key, h.day AS date, h.at, h.action, h.character,
                       h.identity
                FROM {source}{where} ORDER BY h.day DESC, h.id DESC LIMIT ? OFFSET ?
                """,
                (*params, limit, offset),
            ).fetchall()
            items = [dict(row) for row in rows]
            for item in items:
                item["character"] = json.loads(item["character"])
                item.update(json.loads(item.pop("identity")))
            return {"items": items, "total": total}

    async def history(
        self,
        key: str | None = None,
        *,
        query: str = "",
        action: str = "",
        limit: int = 20,
        offset: int = 0,
    ) -> dict[str, Any]:
        """List recorded draws or relationship changes, newest first."""
        if action and action not in (*EVENT_ACTIONS, "draws"):
            raise ValueError("Unknown history action")
        await self.initialize()
        return await asyncio.to_thread(self._history, key, query, action, *_page(limit, offset))

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
