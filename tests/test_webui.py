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


def test_close_does_not_unregister_handlers_from_new_plugin_instance(dashboard):
    old, client, headers = dashboard
    newer = Dashboard(old.plugin, PLUGIN)
    newer.register()
    old.close()
    assert client.get(f"{PATH}/overview", headers=headers).status_code == 200
    newer.close()
    assert client.get(f"{PATH}/overview", headers=headers).json()["status"] == "error"
