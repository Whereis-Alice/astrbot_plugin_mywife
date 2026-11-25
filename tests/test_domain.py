from __future__ import annotations

import copy
import unittest

from domain import change_state, marriage_info, parse_command, prepare_state

A = {"id": "A!Alice", "name": "Alice", "source": "A", "source_kind": "work"}
B = {"id": "B!Isla", "name": "Isla", "source": "B", "source_kind": "work"}


class DomainTests(unittest.TestCase):
    def test_original_commands_and_no_false_word_trigger(self):
        commands = {
            "/jrlp": "draw",
            ".今日老婆": "draw",
            "~hlp": "redraw",
            "换老婆": "redraw",
            "结婚": "marry",
            "jrlp结婚": "marry",
            "与今日老婆离婚": "divorce",
            "jrlp状态": "status",
            "jrlp 下载图库": "download",
            "jrlp help": "help",
            "jrlp force-download": "force_download",
            "jrlp 历史": "history",
        }
        for text, action in commands.items():
            with self.subTest(text=text):
                self.assertEqual(parse_command(text), (action, ""))
        self.assertEqual(parse_command("jrlp 图鉴 BLACKSOULS"), ("catalog", "BLACKSOULS"))
        for text in ("jrlplugin", "我想换老婆", "今天可以结婚吗", "hello"):
            self.assertIsNone(parse_command(text))

    def test_first_draw_does_not_consume_redraw_and_limit_is_exact(self):
        state = prepare_state({}, "2026-11-25", 100)

        def choose(previous):
            return copy.deepcopy(B if previous == A["id"] else A)

        scenario, _, _ = change_state(state, "draw", 100, 2, 7, choose)
        self.assertEqual(scenario, "draw")
        self.assertEqual(state["redraw_count"], 0)
        for _ in range(2):
            self.assertEqual(change_state(state, "redraw", 100, 2, 7, choose)[0], "redraw")
        previous = copy.deepcopy(state)
        self.assertEqual(change_state(state, "redraw", 100, 2, 7, choose)[0], "limit")
        self.assertEqual(state, previous)

    def test_no_other_role_never_consumes_an_attempt(self):
        state = prepare_state({"date": "2026-11-25", "wife": A}, "2026-11-25", 100)
        self.assertEqual(
            change_state(state, "redraw", 100, 5, 7, lambda _: None)[0], "no_alternative"
        )
        self.assertEqual(state["redraw_count"], 0)
        self.assertEqual(state["wife"], A)

    def test_repeat_draw_and_marriage_do_not_redraw(self):
        state = prepare_state({"date": "2026-11-25", "wife": A}, "2026-11-25", 100)

        def fail(_):
            self.fail("Unexpected random draw")

        self.assertEqual(change_state(state, "draw", 100, 5, 7, fail)[0], "again")
        self.assertEqual(change_state(state, "marry", 100, 5, 7, fail)[0], "marry")
        self.assertEqual(change_state(state, "redraw", 101, 5, 7, fail)[0], "blocked")
        self.assertEqual(change_state(state, "marry", 101, 5, 365, fail)[0], "married")
        self.assertEqual(state["marriage"]["duration_days"], 7)

    def test_marriage_survives_midnight_until_its_exact_expiry(self):
        state = prepare_state({"date": "2026-11-25", "wife": A}, "2026-11-25", 100)
        change_state(state, "marry", 100, 5, 7, lambda _: None)
        next_day = prepare_state(state, "2026-11-26", 100 + 86400)
        self.assertIsNone(next_day["wife"])
        self.assertEqual(next_day["redraw_count"], 0)
        self.assertEqual(next_day["marriage"]["wife"], A)
        self.assertEqual(marriage_info(next_day["marriage"], 100 + 86400)["remain"], 6)
        expired = prepare_state(next_day, "2026-12-02", 100 + 7 * 86400)
        self.assertIsNone(expired["marriage"])

    def test_divorce_retains_current_role_without_new_draw(self):
        state = prepare_state({"date": "2026-11-25", "wife": A}, "2026-11-25", 100)
        change_state(state, "marry", 100, 5, 7, lambda _: None)
        next_day = prepare_state(state, "2026-11-26", 200)
        self.assertEqual(change_state(next_day, "divorce", 200, 5, 7, lambda _: None)[0], "divorce")
        self.assertEqual(next_day["wife"], A)
        self.assertIsNone(next_day["marriage"])

    def test_corrupted_saved_state_is_normalized_without_mutating_input(self):
        raw = {
            "date": "2026-11-25",
            "redraw_count": "bad",
            "history": [None, 1],
            "wife": None,
            "marriage": {"wife": A, "expires_at": "9999999999"},
        }
        saved = copy.deepcopy(raw)
        state = prepare_state(raw, "2026-11-25", 100)
        self.assertIsNone(state["marriage"])
        self.assertEqual(state["redraw_count"], 0)
        self.assertEqual(state["history"], [])
        self.assertEqual(raw, saved)

    def test_history_is_bounded(self):
        state = prepare_state({}, "2026-11-25", 100)
        for index in range(30):
            change_state(
                state,
                "draw" if index == 0 else "redraw",
                100,
                100,
                7,
                lambda previous: copy.deepcopy(B if previous == A["id"] else A),
            )
        self.assertEqual(len(state["history"]), 20)


if __name__ == "__main__":
    unittest.main()
