"""我老婆: daily characters from monbed/wife for AstrBot."""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Plain
from astrbot.api.star import Context, Star, StarTools

from .catalog import Catalog
from .domain import parse_command
from .image_store import ImageStore
from .qq_media import OFFICIAL_PLATFORMS, QQMediaStore, send_qq_markdown
from .qq_preview import make_qq_preview
from .rendering import qq_markdown, unescape_markdown_text
from .replies import QuickActions, ReplyMixin, escape_markdown_field, quick_actions
from .storage import WifeStore
from .webui import Dashboard

PLUGIN_NAME = "astrbot_plugin_mywife"
HISTORY_PAGE_SIZE = 10


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
        self._qq_preview_slots = asyncio.Semaphore(2)
        self.store = WifeStore(self.data_dir / "records.sqlite3", self._legacy_records)
        self._dashboard: Dashboard | None = None
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
        await self.store.initialize()
        if callable(getattr(self.context, "register_web_api", None)):
            self._dashboard = Dashboard(self, PLUGIN_NAME)
            self._dashboard.register()
        if self.config.get("auto_refresh_catalog", True):
            self._refresh_task = asyncio.create_task(self.catalog.refresh())

    async def terminate(self) -> None:
        self._closing = True
        if self._dashboard is not None:
            self._dashboard.close()
        if self._refresh_task and not self._refresh_task.done():
            self._refresh_task.cancel()
        if self._refresh_task:
            await asyncio.gather(self._refresh_task, return_exceptions=True)
        await self.qq_images.close()
        await self.images.close()

    async def _legacy_records(self) -> dict[str, Any]:
        # The public per-key KV API cannot enumerate players. Query only this
        # plugin's legacy namespace through the same preferences service.
        from astrbot.core import sp

        rows = await sp.range_get_async("plugin", self.plugin_id)
        return {
            row.key: row.value["val"]
            for row in rows
            if isinstance(row.value, dict) and "val" in row.value
        }

    def _local_now(self) -> tuple[str, float]:
        now = time.time()
        return datetime.fromtimestamp(now, self.timezone).date().isoformat(), now

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
        scope = self._scope()
        if scope == "session":
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

    def _scope(self) -> str:
        scope = self.config.get("state_scope", "session")
        return scope if scope in ("group", "user") else "session"

    def _identity(self, event: AstrMessageEvent) -> dict[str, str]:
        scope = self._scope()
        if scope == "session":
            session = str(event.unified_msg_origin)
        elif scope == "group":
            session = str(event.get_group_id() or "private")
        else:
            session = ""
        return {
            "platform": event.get_platform_name(),
            "platform_id": event.get_platform_id(),
            "scope": scope,
            "session_id": session,
            "user_id": event.get_sender_id(),
            "display_name": event.get_sender_name() or "",
        }

    def _scope_text(self) -> str:
        return {"group": "当前群聊或私聊", "user": "当前 Bot 的跨群共享范围"}.get(
            self._scope(), "当前会话"
        )

    async def _reset_count_text(self, event: AstrMessageEvent) -> str:
        # All routing paths use this guard, including the no-prefix listener.
        if not event.is_admin():
            return "重置次数仅限 AstrBot 管理员使用。"
        day, now = self._local_now()
        state = await self.store.reset_count(
            self._state_key(event), self._identity(event), day, now
        )
        if state.get("marriage"):
            return f"已重置你在{self._scope_text()}的今日换老婆次数。当前婚姻仍有效，婚姻期间不能换老婆。"
        return f"已重置你在{self._scope_text()}的今日换老婆次数，今日可更换 {self.daily_limit} 次。当前角色和历史记录已保留。"

    def _choose(self, previous: str | None) -> dict[str, Any] | None:
        candidates = [c for c in self.catalog.get_filtered() if c.id != previous]
        if not candidates:
            return None
        character = random.choice(candidates)
        record = character.to_record()
        record["image_path"] = random.choice(character.image_paths)
        return record

    async def _business(
        self, event: AstrMessageEvent, action: str
    ) -> tuple[str, dict[str, Any] | None, QuickActions]:
        day, now = self._local_now()
        limit = self.daily_limit
        scenario, character, values = await self.store.change(
            self._state_key(event),
            self._identity(event),
            day,
            now,
            action,
            limit,
            self.marriage_duration,
            self._choose,
        )
        text = await self._reply(
            event, scenario, event.get_sender_name() or "你", character, **values
        )
        return text, character, quick_actions(scenario)

    @staticmethod
    def _directed_to_other(event: AstrMessageEvent) -> bool:
        mentions = [
            str(getattr(comp, "qq", ""))
            for comp in event.get_messages()
            if comp.__class__.__name__ == "At" and str(getattr(comp, "qq", "")).lower() != "all"
        ]
        targets = {event.get_self_id()}
        if event.get_platform_name() in OFFICIAL_PLATFORMS:
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
        self,
        event: AstrMessageEvent,
        text: str,
        character: dict | None = None,
        actions: QuickActions = (),
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
        markdown = self._markdown_enabled(event)
        buttons = actions if markdown and self.config.get("qq_official_buttons", True) else ()
        if self._closing:
            return
        if image and markdown:
            # Use the scaled image itself for both Markdown and rich media.
            try:
                async with self._qq_preview_slots:
                    image = await make_qq_preview(image, self.data_dir / "qq_previews")
            except Exception as exc:
                logger.warning("[我老婆] QQ 预览生成失败：%s", type(exc).__name__)
                image = None
        if self._closing:
            return
        if markdown:
            # QQ clients can reject external GitHub/CDN pictures even when
            # the bot downloaded them successfully. Use QQ-owned media instead.
            image_url, size = None, None
            if image:
                try:
                    size = await self.images.image_dimensions(image)
                    image_url = await self.qq_images.get_image_url(event, image)
                except Exception as exc:
                    logger.warning("[我老婆] QQ 图片处理失败：%s", type(exc).__name__)
            if self._closing:
                return
            if image is None or (image_url and size):
                body = qq_markdown(text, image_url, size)
                try:
                    sent = await send_qq_markdown(event, body, buttons)
                    if sent is None:
                        # Older or custom adapters still get the SDK result.
                        await event.send(self._result(event, body, markdown=True))
                        return
                    if sent:
                        return
                except Exception as exc:
                    # QQ may already have accepted the message if the connection
                    # closed before its response arrived. Avoid a duplicate post.
                    logger.warning("[我老婆] QQ 消息送达状态未确认：%s", type(exc).__name__)
                    return
            else:
                logger.warning("[我老婆] QQ Markdown 图片上传暂不可用，使用普通图文。")
            text = unescape_markdown_text(text)
            if buttons:
                text += "\n快捷指令：" + " · ".join(command for _, command in buttons)
        try:
            await event.send(self._result(event, text, image=image, markdown=False))
        except Exception as exc:
            if not image:
                raise
            logger.warning("[我老婆] 图文发送失败，尝试发送文字：%s", type(exc).__name__)
            await event.send(self._result(event, text, markdown=False))

    def _markdown_enabled(self, event: AstrMessageEvent) -> bool:
        return event.get_platform_name() in OFFICIAL_PLATFORMS and bool(
            self.config.get("qq_official_markdown", True)
        )

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
            "jrlp 历史 [页码]：按页查看自己的抽取记录\n"
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

    async def _history_reply(
        self, event: AstrMessageEvent, argument: str = ""
    ) -> tuple[str, QuickActions]:
        if argument and (not argument.isascii() or not argument.isdecimal() or len(argument) > 7):
            return "页码请用正整数，例如 jrlp 历史 2。", quick_actions("history")
        page = int(argument or "1")
        if not 1 <= page <= 1_000_000:
            return "页码须在 1 到 1000000 之间。", quick_actions("history")
        result = await self.store.history(
            self._state_key(event),
            action="draws",
            limit=HISTORY_PAGE_SIZE,
            offset=(page - 1) * HISTORY_PAGE_SIZE,
        )
        if not result["total"]:
            return "还没有抽取记录，发送 jrlp 抽取今日老婆吧。", quick_actions("history")
        pages = (result["total"] + HISTORY_PAGE_SIZE - 1) // HISTORY_PAGE_SIZE
        if not result["items"]:
            return f"没有第 {page} 页记录。共 {pages} 页，发送 jrlp 历史 返回第一页。", (
                ("第一页", "jrlp 历史"),
            )
        lines = [f"你的抽取记录 · 第 {page}/{pages} 页（共 {result['total']} 条）"]
        display = escape_markdown_field if self._markdown_enabled(event) else str
        for item in result["items"]:
            character = item["character"]
            action = "更换" if item.get("action") == "redraw" else "抽取"
            lines.append(
                f"• {item.get('date', '')} {action}：{display(character.get('name', '未知角色'))} — {display(character.get('source', '出处未收录'))}"
            )
        actions = quick_actions("history")
        if page > 1:
            actions += (("上一页", f"jrlp 历史 {page - 1}"),)
        if page < pages:
            actions += (("下一页", f"jrlp 历史 {page + 1}"),)
            lines.append(f"发送 jrlp 历史 {page + 1} 查看下一页。")
        return "\n".join(lines), actions

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
            actions = quick_actions(action)
            if action == "help":
                text = self._help()
            elif action == "status":
                text = self._status()
            elif action == "catalog":
                text = self._catalog_text(argument, self._markdown_enabled(event))
            elif action == "history":
                text, actions = await self._history_reply(event, argument)
            elif action == "reset_count":
                text = await self._reset_count_text(event)
            elif action in ("download", "force_download"):
                text = await self._download_text(event, action == "force_download")
            else:
                text, character, actions = await self._business(event, action)
        except Exception as exc:
            logger.error("[我老婆] 指令处理失败：%s", type(exc).__name__)
            await self._send(
                event, "本次操作未能完成，请稍后重试；若持续出现，请联系管理员查看我老婆插件日志。"
            )
            return
        await self._send(event, text, character, actions)

    @filter.command("jrlp", alias={"今日老婆"})
    async def jrlp(self, event: AstrMessageEvent) -> None:
        """抽取今日老婆；支持状态、结婚、离婚、下载、图鉴、历史分页、帮助。"""
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
