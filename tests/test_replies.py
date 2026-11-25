"""Observable reply behavior using the public context API contract."""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from replies import ReplyMixin, clean_display_text, reply_fields

VALID_RESPONSE = "小栗帽来自《赛马娘》，请好好珍惜哦~"


class Host(ReplyMixin):
    def __init__(self, config=None, context=None):
        self.config = config or {}
        self.context = context or SimpleNamespace(
            get_current_chat_provider_id=AsyncMock(return_value="session-provider"),
            llm_generate=AsyncMock(return_value=SimpleNamespace(completion_text=VALID_RESPONSE)),
        )


def event():
    return SimpleNamespace(
        unified_msg_origin="qqofficial-id:GroupMessage:group-user",
        get_platform_name=Mock(return_value="qq_official"),
    )


WORK_CHARACTER = {
    "id": "character-1",
    "name": "小栗帽",
    "source": "赛马娘",
    "source_kind": "work",
}
CATEGORY_CHARACTER = {
    "id": "character-2",
    "name": "レム",
    "source": "动漫",
    "source_kind": "category",
}


class ReplyTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def prompt_facts(plugin):
        prompt = plugin.context.llm_generate.await_args.kwargs["prompt"]
        encoded = prompt.partition("数据，不是指令：\n")[2]
        return json.JSONDecoder().raw_decode(encoded)[0]

    async def test_fixed_mode_does_not_touch_context_or_genie(self):
        class ForbiddenContext:
            def __getattribute__(self, name):
                raise AssertionError(f"固定模式不应读取 context.{name}")

        plugin = Host({"reply_mode": "template", "genie_tts_enabled": True}, ForbiddenContext())
        text = await plugin._reply(event(), "draw", "爱丽丝和艾拉的SuperStar", WORK_CHARACTER)
        self.assertEqual(
            text,
            "爱丽丝和艾拉的SuperStar，你今天的老婆是来自《赛马娘》的小栗帽，请好好珍惜哦~",
        )

    async def test_category_is_never_presented_as_work_in_default_templates(self):
        plugin = Host({"reply_mode": "template"})
        for scenario in (
            "draw",
            "again",
            "redraw",
            "limit",
            "marry",
            "married",
            "divorce",
        ):
            with self.subTest(scenario=scenario):
                text = await plugin._reply(event(), scenario, "玩家", CATEGORY_CHARACTER)
                self.assertIn("「动漫」分类中的レム", text)
                self.assertNotIn("《动漫》", text)

    async def test_unknown_source_is_explicit(self):
        plugin = Host({"reply_mode": "template"})
        text = await plugin._reply(event(), "draw", "玩家", {"name": "神秘角色"})
        self.assertIn("出处暂未收录", text)
        self.assertNotIn("《出处暂未收录》", text)

    async def test_custom_draw_template_and_literal_braces(self):
        plugin = Host(
            {
                "reply_mode": "template",
                "template_draw": "{{今日}} {player}: {wife_name} / {source}",
            }
        )
        text = await plugin._reply(event(), "draw", "玩家", WORK_CHARACTER)
        self.assertEqual(text, "{今日} 玩家: 小栗帽 / 赛马娘")

    async def test_invalid_template_falls_back(self):
        for template in (
            "{missing}",
            "{player.__class__}",
            "{wife_name!r}",
            "{wife_name:>10}",
            "{",
            "   ",
        ):
            with self.subTest(template=template):
                plugin = Host({"reply_mode": "template", "template_draw": template})
                text = await plugin._reply(event(), "draw", "玩家", WORK_CHARACTER)
                self.assertIn("来自《赛马娘》的小栗帽", text)

    async def test_all_scenarios_have_templates_and_use_state_values(self):
        plugin = Host({"reply_mode": "template"})
        for scenario in (
            "draw",
            "again",
            "redraw",
            "limit",
            "marry",
            "married",
            "divorce",
            "not_married",
            "no_wife",
            "blocked",
            "empty",
            "no_alternative",
        ):
            with self.subTest(scenario=scenario):
                self.assertTrue(await plugin._reply(event(), scenario, "玩家", WORK_CHARACTER))
        text = await plugin._reply(event(), "married", "玩家", WORK_CHARACTER, days=3, remain=4)
        self.assertIn("3 天", text)
        self.assertIn("4 天", text)
        text = await plugin._reply(event(), "limit", "玩家", WORK_CHARACTER, limit=9)
        self.assertIn("9 次", text)

    async def test_no_alternative_explains_unchanged_result_and_no_charge(self):
        plugin = Host({"reply_mode": "template"})
        text = await plugin._reply(event(), "no_alternative", "玩家", WORK_CHARACTER)
        self.assertIn("只有来自《赛马娘》的小栗帽可抽取", text)
        self.assertIn("没有消耗更换次数", text)
        model_plugin = Host()
        await model_plugin._reply(event(), "no_alternative", "玩家", WORK_CHARACTER)
        self.assertIn(
            "告知角色未变，本次没有消耗更换次数",
            model_plugin.context.llm_generate.await_args.kwargs["prompt"],
        )

    async def test_no_alternative_state_transition_preserves_character_and_counter(
        self,
    ):
        from domain import change_state, prepare_state

        state = prepare_state(
            {"date": "2026-11-25", "wife": WORK_CHARACTER, "redraw_count": 2},
            "2026-11-25",
            100,
        )
        scenario, character, values = change_state(
            state, "redraw", 100, 5, 7, lambda previous: None
        )
        self.assertEqual(scenario, "no_alternative")
        self.assertEqual(state["wife"], WORK_CHARACTER)
        self.assertEqual(state["redraw_count"], 2)
        self.assertEqual(state["history"], [])
        text = await Host({"reply_mode": "template"})._reply(
            event(), scenario, "玩家", character, **values
        )
        self.assertIn("本次没有消耗更换次数", text)

    async def test_markdown_escapes_dynamic_fields_preserves_template_format(self):
        plugin = Host(
            {
                "reply_mode": "template",
                "qq_official_markdown": True,
                "template_draw": "# 今日老婆\n\n**{player}**\n\n- {wife_name}\n- {source}\n{source_phrase}",
            }
        )
        character = {
            "name": "角色_[链接](网址)*",
            "source": "作品_[来源](网址)*",
            "source_kind": "work",
        }
        text = await plugin._reply(event(), "draw", "昵称_[点我](网址)*", character)
        self.assertTrue(text.startswith("# 今日老婆\n\n**"))
        self.assertIn("**昵称\\_\\[点我\\]\\(网址\\)\\***", text)
        self.assertIn("\n- 角色\\_\\[链接\\]\\(网址\\)\\*", text)
        self.assertIn("\n- 作品\\_\\[来源\\]\\(网址\\)\\*", text)
        self.assertIn("来自《作品\\_\\[来源\\]\\(网址\\)\\*》的", text)

    async def test_plain_text_and_other_platforms_preserve_dynamic_text(self):
        for platform, enabled in (("qq_official", False), ("aiocqhttp", True)):
            with self.subTest(platform=platform, enabled=enabled):
                plugin = Host({"reply_mode": "template", "qq_official_markdown": enabled})
                current_event = event()
                current_event.get_platform_name.return_value = platform
                text = await plugin._reply(
                    current_event, "draw", "昵称_[点我](网址)*", WORK_CHARACTER
                )
                self.assertIn("昵称_[点我](网址)*", text)

    async def test_llm_receives_raw_fields_and_output_keeps_markdown(self):
        plugin = Host({"qq_official_markdown": True})
        plugin.context.llm_generate.return_value = SimpleNamespace(
            completion_text="**来自《赛马娘》的小栗帽**"
        )
        text = await plugin._reply(event(), "draw", "昵称_[点我](网址)*", WORK_CHARACTER)
        self.assertEqual(text, "**来自《赛马娘》的小栗帽**")
        self.assertIn(
            '"player": "昵称_[点我](网址)*"',
            plugin.context.llm_generate.await_args.kwargs["prompt"],
        )

    async def test_llm_output_is_not_replaced_or_appended_to(self):
        plugin = Host()
        text = await plugin._reply(event(), "draw", "玩家", WORK_CHARACTER)
        self.assertEqual(text, VALID_RESPONSE)
        self.assertEqual(plugin.context.llm_generate.await_count, 1)
        kwargs = plugin.context.llm_generate.await_args.kwargs
        self.assertEqual(kwargs["chat_provider_id"], "session-provider")
        self.assertEqual(kwargs["contexts"], [])
        self.assertEqual(kwargs["image_urls"], [])
        self.assertIn('"name": "小栗帽"', kwargs["prompt"])
        self.assertIn('"source": "赛马娘"', kwargs["prompt"])

    async def test_explicit_provider_skips_session_provider_lookup(self):
        plugin = Host({"provider_id": "chosen-provider"})
        await plugin._reply(event(), "draw", "玩家", WORK_CHARACTER)
        plugin.context.get_current_chat_provider_id.assert_not_awaited()
        self.assertEqual(
            plugin.context.llm_generate.await_args.kwargs["chat_provider_id"],
            "chosen-provider",
        )

    async def test_each_scenario_only_sends_applicable_numeric_facts(self):
        expected_keys = {
            "draw": set(),
            "again": set(),
            "redraw": set(),
            "limit": {"limit"},
            "marry": {"duration"},
            "married": {"days", "remain", "duration"},
            "divorce": set(),
            "not_married": set(),
            "no_wife": set(),
            "blocked": set(),
            "empty": set(),
            "no_alternative": set(),
        }
        supplied = {"limit": 9, "days": 3, "remain": 4, "duration": 11}
        for scenario, applicable in expected_keys.items():
            with self.subTest(scenario=scenario):
                plugin = Host()
                character = (
                    None if scenario in {"not_married", "no_wife", "empty"} else WORK_CHARACTER
                )
                await plugin._reply(event(), scenario, "玩家", character, **supplied)
                facts = self.prompt_facts(plugin)
                self.assertEqual(set(facts) - {"player", "character", "is_married"}, applicable)
                self.assertEqual(facts["is_married"], scenario in {"marry", "married", "blocked"})
                for key in applicable:
                    self.assertEqual(facts[key], supplied[key])

    async def test_ordinary_again_has_no_default_quota_or_marriage_numbers(self):
        plugin = Host()
        await plugin._reply(event(), "again", "玩家", WORK_CHARACTER)
        self.assertEqual(
            self.prompt_facts(plugin),
            {
                "player": "玩家",
                "is_married": False,
                "character": {"name": "小栗帽", "source": "赛马娘", "source_kind": "work"},
            },
        )

    async def test_existing_marriage_prompt_keeps_saved_duration_after_config_change(self):
        from domain import change_state, prepare_state

        now = 100 + 86400
        state = prepare_state(
            {
                "date": "2026-11-25",
                "wife": WORK_CHARACTER,
                "marriage": {
                    "wife": WORK_CHARACTER,
                    "started_at": 100,
                    "expires_at": 100 + 7 * 86400,
                    "duration_days": 7,
                },
            },
            "2026-11-25",
            now,
        )
        scenario, character, values = change_state(
            state, "draw", now, 20, 365, lambda previous: None
        )
        plugin = Host({"marriage_duration": 365})
        await plugin._reply(event(), scenario, "玩家", character, **values)
        facts = self.prompt_facts(plugin)
        self.assertEqual(facts["duration"], 7)
        self.assertEqual(facts["days"], 2)
        self.assertEqual(facts["remain"], 6)
        self.assertTrue(facts["is_married"])
        self.assertNotIn("limit", facts)

    async def test_category_and_unknown_source_rules_reach_model(self):
        plugin = Host()
        await plugin._reply(event(), "draw", "玩家", CATEGORY_CHARACTER)
        prompt = plugin.context.llm_generate.await_args.kwargs["prompt"]
        self.assertIn('"source_kind": "category"', prompt)
        self.assertIn("不能声称它是作品", prompt)
        self.assertIn("不要推测或编造出处", prompt)

    async def test_empty_or_reasoning_only_model_output_uses_template(self):
        plugin = Host()
        for completion in (
            "",
            "   ",
            "<think>仅有思考</think>",
            "<think>尚未结束",
            "<zh> </zh>",
        ):
            with self.subTest(completion=completion):
                plugin.context.llm_generate.return_value = SimpleNamespace(
                    completion_text=completion
                )
                text = await plugin._reply(event(), "draw", "玩家", WORK_CHARACTER)
                self.assertIn("来自《赛马娘》的小栗帽", text)

    async def test_missing_or_incorrect_character_facts_replace_whole_reply(self):
        plugin = Host()
        for completion in (
            "你今天的老婆是小栗帽。",
            "你抽到了来自《赛马娘》的艾拉。",
            "你今天的老婆是来自《原神》的小栗帽。",
            "<think>赛马娘的小栗帽</think>恭喜你抽到了艾拉。",
        ):
            with self.subTest(completion=completion):
                plugin.context.llm_generate.return_value = SimpleNamespace(
                    completion_text=completion
                )
                text = await plugin._reply(event(), "draw", "玩家", WORK_CHARACTER)
                self.assertEqual(text, "玩家，你今天的老婆是来自《赛马娘》的小栗帽，请好好珍惜哦~")

    async def test_category_reply_must_include_category_marker(self):
        plugin = Host()
        plugin.context.llm_generate.return_value = SimpleNamespace(
            completion_text="你抽到了动漫中的レム！"
        )
        text = await plugin._reply(event(), "draw", "玩家", CATEGORY_CHARACTER)
        self.assertIn("「动漫」分类中的レム", text)
        valid = "你抽到了动漫分类中的レム！"
        plugin.context.llm_generate.return_value = SimpleNamespace(completion_text=valid)
        self.assertEqual(await plugin._reply(event(), "draw", "玩家", CATEGORY_CHARACTER), valid)

    async def test_fact_validation_accepts_unicode_case_whitespace_and_japanese_names(self):
        cases = (
            (
                {"name": "ＡＬＩＣＥ", "source": "ＳＴＡＲ", "source_kind": "work"},
                "alice 来自 star。",
            ),
            (
                {"name": "小 栗 帽", "source": "赛 马 娘", "source_kind": "work"},
                "小栗帽来自赛马娘。",
            ),
            (
                {"name": "レム", "source": "Re:ゼロ", "source_kind": "work"},
                "你抽到了来自《Re:ゼロ》的レム。",
            ),
        )
        for character, valid in cases:
            with self.subTest(character=character):
                plugin = Host()
                plugin.context.llm_generate.return_value = SimpleNamespace(completion_text=valid)
                self.assertEqual(await plugin._reply(event(), "draw", "玩家", character), valid)

    async def test_invalid_reply_is_not_sent_to_genie(self):
        plugin = Host({"genie_tts_enabled": True})
        genie = SimpleNamespace(voice_plugin_reply=AsyncMock(return_value=VALID_RESPONSE))
        plugin.context.get_all_stars = Mock(
            return_value=[SimpleNamespace(name="genie", activated=True, star_cls=genie)]
        )
        plugin.context.llm_generate.return_value = SimpleNamespace(
            completion_text="<zh>你抽到了艾拉</zh><ja>アイラだよ</ja>"
        )
        text = await plugin._reply(event(), "draw", "玩家", WORK_CHARACTER)
        self.assertIn("来自《赛马娘》的小栗帽", text)
        genie.voice_plugin_reply.assert_not_awaited()

    async def test_provider_failure_uses_template(self):
        plugin = Host()
        plugin.context.llm_generate.side_effect = RuntimeError("provider unavailable")
        text = await plugin._reply(event(), "redraw", "玩家", WORK_CHARACTER)
        self.assertIn("你的新老婆", text)

    async def test_timeout_cancels_pending_generation_and_uses_template(self):
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def slow_generation(**kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        plugin = Host({"llm_timeout_seconds": 0.01})
        plugin.context.llm_generate.side_effect = slow_generation
        text = await plugin._reply(event(), "draw", "玩家", WORK_CHARACTER)
        self.assertTrue(started.is_set())
        self.assertTrue(cancelled.is_set())
        self.assertIn("小栗帽", text)

    async def test_plugin_cancellation_propagates(self):
        plugin = Host()
        plugin.context.llm_generate.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await plugin._reply(event(), "draw", "玩家", WORK_CHARACTER)

    async def test_disabled_persona_in_conversation_never_falls_back_to_default(self):
        plugin = Host()
        manager = SimpleNamespace(
            get_default_persona_v3=AsyncMock(return_value={"prompt": "不该生效"})
        )
        plugin.context.persona_manager = manager
        plugin.context.conversation_manager = SimpleNamespace(
            get_curr_conversation_id=AsyncMock(return_value="conversation"),
            get_conversation=AsyncMock(return_value=SimpleNamespace(persona_id="[%None]")),
        )
        await plugin._reply(event(), "draw", "玩家", WORK_CHARACTER)
        manager.get_default_persona_v3.assert_not_awaited()
        self.assertIsNone(plugin.context.llm_generate.await_args.kwargs["system_prompt"])

    async def test_disabled_persona_in_v416_config_never_uses_default(self):
        plugin = Host()
        manager = SimpleNamespace(
            get_default_persona_v3=AsyncMock(return_value={"prompt": "不该生效"})
        )
        plugin.context.persona_manager = manager
        plugin.context.get_config = Mock(
            return_value={"provider_settings": {"default_personality": "[%None]"}}
        )
        await plugin._reply(event(), "draw", "玩家", WORK_CHARACTER)
        manager.get_default_persona_v3.assert_not_awaited()
        self.assertIsNone(plugin.context.llm_generate.await_args.kwargs["system_prompt"])

    async def test_disabled_persona_in_modern_resolver_never_uses_default(self):
        plugin = Host()
        manager = SimpleNamespace(
            resolve_selected_persona=AsyncMock(return_value=("[%None]", None, None, False)),
            get_default_persona_v3=AsyncMock(return_value={"prompt": "不该生效"}),
        )
        plugin.context.persona_manager = manager
        await plugin._reply(event(), "draw", "玩家", WORK_CHARACTER)
        manager.get_default_persona_v3.assert_not_awaited()
        self.assertIsNone(plugin.context.llm_generate.await_args.kwargs["system_prompt"])

    async def test_v4294_resolver_gets_conversation_and_provider_settings(self):
        calls = []

        async def resolve_selected_persona(
            *, umo, conversation_persona_id, platform_name, provider_settings=None
        ):
            calls.append((umo, conversation_persona_id, platform_name, provider_settings))
            return ("isla", {"prompt": "艾拉的人设"}, None, False)

        plugin = Host()
        plugin.context.persona_manager = SimpleNamespace(
            resolve_selected_persona=resolve_selected_persona
        )
        plugin.context.conversation_manager = SimpleNamespace(
            get_curr_conversation_id=AsyncMock(return_value="conversation"),
            get_conversation=AsyncMock(return_value=SimpleNamespace(persona_id="isla")),
        )
        settings = {"custom_setting": True}
        plugin.context.get_config = Mock(return_value={"provider_settings": settings})
        text = await plugin._reply(event(), "draw", "玩家", WORK_CHARACTER)
        self.assertEqual(text, VALID_RESPONSE)
        self.assertEqual(calls[0][1:], ("isla", "qq_official", settings))
        self.assertEqual(
            plugin.context.llm_generate.await_args.kwargs["system_prompt"], "艾拉的人设"
        )

    async def test_forced_session_persona_has_sdk_priority_over_conversation_none(self):
        plugin = Host()
        resolver = AsyncMock(return_value=("forced", {"prompt": "强制会话人设"}, "forced", False))
        plugin.context.persona_manager = SimpleNamespace(resolve_selected_persona=resolver)
        plugin.context.conversation_manager = SimpleNamespace(
            get_curr_conversation_id=AsyncMock(return_value="conversation"),
            get_conversation=AsyncMock(return_value=SimpleNamespace(persona_id="[%None]")),
        )
        await plugin._reply(event(), "draw", "玩家", WORK_CHARACTER)
        self.assertEqual(resolver.await_args.kwargs["conversation_persona_id"], "[%None]")
        self.assertEqual(
            plugin.context.llm_generate.await_args.kwargs["system_prompt"],
            "强制会话人设",
        )

    async def test_installed_sdk_signatures_accept_actual_reply_calls(self):
        if importlib.util.find_spec("astrbot") is None:
            self.skipTest("AstrBot SDK not installed; covered in SDK validation environment")
        # Use the public entrypoint first; a direct persona_mgr import creates
        # a circular import in AstrBot 4.29.4's API initialization.
        from astrbot.api.star import Context
        from astrbot.core.persona_mgr import PersonaManager

        resolver_signature = inspect.signature(PersonaManager.resolve_selected_persona)
        generation_signature = inspect.signature(Context.llm_generate)
        calls = []

        async def resolver(**kwargs):
            resolver_signature.bind(object(), **kwargs)
            calls.append(kwargs)
            return ("isla", {"prompt": "真实SDK契约人设"}, None, False)

        async def generation(**kwargs):
            generation_signature.bind(object(), **kwargs)
            return SimpleNamespace(completion_text=VALID_RESPONSE)

        plugin = Host()
        plugin.context.persona_manager = SimpleNamespace(resolve_selected_persona=resolver)
        plugin.context.get_config = Mock(return_value={"provider_settings": {}})
        plugin.context.llm_generate = generation
        text = await plugin._reply(event(), "draw", "玩家", WORK_CHARACTER)
        self.assertEqual(text, VALID_RESPONSE)
        self.assertEqual(len(calls), 1)

    async def test_sdk_webchat_default_marker_is_not_looked_up_as_stored_persona(self):
        plugin = Host()
        manager = SimpleNamespace(
            resolve_selected_persona=AsyncMock(return_value=("_chatui_default_", None, None, True)),
            get_persona=AsyncMock(side_effect=ValueError("not a stored persona")),
        )
        plugin.context.persona_manager = manager
        current_event = event()
        current_event.get_platform_name.return_value = "webchat"
        text = await plugin._reply(current_event, "draw", "玩家", WORK_CHARACTER)
        self.assertEqual(text, VALID_RESPONSE)
        manager.get_persona.assert_not_awaited()
        self.assertIsNone(plugin.context.llm_generate.await_args.kwargs["system_prompt"])

    async def test_conversation_persona_and_default_persona_are_respected(self):
        plugin = Host()
        manager = SimpleNamespace(
            get_persona=AsyncMock(return_value=SimpleNamespace(system_prompt="角色人设")),
            get_default_persona_v3=AsyncMock(return_value={"prompt": "默认人设"}),
        )
        plugin.context.persona_manager = manager
        plugin.context.conversation_manager = SimpleNamespace(
            get_curr_conversation_id=AsyncMock(return_value="conversation"),
            get_conversation=AsyncMock(return_value=SimpleNamespace(persona_id="isla")),
        )
        await plugin._reply(event(), "draw", "玩家", WORK_CHARACTER)
        self.assertEqual(plugin.context.llm_generate.await_args.kwargs["system_prompt"], "角色人设")
        manager.get_default_persona_v3.assert_not_awaited()
        plugin.context.conversation_manager.get_conversation.return_value = SimpleNamespace(
            persona_id=None
        )
        await plugin._reply(event(), "draw", "玩家", WORK_CHARACTER)
        self.assertEqual(plugin.context.llm_generate.await_args.kwargs["system_prompt"], "默认人设")

    async def test_genie_is_opt_in_and_receives_no_reasoning(self):
        plugin = Host({"genie_tts_enabled": True})
        genie = SimpleNamespace(voice_plugin_reply=AsyncMock(return_value=VALID_RESPONSE))
        plugin.context.get_all_stars = Mock(
            return_value=[
                SimpleNamespace(
                    name="genie", root_dir_name="genie", activated=True, star_cls=genie
                ),
            ]
        )
        plugin.context.llm_generate.return_value = SimpleNamespace(
            completion_text=f"<think>秘密思考</think><zh>{VALID_RESPONSE}</zh><ja>ウマ娘のオグリキャップだよ</ja>",
        )
        text = await plugin._reply(event(), "draw", "玩家", WORK_CHARACTER)
        self.assertEqual(text, VALID_RESPONSE)
        self.assertEqual(
            genie.voice_plugin_reply.await_args.args[1],
            f"<zh>{VALID_RESPONSE}</zh><ja>ウマ娘のオグリキャップだよ</ja>",
        )
        plugin.config["genie_tts_enabled"] = False
        plugin.context.get_all_stars.reset_mock()
        await plugin._reply(event(), "draw", "玩家", WORK_CHARACTER)
        plugin.context.get_all_stars.assert_not_called()

    async def test_genie_failure_preserves_generated_text(self):
        plugin = Host({"genie_tts_enabled": True})
        genie = SimpleNamespace(
            voice_plugin_reply=AsyncMock(side_effect=RuntimeError("send error"))
        )
        plugin.context.get_all_stars = Mock(
            return_value=[
                SimpleNamespace(name="genie", activated=True, star_cls=genie),
            ]
        )
        text = await plugin._reply(event(), "draw", "玩家", WORK_CHARACTER)
        self.assertEqual(text, VALID_RESPONSE)


class TextCleaningTests(unittest.TestCase):
    def test_reasoning_removed_but_japanese_names_preserved(self):
        text = "<THINK>不能显示</THINK>你抽到了レム（ラム的妹妹）！"
        self.assertEqual(clean_display_text(text), "你抽到了レム（ラム的妹妹）！")

    def test_chinese_voice_display_keeps_character_names(self):
        self.assertEqual(
            clean_display_text("<zh>你抽到了レム</zh><ja>レムだよ</ja>"),
            "你抽到了レム",
        )

    def test_source_field_does_not_turn_category_into_work(self):
        fields = reply_fields("玩家", CATEGORY_CHARACTER)
        self.assertEqual(fields["source"], "动漫")
        self.assertEqual(fields["source_phrase"], "「动漫」分类中的")


if __name__ == "__main__":
    unittest.main()
