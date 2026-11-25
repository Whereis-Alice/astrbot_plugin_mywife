from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from botpy.message import C2CMessage, DirectMessage, GroupMessage, Message

from domain import parse_command
from qq_media import send_qq_markdown
from replies import quick_actions

CONTENT = "> test\n\n![角色图片 #10px #20px](qqbot-media://image/v1/test)\n"


class Response:
    def __init__(self, status: int = 200, document: dict | None = None):
        self.status = status
        self.document = document if document is not None else {"id": "reply-id"}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def json(self, **_):
        return self.document


class Session:
    def __init__(self, *responses: Response | Exception):
        self.calls = []
        self.responses = iter(responses)

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


def make_event(*responses, platform="qq_official", source_type=GroupMessage):
    source = source_type(
        api=None,
        event_id="event-1",
        data={
            "id": "source-id",
            "group_openid": "group-id",
            "guild_id": "guild-id",
            "channel_id": "channel-id",
            "author": {"id": "owner-id", "member_openid": "owner-id", "user_openid": "owner-id"},
        },
    )
    session = Session(*responses)
    http = SimpleNamespace(
        is_sandbox=True,
        check_session=AsyncMock(),
        _headers={"Authorization": "fake test authorization"},
        _session=session,
    )
    event = SimpleNamespace(
        get_platform_name=lambda: platform,
        get_sender_id=lambda: "owner-id",
        bot=SimpleNamespace(api=SimpleNamespace(_http=http)),
        message_obj=SimpleNamespace(raw_message=source),
        _has_send_oper=False,
    )
    return event, session


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source_type", "path", "v2"),
    [
        (GroupMessage, "/v2/groups/group-id/messages", True),
        (C2CMessage, "/v2/users/owner-id/messages", True),
        (Message, "/channels/channel-id/messages", False),
        (DirectMessage, "/dms/guild-id/messages", False),
    ],
)
async def test_posts_a_single_native_card_with_sender_only_command_buttons(source_type, path, v2):
    event, session = make_event(Response(), platform="qq_official_webhook", source_type=source_type)
    assert await send_qq_markdown(event, CONTENT, quick_actions("draw")) is True
    ((method, url, kwargs),) = session.calls
    assert method == "POST"
    assert url == "https://sandbox.api.sgroup.qq.com" + path
    assert not kwargs["allow_redirects"]
    payload = kwargs["json"]
    assert payload["msg_id"] == "source-id"
    assert payload["markdown"] == {"content": CONTENT}
    assert payload.get("msg_type") == (2 if v2 else None)
    assert ("msg_seq" in payload) == v2
    (row,) = payload["keyboard"]["content"]["rows"]
    buttons = row["buttons"]
    assert [button["render_data"]["label"] for button in buttons] == ["结婚", "换老婆", "历史"]
    for button, action in zip(buttons, ("marry", "redraw", "history"), strict=True):
        wire = button["action"]
        assert wire["type"] == 2 and wire["enter"] is True
        assert wire["permission"] == {"type": 0, "specify_user_ids": ["owner-id"]}
        assert parse_command(wire["data"]) == (action, "")
    assert event._has_send_oper


@pytest.mark.asyncio
async def test_rejected_keyboard_preserves_image_and_retries_markdown_with_commands():
    event, session = make_event(Response(403, {"code": 12345}), Response())
    assert await send_qq_markdown(event, CONTENT, quick_actions("draw")) is True
    original, fallback = (call[2]["json"] for call in session.calls)
    assert "keyboard" in original
    assert "keyboard" not in fallback
    assert original["msg_seq"] == fallback["msg_seq"]
    assert fallback["markdown"]["content"].startswith(CONTENT)
    assert "`jrlp 历史`" in fallback["markdown"]["content"]


@pytest.mark.asyncio
async def test_two_explicit_native_rejections_allow_plain_image_fallback():
    event, session = make_event(Response(403, {"code": 1}), Response(400, {"code": 2}))
    assert await send_qq_markdown(event, CONTENT, quick_actions("draw")) is False
    assert len(session.calls) == 2
    assert not event._has_send_oper


@pytest.mark.asyncio
async def test_timeout_does_not_risk_duplicate_delivery():
    event, session = make_event(TimeoutError())
    with pytest.raises(TimeoutError):
        await send_qq_markdown(event, CONTENT, quick_actions("draw"))
    assert len(session.calls) == 1


@pytest.mark.asyncio
async def test_other_platforms_and_unsupported_raw_sources_keep_normal_dispatch():
    event, session = make_event(platform="aiocqhttp")
    assert await send_qq_markdown(event, CONTENT, quick_actions("draw")) is None
    event.get_platform_name = lambda: "qq_official"
    event.message_obj.raw_message = object()
    assert await send_qq_markdown(event, CONTENT, quick_actions("draw")) is None
    assert not session.calls


def test_suggested_actions_fit_the_result_and_do_not_end_marriages():
    assert [label for label, _ in quick_actions("draw", can_redraw=False)] == ["结婚", "历史"]
    assert [label for label, _ in quick_actions("limit")] == ["结婚", "历史"]
    assert [label for label, _ in quick_actions("married")] == ["历史"]
    assert [label for label, _ in quick_actions("no_wife")] == ["今日老婆"]
