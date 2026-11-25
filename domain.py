"""Command parsing and state transitions without platform dependencies."""

from __future__ import annotations

import copy
import math
import re
from typing import Any


def parse_command(text: str) -> tuple[str, str] | None:
    text = re.sub(r"^[~./。]", "", text.strip()).strip()
    lower = text.lower()
    standalone = {
        "hlp": "redraw",
        "换老婆": "redraw",
        "结婚": "marry",
        "离婚": "divorce",
        "和今日老婆结婚": "marry",
        "与今日老婆结婚": "marry",
        "和今日老婆离婚": "divorce",
        "与今日老婆离婚": "divorce",
        "下载图库": "download",
        "强制下载图库": "force_download",
        "重置次数": "reset_count",
        "重置换": "reset_count",
    }
    if lower in standalone:
        return standalone[lower], ""
    match = re.fullmatch(r"(?:jrlp|今日老婆)(.*)", lower)
    if not match:
        return None
    remainder = match.group(1)
    # Do not intercept ordinary words such as 'jrlplugin'.
    if (
        remainder
        and not remainder[0].isspace()
        and not remainder.startswith(
            ("状态", "帮助", "结婚", "离婚", "下载", "强制下载", "图鉴", "历史", "重置")
        )
    ):
        return None
    sub = remainder.strip()
    if not sub:
        return "draw", ""
    aliases = {
        "status": "status",
        "状态": "status",
        "help": "help",
        "帮助": "help",
        "-h": "help",
        "--help": "help",
        "marry": "marry",
        "结婚": "marry",
        "divorce": "divorce",
        "离婚": "divorce",
        "download": "download",
        "下载": "download",
        "下载图库": "download",
        "图库下载": "download",
        "force-download": "force_download",
        "强制下载": "force_download",
        "强制下载图库": "force_download",
        "history": "history",
        "历史": "history",
        "重置次数": "reset_count",
        "重置换": "reset_count",
        "reset": "reset_count",
        "reset-count": "reset_count",
    }
    if sub in aliases:
        return aliases[sub], ""
    parts = sub.split(maxsplit=1)
    first = parts[0]
    if first in ("历史", "history"):
        return "history", parts[1] if len(parts) > 1 else ""
    if first in ("图鉴", "catalog"):
        original_sub = text[len(text) - len(sub) :]
        parts = original_sub.split(maxsplit=1)
        return "catalog", parts[1] if len(parts) > 1 else ""
    return "help", ""


def prepare_state(raw: Any, day: str, now: float) -> dict[str, Any]:
    state = copy.deepcopy(raw) if isinstance(raw, dict) else {}
    if not isinstance(state.get("wife"), dict) or not state["wife"].get("id"):
        state["wife"] = None
    marriage = state.get("marriage")
    if not isinstance(marriage, dict) or not isinstance(marriage.get("wife"), dict):
        state["marriage"] = None
    else:
        try:
            started = float(marriage["started_at"])
            expires = float(marriage["expires_at"])
            duration = int(marriage["duration_days"])
            if (
                not all(math.isfinite(n) for n in (started, expires))
                or duration < 1
                or expires <= started
            ):
                raise ValueError("Invalid marriage period")
            if not marriage["wife"].get("id") or expires <= now:
                state["marriage"] = None
            else:
                marriage.update(started_at=started, expires_at=expires, duration_days=duration)
        except (KeyError, TypeError, ValueError, OverflowError):
            state["marriage"] = None
    if state.get("date") != day:
        state.update(date=day, wife=None, redraw_count=0)
    try:
        state["redraw_count"] = max(0, int(state.get("redraw_count", 0)))
    except (ValueError, TypeError, OverflowError):
        state["redraw_count"] = 0
    state["history"] = (
        [
            item
            for item in state.get("history", [])
            if isinstance(item, dict) and isinstance(item.get("character"), dict)
        ][-20:]
        if isinstance(state.get("history"), list)
        else []
    )
    return state


def marriage_info(marriage: dict[str, Any], now: float) -> dict[str, int]:
    return {
        "days": max(1, int((now - marriage["started_at"]) // 86400) + 1),
        "remain": max(0, math.ceil((marriage["expires_at"] - now) / 86400)),
        "duration": int(marriage.get("duration_days", 7)),
    }


def change_state(
    state: dict[str, Any],
    action: str,
    now: float,
    limit: int,
    duration: int,
    choose: Any,
) -> tuple[str, dict[str, Any] | None, dict[str, int]]:
    marriage = state.get("marriage")
    wife = state.get("wife")
    values = {"limit": limit, "duration": duration}
    if marriage and action in ("draw", "marry"):
        return "married", marriage["wife"], marriage_info(marriage, now)
    if action == "redraw" and marriage:
        return "blocked", marriage["wife"], values
    if action == "divorce":
        if not marriage:
            return "not_married", None, values
        state["marriage"] = None
        state["wife"] = copy.deepcopy(marriage["wife"])
        return "divorce", state["wife"], values
    if action == "marry":
        if not wife:
            return "no_wife", None, values
        state["marriage"] = {
            "wife": copy.deepcopy(wife),
            "started_at": now,
            "expires_at": now + duration * 86400,
            "duration_days": duration,
        }
        return "marry", wife, values
    if action == "draw" and wife:
        return "again", wife, values
    if action == "redraw":
        if not wife:
            return "no_wife", None, values
        if state["redraw_count"] >= limit:
            return "limit", wife, values
    selected = choose(wife.get("id") if wife else None)
    if selected is None:
        return ("no_alternative" if wife and action == "redraw" else "empty"), wife, values
    state["wife"] = selected
    if action == "redraw":
        state["redraw_count"] += 1
    state["history"].append(
        {"date": state["date"], "action": action, "character": copy.deepcopy(selected)}
    )
    state["history"] = state["history"][-20:]
    return ("redraw" if action == "redraw" else "draw"), selected, values
