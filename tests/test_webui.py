"""Exercise the plugin through AstrBot's native authenticated extension route."""

from __future__ import annotations

import asyncio
from importlib import import_module
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import jwt
import pytest
from astrbot.api.star import Context
from astrbot.dashboard.api.plugins import dashboard_plugin_extension_route
from astrbot.dashboard.responses import ApiError
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from test_main import PACKAGE

Dashboard = import_module(f"{PACKAGE}.webui").Dashboard
WifeStore = import_module(f"{PACKAGE}.storage").WifeStore
PLUGIN = "astrbot_plugin_mywife"
PATH = f"/api/plug/{PLUGIN}/ledger"
DAY, NOW = "2026-11-26", 1_795_689_600.0
KEY, OTHER = ("user:session:" + c * 64 for c in "ab")


class RegistrationContext:
    register_web_api = Context.register_web_api

    def __init__(self):
        self.registered_web_apis = []


@pytest.fixture
def dashboard(tmp_path, monkeypatch):
    store = WifeStore(tmp_path / "ledger.sqlite3")

    async def seed():
        user = {"display_name": "艾拉", "user_id": "player-a"}

        def choice(previous):
            return {
                "id": "更换" if previous else "初选",
                "name": "蔷薇" if previous else "爱乃",
                "source": "视觉小说",
            }

        for action in ["draw", "redraw", "marry"]:
            await store.change(KEY, user, DAY, NOW, action, 5, 7, choice)
        await store.change(OTHER, {"display_name": "Bob"}, DAY, NOW, "draw", 5, 7, choice)

    asyncio.run(seed())
    plugin = SimpleNamespace(
        context=RegistrationContext(),
        store=store,
        timezone=ZoneInfo("Asia/Shanghai"),
        daily_limit=5,
    )
    page = Dashboard(plugin, PLUGIN)
    monkeypatch.setattr(page, "_local_now", lambda: (DAY, NOW))
    page.register()
    app = FastAPI()
    app.state.jwt_secret = "test-only-dashboard-signing-secret-for-webui"
    app.state.core_lifecycle = SimpleNamespace(star_context=plugin.context)

    @app.exception_handler(ApiError)
    async def api_error(_, exc):
        return JSONResponse({"message": exc.message}, status_code=exc.status_code)

    app.add_api_route(
        "/api/plug/{plugin_path:path}", dashboard_plugin_extension_route, methods=["GET", "POST"]
    )
    token = jwt.encode({"username": "dashboard-admin"}, app.state.jwt_secret, algorithm="HS256")
    with TestClient(app) as client:
        yield page, client, {"Authorization": f"Bearer {token}"}


def test_dashboard_requires_login_and_supports_filtered_history_pagination(dashboard):
    _, client, headers = dashboard
    assert client.get(f"{PATH}/history").status_code == 401
    overview = client.get(f"{PATH}/overview", headers=headers)
    assert overview.headers["cache-control"] == "no-store"
    assert overview.json()["marriages"] == 1
    assert overview.json()["today_draws"] == 3

    params = {"player_key": KEY, "action": "draws", "q": "视觉小说", "page": 2, "size": 1}
    page = client.get(f"{PATH}/history", params=params, headers=headers)
    assert page.status_code == 200
    result = page.json()
    assert result["total"] == 2 and result["page"] == 2
    assert [(row["action"], row["character"]["name"]) for row in result["items"]] == [
        ("draw", "爱乃")
    ]
    assert client.get(f"{PATH}/history?size=101", headers=headers).status_code == 400
    assert client.get(f"{PATH}/history?action=unknown", headers=headers).status_code == 400


def test_reset_is_post_only_and_preserves_role_marriage_and_history(dashboard):
    _, client, headers = dashboard
    reset = f"{PATH}/reset-count"
    assert client.get(reset, headers=headers).json()["status"] == "error"
    assert client.post(reset, json={"player_key": KEY}).status_code == 401
    assert client.post(reset, json={"player_key": "unknown"}, headers=headers).status_code == 400
    assert (
        client.post(reset, json={"player_key": "user:" + "c" * 64}, headers=headers).status_code
        == 404
    )

    before = client.get(f"{PATH}/players?q=player-a", headers=headers).json()["items"][0]
    assert before["redraw_count"] == 1
    assert client.post(reset, json={"player_key": KEY}, headers=headers).json()["redraw_count"] == 0
    after = client.get(f"{PATH}/players?q=player-a", headers=headers).json()["items"][0]
    assert after["marriage"] == before["marriage"]
    assert after["wife"] == before["wife"]
    assert after["draws"] == before["draws"] == 2
    assert client.get(f"{PATH}/history", headers=headers).json()["total"] == 4


def test_dashboard_groups_filter_shared_player_history_by_event_location(dashboard):
    page, client, headers = dashboard
    shared = "user:" + "d" * 64
    identity = {"scope": "user", "platform": "qq_official", "platform_id": "leaf"}

    async def seed():
        for day, conversation, name in (
            ("2026-11-24", {"group_id": "101", "group_name": "雏鸟文艺部"}, "鸢泽美咲"),
            ("2026-11-25", {"group_id": "202", "group_name": "飞行社"}, "总羽爱乃"),
            (DAY, {"conversation_type": "private"}, "艾拉"),
        ):
            await page.plugin.store.change(
                shared,
                {**identity, **conversation},
                day,
                NOW,
                "draw",
                5,
                7,
                lambda _: {"id": name, "name": name},
            )

    asyncio.run(seed())
    groups = f"{PATH}/groups"
    assert client.get(groups).status_code == 401
    response = client.get(groups, params={"q": "飞行"}, headers=headers)
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["items"][0]["group_id"] == "202"
    all_groups = client.get(groups, headers=headers).json()
    assert all_groups["total"] == 4
    rows = {row["group_name"] or row["conversation_type"]: row for row in all_groups["items"]}
    literary = rows["雏鸟文艺部"]
    assert literary["player_count"] == literary["history_count"] == 1
    assert rows["unknown"]["history_count"] == 4
    assert rows["private"]["history_count"] == 1

    params = {"player_key": shared, "group_key": literary["group_key"]}
    history = client.get(f"{PATH}/history", params=params, headers=headers).json()
    assert history["total"] == 1
    assert [item["character"]["name"] for item in history["items"]] == ["鸢泽美咲"]
    assert history["items"][0]["group_name"] == "雏鸟文艺部"
    del params["player_key"]
    players = client.get(f"{PATH}/players", params=params, headers=headers).json()
    assert [item["key"] for item in players["items"]] == [shared]
    assert client.get(f"{PATH}/history?group_key=bad", headers=headers).status_code == 400
    assert client.get(f"{PATH}/players?group_key=bad", headers=headers).status_code == 400

    alias = f"{PATH}/group-alias"
    rename = {"group_key": literary["group_key"], "alias": "  四重奏  "}
    assert client.post(alias, json=rename).status_code == 401
    assert (
        client.post(alias, json={**rename, "alias": "a" * 101}, headers=headers).status_code == 400
    )
    assert (
        client.post(alias, json={**rename, "group_key": "private"}, headers=headers).status_code
        == 404
    )
    changed = client.post(alias, json=rename, headers=headers)
    assert changed.json()["group_name"] == "四重奏"
    assert changed.json()["reported_group_name"] == "雏鸟文艺部"
    assert client.get(groups, params={"q": "四重"}, headers=headers).json()["total"] == 1
    for view in ("history", "players"):
        item = client.get(f"{PATH}/{view}", params=params, headers=headers).json()["items"][0]
        assert (item["group_name"], item["group_alias"], item["reported_group_name"]) == (
            "四重奏",
            "四重奏",
            "雏鸟文艺部",
        )
    restored = client.post(alias, json={**rename, "alias": ""}, headers=headers)
    assert restored.json()["group_name"] == "雏鸟文艺部"


def test_close_does_not_unregister_handlers_from_new_plugin_instance(dashboard):
    old, client, headers = dashboard
    newer = Dashboard(old.plugin, PLUGIN)
    newer.register()
    old.close()
    assert client.get(f"{PATH}/overview", headers=headers).status_code == 200
    newer.close()
    assert client.get(f"{PATH}/overview", headers=headers).json()["status"] == "error"
