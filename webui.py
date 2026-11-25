"""Authenticated AstrBot Dashboard extensions for the draw ledger."""

from __future__ import annotations

import re
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from astrbot.api import logger

PLAYER_KEY = re.compile(r"user:(?:session:)?[a-f0-9]{64}")
HISTORY_ACTIONS = {"", "draws", "draw", "redraw", "marry", "divorce"}
PRIVATE_HEADERS = {"Cache-Control": "no-store"}


class RequestError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def player_key(value: Any) -> str:
    if not isinstance(value, str) or not PLAYER_KEY.fullmatch(value):
        raise RequestError("玩家标识无效，请重新选择记录。")
    return value


class Dashboard:
    def __init__(self, plugin: Any, plugin_name: str) -> None:
        # Import at registration time so the command plugin can still load if
        # a test harness or compatible core doesn't start a Dashboard.
        from astrbot.api.web import json_response, request

        self.plugin = plugin
        self.plugin_name = plugin_name
        self.request = request
        self.json_response = json_response
        self._closed = False

    def register(self) -> None:
        for path, handler, method, description in (
            ("overview", self.overview, "GET", "概览"),
            ("players", self.players, "GET", "玩家和婚姻"),
            ("history", self.history, "GET", "抽取与关系流水"),
            ("reset-count", self.reset_count, "POST", "重置玩家今日换老婆次数"),
        ):
            self.plugin.context.register_web_api(
                f"/{self.plugin_name}/ledger/{path}", handler, [method], f"我老婆：{description}"
            )

    def close(self) -> None:
        self._closed = True
        # AstrBot replaces matching routes on reload but doesn't offer an
        # unregister method. Do not remove handlers owned by a newer instance.
        routes = getattr(self.plugin.context, "registered_web_apis", None)
        if isinstance(routes, list):
            routes[:] = [
                route for route in routes if getattr(route[1], "__self__", None) is not self
            ]

    def _reply(self, data: dict[str, Any], status: int = 200) -> Any:
        return self.json_response(data, status_code=status, headers=PRIVATE_HEADERS)

    async def _serve(self, operation: Callable[[], Awaitable[dict[str, Any]]]) -> Any:
        if self._closed:
            return self._reply({"status": "error", "message": "插件正在停止，请稍后刷新。"}, 503)
        if not self.request.username:
            return self._reply({"status": "error", "message": "请先登录 AstrBot 管理面板。"}, 401)
        try:
            return self._reply(await operation())
        except RequestError as exc:
            return self._reply({"status": "error", "message": str(exc)}, exc.status)
        except Exception as exc:
            logger.error("[我老婆] 管理页面 %s 失败：%s", operation.__name__, type(exc).__name__)
            return self._reply(
                {"status": "error", "message": "数据暂不可用，请查看插件日志。"}, 500
            )

    def _local_now(self) -> tuple[str, float]:
        now = time.time()
        return datetime.fromtimestamp(now, self.plugin.timezone).date().isoformat(), now

    def _page(self) -> tuple[int, int, str]:
        args = self.request.query
        try:
            page = int(args.get("page", "1"))
            size = int(args.get("size", "25"))
        except (TypeError, ValueError):
            raise RequestError("页码和每页条数必须为整数。") from None
        if not 1 <= page <= 1_000_000 or not 1 <= size <= 100:
            raise RequestError("页码须为正整数，每页条数须在 1–100 之间。")
        query = args.get("q", "").strip()
        if len(query) > 100:
            raise RequestError("搜索词最多输入 100 个字符。")
        return page, size, query

    async def overview(self) -> Any:
        async def load() -> dict[str, Any]:
            day, now = self._local_now()
            return {
                **await self.plugin.store.stats(day, now),
                "date": day,
                "timezone": self.plugin.timezone.key,
                "daily_limit": self.plugin.daily_limit,
            }

        return await self._serve(load)

    async def players(self) -> Any:
        async def load() -> dict[str, Any]:
            page, size, query = self._page()
            day, now = self._local_now()
            listing = await self.plugin.store.players(
                day=day, now=now, query=query, limit=size, offset=(page - 1) * size
            )
            return {**listing, "page": page, "size": size}

        return await self._serve(load)

    async def history(self) -> Any:
        async def load() -> dict[str, Any]:
            page, size, query = self._page()
            action = self.request.query.get("action", "")
            if action not in HISTORY_ACTIONS:
                raise RequestError("不支持的流水类型。")
            raw_key = self.request.query.get("player_key", "")
            key = player_key(raw_key) if raw_key else None
            listing = await self.plugin.store.history(
                key, query=query, action=action, limit=size, offset=(page - 1) * size
            )
            return {**listing, "page": page, "size": size}

        return await self._serve(load)

    async def reset_count(self) -> Any:
        async def reset() -> dict[str, Any]:
            if (self.request.content_type or "").partition(";")[0].strip() != "application/json":
                raise RequestError("请提交 JSON 请求。")
            body = await self.request.json()
            if not isinstance(body, dict) or set(body) != {"player_key"}:
                raise RequestError("请先选择一位玩家。")
            key = player_key(body["player_key"])
            day, now = self._local_now()
            player = await self.plugin.store.admin_reset_count(key, day, now)
            if player is None:
                raise RequestError("找不到这位玩家的记录。", 404)
            return {"key": player["key"], "redraw_count": player["redraw_count"]}

        return await self._serve(reset)
