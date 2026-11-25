"""我老婆: daily characters from monbed/wife for AstrBot."""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
import time
import weakref
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Plain
from astrbot.api.star import Context, Star, StarTools

from .catalog import Catalog
from .domain import change_state, parse_command, prepare_state
from .image_store import ImageStore
from .qq_media import QQMediaStore
from .rendering import qq_markdown, unescape_markdown_text
from .replies import ReplyMixin, escape_markdown_field

PLUGIN_NAME = "astrbot_plugin_mywife"


def bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        return max(minimum, min(maximum, int(value)))
    except (TypeError, ValueError):
        return default


class MyWifePlugin(ReplyMixin, Star):
    """抽取今日老婆、换老婆、结婚及离婚；支持模型或固定模板。"""

    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.config = config
        self.data_dir = Path(StarTools.get_data_dir(PLUGIN_NAME))
        self.catalog = Catalog(self.data_dir, config)
        self.images = ImageStore(self.data_dir, config)
        self.qq_images = QQMediaStore(self.data_dir)
        self._locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()
        self._refresh_task: asyncio.Task | None = None
        self._last_manual_refresh = 0.0
        self._closing = False
        timezone = str(
            config.get("timezone", "") or context.get_config().get("timezone", "Asia/Shanghai")
        )
        try:
            self.timezone = ZoneInfo(timezone)
        except (KeyError, ValueError):
            self.timezone = ZoneInfo("Asia/Shanghai")
            logger.warning("[我老婆] 时区无效，使用 Asia/Shanghai。")

    async def initialize(self) -> None:
        if self.config.get("auto_refresh_catalog", True):
            self._refresh_task = asyncio.create_task(self.catalog.refresh())

    async def terminate(self) -> None:
        self._closing = True
        if self._refresh_task and not self._refresh_task.done():
            self._refresh_task.cancel()
        if self._refresh_task:
            await asyncio.gather(self._refresh_task, return_exceptions=True)
        await self.qq_images.close()
        await self.images.close()

    @property
    def daily_limit(self) -> int:
        return bounded_int(self.config.get("daily_hlp_limit", 5), 5, 0, 100)

    @property
    def marriage_duration(self) -> int:
        return bounded_int(self.config.get("marriage_duration", 7), 7, 1, 3650)

    def _state_key(self, event: AstrMessageEvent) -> str:
        platform = event.get_platform_name()
        # QQ's adapter uses different pseudo self IDs for C2C/group/channel
        # messages. Its configured platform ID is the stable bot identity.
        self_id = "qq_official" if platform == "qq_official" else event.get_self_id()
        scope = self.config.get("state_scope", "session")
        if scope not in ("group", "user"):
            # UMO follows AstrBot's actual session splitting. A shared group
            # UMO still needs the sender ID to keep each player's own state.
            parts = [
                platform,
                event.get_platform_id(),
                self_id,
                "session",
                str(event.unified_msg_origin),
                event.get_sender_id(),
            ]
            digest = hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode()).hexdigest()
            return f"user:session:{digest}"
        # Keep v1.0.0 keys exactly compatible for existing configurations.
        group = str(event.get_group_id() or "private") if scope == "group" else "all_groups"
        parts = [
            platform,
            event.get_platform_id(),
            self_id,
            group,
            event.get_sender_id(),
        ]
        digest = hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode()).hexdigest()
        return f"user:{digest}"

    def _scope_text(self) -> str:
        return {"group": "当前群聊或私聊", "user": "当前 Bot 的跨群共享范围"}.get(
            self.config.get("state_scope", "session"), "当前会话"
        )

    async def _reset_count_text(self, event: AstrMessageEvent) -> str:
        # All routing paths use this guard, including the no-prefix listener.
        if not event.is_admin():
            return "重置次数仅限 AstrBot 管理员使用。"
        key = self._state_key(event)
        async with self._user_lock(key):
            now = time.time()
            day = datetime.fromtimestamp(now, self.timezone).date().isoformat()
            state = prepare_state(await self.get_kv_data(key, {}), day, now)
            state["redraw_count"] = 0
            await self.put_kv_data(key, state)
        if state.get("marriage"):
            return f"已重置你在{self._scope_text()}的今日换老婆次数。当前婚姻仍有效，婚姻期间不能换老婆。"
        return f"已重置你在{self._scope_text()}的今日换老婆次数，今日可更换 {self.daily_limit} 次。当前角色和历史记录已保留。"

    def _user_lock(self, key: str) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    def _choose(self, previous: str | None) -> dict[str, Any] | None:
        candidates = [c for c in self.catalog.get_filtered() if c.id != previous]
        if not candidates:
            return None
        character = random.choice(candidates)
        record = character.to_record()
        record["image_path"] = random.choice(character.image_paths)
        return record

    async def _business(self, event: AstrMessageEvent, action: str) -> tuple[str, dict | None]:
        key = self._state_key(event)
        async with self._user_lock(key):
            now = time.time()
            day = datetime.fromtimestamp(now, self.timezone).date().isoformat()
            state = prepare_state(await self.get_kv_data(key, {}), day, now)
            scenario, character, values = change_state(
                state, action, now, self.daily_limit, self.marriage_duration, self._choose
            )
            await self.put_kv_data(key, state)
        text = await self._reply(
            event, scenario, event.get_sender_name() or "你", character, **values
        )
        return text, character

    @staticmethod
    def _directed_to_other(event: AstrMessageEvent) -> bool:
        mentions = [
            str(getattr(comp, "qq", ""))
            for comp in event.get_messages()
            if comp.__class__.__name__ == "At" and str(getattr(comp, "qq", "")).lower() != "all"
        ]
        targets = {event.get_self_id()}
        if event.get_platform_name() == "qq_official":
            # The official adapter represents its own C2C/channel mention with
            # this pseudo ID instead of the app ID.
            targets.add("qq_official")
        return bool(mentions and not targets.intersection(mentions))

    def _result(
        self, event: AstrMessageEvent, text: str, image: Path | None = None, markdown: bool = False
    ) -> Any:
        chain = [Plain(text)] if text else []
        if image:
            chain.append(Image.fromFileSystem(str(image)))
        return event.chain_result(chain).use_markdown(markdown).use_t2i(False)

    async def _send(
        self, event: AstrMessageEvent, text: str, character: dict | None = None
    ) -> None:
        if self._closing:
            return
        image = None
        path = None
        if character and self.config.get("enable_images", True):
            path = character.get("image_path")
            if not path:
                found = self.catalog.find(character.get("id", ""))
                if found:
                    path = found.image_paths[0]
            if path:
                try:
                    image = await self.images.get_image(path)
                except Exception as exc:
                    logger.warning("[我老婆] 图片获取失败：%s", type(exc).__name__)
        markdown = event.get_platform_name() == "qq_official" and bool(
            self.config.get("qq_official_markdown", True)
        )
        if self._closing:
            return
        if markdown:
            # QQ clients can reject external GitHub/CDN pictures even when
            # the bot downloaded them successfully. Use QQ-owned media instead.
            image_url, size = None, None
            if image and path:
                try:
                    image_url = await self.qq_images.get_image_url(event, image)
                    size = await self.images.image_dimensions(image)
                except Exception as exc:
                    logger.warning("[我老婆] QQ 图片处理失败：%s", type(exc).__name__)
            if self._closing:
                return
            if image is None or (image_url and size):
                await event.send(
                    self._result(event, qq_markdown(text, image_url, size), markdown=True)
                )
                return
            # Keep text+picture together in normal rich media when the native
            # Markdown image service is unavailable. Never embed a broken link.
            logger.warning("[我老婆] QQ Markdown 图片上传暂不可用，使用普通图文。")
            text = unescape_markdown_text(text)
        try:
            await event.send(self._result(event, text, image=image, markdown=False))
        except Exception as exc:
            if not image:
                raise
            logger.warning("[我老婆] 图文发送失败，尝试发送文字：%s", type(exc).__name__)
            await event.send(self._result(event, text, markdown=False))

    def _help(self) -> str:
        return (
            "我老婆 · 使用帮助\n"
            "jrlp / 今日老婆：抽取今日老婆；当天重复查看保持不变\n"
            f"hlp / 换老婆：每天可换 {self.daily_limit} 次，首次抽取不占次数\n"
            f"结婚 / jrlp 结婚：与当前老婆结婚，维持 {self.marriage_duration} 天\n"
            "离婚 / jrlp 离婚：解除婚姻，保留当前角色\n"
            "jrlp 状态：查看模式、角色库和规则\n"
            "jrlp 下载图库：刷新角色清单；图片在抽取时下载\n"
            "jrlp force-download：管理员强制刷新清单\n"
            "jrlp 图鉴 关键词：查找角色或作品；省略关键词查看统计\n"
            "jrlp 历史：查看自己的最近 20 次抽取记录\n"
            "jrlp 重置次数 / 重置次数 / 重置换：管理员重置自己的今日更换次数\n"
            "jrlp help：查看帮助。支持 ~、.、/ 前缀。"
        )

    def _status(self) -> str:
        mode = "固定模板" if self.config.get("reply_mode") == "template" else "LLM"
        return (
            "我老婆 · 状态\n"
            f"回复模式：{mode}\n"
            f"记录范围：{self._scope_text()}\n"
            f"角色库：monbed/wife，共 {len(self.catalog.characters)} 条角色记录、{self.catalog.image_count} 张图片\n"
            f"当前可抽：{len(self.catalog.get_filtered())} 条角色记录\n"
            f"每日换老婆上限：{self.daily_limit} 次（首次抽取不计入）\n"
            f"新婚姻维持：{self.marriage_duration} 天\n"
            f"跨天时区：{self.timezone.key}\n"
            "同角色多图只占一个抽取名额。"
        )

    def _catalog_text(self, query: str, markdown: bool = False) -> str:
        if not query:
            return self._status() + "\n发送 jrlp 图鉴 角色名或作品名 可搜索。"
        term = query[:100].casefold()
        display = escape_markdown_field if markdown else str
        matches = [
            c for c in self.catalog.get_filtered() if term in f"{c.source} {c.name}".casefold()
        ]
        if not matches:
            return f"当前可抽角色中没有找到「{display(query[:100])}」。可检查作品筛选与排除设置。"
        lines = [f"找到 {len(matches)} 条记录，显示前 10 条："]
        lines.extend(f"• {display(c.name)} — {display(c.source)}" for c in matches[:10])
        return "\n".join(lines)

    async def _history_text(self, event: AstrMessageEvent) -> str:
        key = self._state_key(event)
        async with self._user_lock(key):
            now = time.time()
            day = datetime.fromtimestamp(now, self.timezone).date().isoformat()
            state = prepare_state(await self.get_kv_data(key, {}), day, now)
            await self.put_kv_data(key, state)
        if not state["history"]:
            return "还没有抽取记录，发送 jrlp 抽取今日老婆吧。"
        lines = ["你的最近抽取记录："]
        markdown = event.get_platform_name() == "qq_official" and self.config.get(
            "qq_official_markdown", True
        )
        display = escape_markdown_field if markdown else str
        for item in reversed(state["history"]):
            character = item["character"]
            action = "更换" if item.get("action") == "redraw" else "抽取"
            lines.append(
                f"• {item.get('date', '')} {action}：{display(character.get('name', '未知角色'))} — {display(character.get('source', '出处未收录'))}"
            )
        return "\n".join(lines)

    async def _download_text(self, event: AstrMessageEvent, force: bool) -> str:
        if force and not event.is_admin():
            return "强制刷新图库需要 AstrBot 管理员权限；普通刷新可发送 jrlp 下载图库。"
        now = time.monotonic()
        if now - self._last_manual_refresh < 60:
            return f"图库刚刚刷新过，请一分钟后再试。\n当前共有 {len(self.catalog.characters)} 条角色记录。"
        self._last_manual_refresh = now
        if await self.catalog.refresh(force=True):
            return f"图库清单已更新：{len(self.catalog.characters)} 条角色记录、{self.catalog.image_count} 张图片。\n图片会在抽取时下载；不需要下载整个图库。"
        return f"暂时无法更新在线清单，继续使用已有的 {len(self.catalog.characters)} 条角色记录；抽取仍可使用。"

    async def _dispatch(self, event: AstrMessageEvent) -> None:
        command = parse_command(event.message_str)
        if command is None or self._closing or self._directed_to_other(event):
            return
        if getattr(event, "_mywife_claimed", False):
            return
        event._mywife_claimed = True
        event.stop_event()
        action, argument = command
        try:
            character = None
            if action == "help":
                text = self._help()
            elif action == "status":
                text = self._status()
            elif action == "catalog":
                markdown = event.get_platform_name() == "qq_official" and self.config.get(
                    "qq_official_markdown", True
                )
                text = self._catalog_text(argument, markdown)
            elif action == "history":
                text = await self._history_text(event)
            elif action == "reset_count":
                text = await self._reset_count_text(event)
            elif action in ("download", "force_download"):
                text = await self._download_text(event, action == "force_download")
            else:
                text, character = await self._business(event, action)
        except Exception as exc:
            logger.error("[我老婆] 指令处理失败：%s", type(exc).__name__)
            await self._send(
                event, "本次操作未能完成，请稍后重试；若持续出现，请联系管理员查看我老婆插件日志。"
            )
            return
        await self._send(event, text, character)

    @filter.command("jrlp", alias={"今日老婆"})
    async def jrlp(self, event: AstrMessageEvent) -> None:
        """抽取今日老婆；支持状态、结婚、离婚、下载、图鉴、历史、帮助。"""
        await self._dispatch(event)

    @filter.command("hlp", alias={"换老婆"})
    async def hlp(self, event: AstrMessageEvent) -> None:
        """更换今日老婆，首次抽取不计入每日更换次数。"""
        await self._dispatch(event)

    @filter.command("重置次数", alias={"重置换"})
    async def reset_count(self, event: AstrMessageEvent) -> None:
        """AstrBot 管理员重置自己的当前范围今日换老婆次数。"""
        await self._dispatch(event)

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent) -> None:
        """兼容无前缀和原有中文快捷指令。"""
        await self._dispatch(event)
