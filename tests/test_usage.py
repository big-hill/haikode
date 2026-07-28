import unittest

from haikode.context import estimate_tokens, message_tokens
from haikode.schema import Msg, ToolCall, ToolSpec
from haikode.usage import (ContextState, Usage, UsageTracker, context_bar,
                           detail_lines, format_context, format_cost,
                           format_tokens, measure_context, summary_line)


class _StubAgent:
    """An agent-shaped object with hand-countable parts.

    Numbers are chosen so every component is exact under estimate_tokens
    (~4 chars per token) and message_tokens (+4 overhead per message).
    """

    def __init__(self):
        self.system_prompt = "S" * 40
        self.context_window = 1000
        self.messages = [
            Msg(role="user", content="U" * 20),
            Msg(role="assistant", content="A" * 8,
                tool_calls=[ToolCall(id="1", name="read", arguments={})]),
        ]
        self.specs = [ToolSpec(name="read", description="D" * 16,
                               parameters={"type": "object"})]

    def _system_message(self) -> Msg:
        return Msg(role="system", content=self.system_prompt)


class UsageTest(unittest.TestCase):
    def test_total_counts_every_billed_kind(self):
        usage = Usage(input_tokens=10, output_tokens=5, reasoning_tokens=3,
                      cache_read=2, cache_write=1)
        self.assertEqual(usage.total, 21)
        self.assertEqual(Usage().total, 0)

    def test_add_accumulates_every_field(self):
        first = Usage(1, 2, 3, 4, 5, 0.5)
        second = Usage(10, 20, 30, 40, 50, 1.25)
        merged = first.add(second)
        self.assertEqual(
            (merged.input_tokens, merged.output_tokens, merged.reasoning_tokens,
             merged.cache_read, merged.cache_write), (11, 22, 33, 44, 55))
        self.assertAlmostEqual(merged.cost, 1.75)

    def test_add_leaves_both_operands_untouched(self):
        first = Usage(1, 2, 3, 4, 5, 0.5)
        second = Usage(10, 20, 30, 40, 50, 1.25)
        merged = first.add(second)
        self.assertIsNot(merged, first)
        self.assertIsNot(merged, second)
        self.assertEqual(first, Usage(1, 2, 3, 4, 5, 0.5))
        self.assertEqual(second, Usage(10, 20, 30, 40, 50, 1.25))

    def test_add_none_returns_a_copy(self):
        first = Usage(1, 2)
        copy = first.add(None)
        self.assertEqual(copy, first)
        self.assertIsNot(copy, first)

    def test_operator_matches_add(self):
        first, second = Usage(1, 2), Usage(3, 4)
        self.assertEqual(first + second, first.add(second))

    def test_sum_of_usages(self):
        self.assertEqual(sum([Usage(1, 2), Usage(3, 4)]), Usage(4, 6))

    def test_adding_a_non_usage_is_a_type_error_not_an_attribute_error(self):
        with self.assertRaises(TypeError):
            Usage(1, 2) + 3
        with self.assertRaises(TypeError):
            Usage(1, 2).add({"input": 3})

    def test_a_recorded_total_cannot_be_edited_from_outside(self):
        tracker = UsageTracker()
        tracker.record({"input": 10})
        with self.assertRaises(Exception):
            tracker.session.input_tokens = 99999
        self.assertEqual(tracker.session, Usage(10))

    def test_from_dict_reads_provider_spellings(self):
        # The OpenAI adapter's shape.
        self.assertEqual(Usage.from_dict({"input": 7, "output": 3}),
                         Usage(input_tokens=7, output_tokens=3))
        # Raw Anthropic keys, including the cache counters.
        anthropic = Usage.from_dict({
            "input_tokens": 100, "output_tokens": 20, "reasoning": 5,
            "cache_read_input_tokens": 900, "cache_creation_input_tokens": 40})
        self.assertEqual(anthropic, Usage(100, 20, 5, 900, 40))
        # opencode nests the cache pair.
        nested = Usage.from_dict({"input": 1, "cache": {"read": 2, "write": 3}})
        self.assertEqual(nested, Usage(input_tokens=1, cache_read=2, cache_write=3))

    def test_from_dict_ignores_junk(self):
        self.assertEqual(Usage.from_dict(None), Usage())
        self.assertEqual(Usage.from_dict("nope"), Usage())
        self.assertEqual(Usage.from_dict({}), Usage())
        self.assertEqual(Usage.from_dict({"input": None, "output": "x"}), Usage())
        # A negative counter is a provider bug, not a refund.
        self.assertEqual(Usage.from_dict({"input": -5}), Usage())


class ContextStateTest(unittest.TestCase):
    def test_percent_and_remaining(self):
        state = ContextState(used=12800, window=128000)
        self.assertAlmostEqual(state.percent, 10.0)
        self.assertEqual(state.remaining, 115200)

    def test_zero_window_never_divides(self):
        state = ContextState(used=500, window=0)
        self.assertEqual(state.percent, 0.0)
        self.assertEqual(state.remaining, 0)
        self.assertEqual(state.pressure, "ok")

    def test_overflow_reports_the_true_share(self):
        # opencode's footer is an uncapped Math.round(tokens / limit * 100); a
        # history that has outgrown the window must not read as a tidy 100%.
        state = ContextState(used=200000, window=100000)
        self.assertEqual(state.percent, 200.0)
        self.assertEqual(state.remaining, 0)
        self.assertEqual(state.pressure, "critical")
        self.assertEqual(format_context(state), "200k/100k (200%)")

    def test_pressure_boundaries(self):
        window = 10000
        cases = [
            (0, "ok"),
            (5999, "ok"),      # 59.99%
            (6000, "warn"),    # exactly 60%
            (8499, "warn"),    # 84.99%
            (8500, "critical"),  # exactly 85%
            (10000, "critical"),
        ]
        for used, expected in cases:
            with self.subTest(used=used):
                self.assertEqual(ContextState(used=used, window=window).pressure,
                                 expected)


class MeasureContextTest(unittest.TestCase):
    def test_components_of_a_stub_agent(self):
        state = measure_context(_StubAgent())
        # system: 40 chars // 4 + 4 message overhead
        self.assertEqual(state.system, 14)
        # tools: "read" + 16 chars of description + '{"type": "object"}' = 38 // 4
        self.assertEqual(state.tools, 9)
        # history: (20//4 + 4) + (8//4 + "read" + "{}" + 4)
        self.assertEqual(state.history, 17)
        self.assertEqual(state.messages, 2)
        self.assertEqual(state.used, 40)
        self.assertEqual(state.window, 1000)
        self.assertEqual(state.used, state.system + state.tools + state.history)

    def test_components_match_the_shared_estimators(self):
        agent = _StubAgent()
        state = measure_context(agent)
        self.assertEqual(state.system, message_tokens(agent._system_message()))
        self.assertEqual(state.history,
                         sum(message_tokens(m) for m in agent.messages))
        self.assertEqual(state.tools, estimate_tokens(
            'read' + 'D' * 16 + '{"type": "object"}'))

    def test_bare_object_degrades_to_zero(self):
        state = measure_context(object())
        self.assertEqual(state, ContextState())
        self.assertEqual(state.percent, 0.0)
        self.assertEqual(state.pressure, "ok")

    def test_none_agent(self):
        self.assertEqual(measure_context(None), ContextState())

    def test_raising_system_message_falls_back_to_the_raw_prompt(self):
        class Broken:
            system_prompt = "P" * 80
            context_window = 500
            messages = []

            def _system_message(self):
                raise OSError("no AGENTS.md here")

        state = measure_context(Broken())
        self.assertEqual(state.system, 20)  # 80 // 4, no message overhead
        self.assertEqual(state.used, 20)
        self.assertEqual(state.window, 500)

    def test_unusable_attributes_do_not_raise(self):
        class Hostile:
            system_prompt = None
            context_window = "wide"
            messages = 7            # not iterable
            specs = [object()]      # not a ToolSpec

        state = measure_context(Hostile())
        self.assertEqual(state.window, 0)
        self.assertEqual(state.messages, 0)
        self.assertEqual(state.history, 0)
        self.assertEqual(state.system, 0)

    def test_messages_that_are_not_msgs_are_measured_not_dropped(self):
        class Loose:
            messages = ["P" * 400, {"role": "user", "content": "Q" * 800}]
            context_window = 10000

        state = measure_context(Loose())
        self.assertEqual(state.messages, 2)
        # 400//4 + 4 and 800//4 + 4: the text is counted, not reduced to a
        # single token by an unread .content attribute.
        self.assertEqual(state.history, 104 + 204)

    def test_attributes_that_raise_do_not_take_the_frame_down(self):
        class Exploding:
            context_window = 1000

            @property
            def specs(self):
                raise RuntimeError("boom")

            @property
            def messages(self):
                raise RuntimeError("boom")

            @property
            def system_prompt(self):
                raise RuntimeError("boom")

        state = measure_context(Exploding())
        self.assertEqual(state, ContextState(window=1000))

    def test_tools_mapping_is_accepted_instead_of_specs(self):
        agent = _StubAgent()
        spec = agent.specs[0]
        del agent.specs
        agent.tools = {"read": spec}
        self.assertEqual(measure_context(agent).tools, 9)

    def test_specs_mapping_prices_the_schemas_not_their_names(self):
        agent = _StubAgent()
        agent.specs = {"read": agent.specs[0]}
        self.assertEqual(measure_context(agent).tools, 9)


class UsageTrackerTest(unittest.TestCase):
    def test_run_and_session_separate_across_start_run(self):
        tracker = UsageTracker()
        tracker.record({"input": 100, "output": 10})
        self.assertEqual(tracker.run, Usage(100, 10))
        self.assertEqual(tracker.session, Usage(100, 10))

        tracker.start_run()
        self.assertEqual(tracker.run, Usage())
        self.assertEqual(tracker.session, Usage(100, 10))

        tracker.record({"input": 50, "output": 5})
        tracker.record({"input": 25, "output": 1})
        self.assertEqual(tracker.run, Usage(75, 6))
        self.assertEqual(tracker.session, Usage(175, 16))

    def test_record_returns_the_delta_only(self):
        tracker = UsageTracker()
        tracker.record({"input": 100})
        delta = tracker.record({"input": 5, "output": 2})
        self.assertEqual(delta, Usage(5, 2))

    def test_record_ignores_an_empty_payload(self):
        tracker = UsageTracker()
        tracker.record(None)
        self.assertEqual(tracker.session, Usage())

    def test_record_accepts_an_already_parsed_usage(self):
        tracker = UsageTracker()
        delta = tracker.record(Usage(input_tokens=500, output_tokens=20))
        self.assertEqual(delta, Usage(500, 20))
        self.assertEqual(tracker.session, Usage(500, 20))

    def test_reset_clears_both(self):
        tracker = UsageTracker()
        tracker.record({"input": 9, "output": 9})
        tracker.reset()
        self.assertEqual(tracker.run, Usage())
        self.assertEqual(tracker.session, Usage())

    def test_estimate_cost_with_pricing(self):
        tracker = UsageTracker()
        tracker.record({"input": 1_000_000, "output": 500_000,
                        "reasoning": 100_000, "cache_read": 2_000_000,
                        "cache_write": 250_000})
        pricing = {"input": 3.0, "output": 15.0,
                   "cache_read": 0.3, "cache_write": 3.75}
        # 3 + 7.5 + 1.5 (reasoning at the output rate) + 0.6 + 0.9375
        self.assertAlmostEqual(tracker.estimate_cost(pricing), 13.5375)

    def test_estimate_cost_uses_partial_pricing_without_guessing(self):
        tracker = UsageTracker()
        tracker.record({"input": 1_000_000, "output": 1_000_000})
        self.assertAlmostEqual(tracker.estimate_cost({"input": 2.0}), 2.0)

    def test_estimate_cost_accepts_opencodes_nested_cache_rates(self):
        # opencode normalises models.dev's flat cache_read/cache_write onto
        # Provider.Model.cost.cache.{read,write}; both spellings must price.
        tracker = UsageTracker()
        tracker.record({"input": 1_000_000, "cache_read": 1_000_000,
                        "cache_write": 1_000_000})
        nested = {"input": 3.0, "cache": {"read": 0.3, "write": 3.75}}
        self.assertAlmostEqual(tracker.estimate_cost(nested), 7.05)

    def test_estimate_cost_without_pricing_is_zero(self):
        tracker = UsageTracker()
        tracker.record({"input": 1_000_000, "output": 1_000_000})
        for pricing in (None, {}, "free", {"input": 0, "output": 0},
                        {"unknown": 5}, {"input": "cheap"}):
            with self.subTest(pricing=pricing):
                self.assertEqual(tracker.estimate_cost(pricing), 0.0)

    def test_estimate_cost_can_price_the_run_alone(self):
        tracker = UsageTracker()
        tracker.record({"input": 1_000_000})
        tracker.start_run()
        tracker.record({"input": 500_000})
        pricing = {"input": 4.0}
        self.assertAlmostEqual(tracker.estimate_cost(pricing), 6.0)
        self.assertAlmostEqual(tracker.estimate_cost(pricing, tracker.run), 2.0)


class FormatTokensTest(unittest.TestCase):
    def test_exact_output(self):
        cases = [
            (0, "0"), (1, "1"), (985, "985"), (999, "999"),
            (1000, "1k"), (1200, "1.2k"), (1234, "1.2k"), (12345, "12.3k"),
            (128000, "128k"), (999949, "999.9k"), (999950, "1M"),
            (1000000, "1M"), (1400000, "1.4M"), (12500000, "12.5M"),
            (-500, "-500"), (-1500, "-1.5k"),
        ]
        for count, expected in cases:
            with self.subTest(count=count):
                self.assertEqual(format_tokens(count), expected)

    def test_junk_is_zero(self):
        self.assertEqual(format_tokens(None), "0")
        self.assertEqual(format_tokens("many"), "0")


class FormatCostTest(unittest.TestCase):
    def test_exact_output(self):
        self.assertEqual(format_cost(0), "$0.00")
        self.assertEqual(format_cost(0.005), "$0.0050")
        self.assertEqual(format_cost(0.01), "$0.01")
        self.assertEqual(format_cost(1.2345), "$1.23")
        self.assertEqual(format_cost(12.5), "$12.50")
        self.assertEqual(format_cost(-3), "$0.00")


class FormatContextTest(unittest.TestCase):
    def test_exact_output(self):
        state = ContextState(used=12300, window=128000)
        self.assertEqual(format_context(state), "12.3k/128k (10%)")

    def test_empty_context(self):
        self.assertEqual(format_context(ContextState(used=0, window=1000)),
                         "0/1k (0%)")

    def test_unknown_window_shows_the_count_alone(self):
        self.assertEqual(format_context(ContextState(used=985)), "985")

    def test_percent_rounds_half_up(self):
        # 0.5% must not disappear the way banker's rounding would lose it.
        self.assertEqual(format_context(ContextState(used=5, window=1000)),
                         "5/1k (1%)")


class ContextBarTest(unittest.TestCase):
    def test_exact_output_per_pressure(self):
        cases = [
            (0, "[----------]"),
            (4000, "[####------]"),
            (7000, "[%%%%%%%---]"),
            (9000, "[@@@@@@@@@-]"),
            (10000, "[@@@@@@@@@@]"),
            (25000, "[@@@@@@@@@@]"),  # overflow saturates
        ]
        for used, expected in cases:
            with self.subTest(used=used):
                bar = context_bar(ContextState(used=used, window=10000), 12)
                self.assertEqual(bar, expected)

    def test_unknown_window_reads_empty(self):
        self.assertEqual(context_bar(ContextState(used=500), 12), "[----------]")

    def test_width_is_never_exceeded(self):
        state = ContextState(used=6666, window=10000)
        for width in range(3, 41):
            with self.subTest(width=width):
                self.assertEqual(len(context_bar(state, width)), width)

    def test_width_below_a_usable_meter_is_empty(self):
        state = ContextState(used=5000, window=10000)
        for width in (-10, 0, 1, 2):
            with self.subTest(width=width):
                self.assertEqual(context_bar(state, width), "")
        self.assertEqual(context_bar(state, "wide"), "")

    def test_smallest_meter(self):
        self.assertEqual(context_bar(ContextState(used=4000, window=10000), 3), "[-]")
        self.assertEqual(context_bar(ContextState(used=10000, window=10000), 3), "[@]")


class SummaryLineTest(unittest.TestCase):
    def test_exact_output(self):
        tracker = UsageTracker()
        tracker.record({"input": 4100, "output": 892})
        state = ContextState(used=12300, window=128000)
        self.assertEqual(summary_line(tracker, state),
                         "12.3k/128k (10%) - 4.1k in / 892 out")

    def test_cost_is_appended_only_when_known(self):
        tracker = UsageTracker()
        tracker.record({"input": 4100, "output": 892, "cost": 0.42})
        state = ContextState(used=12300, window=128000)
        self.assertEqual(summary_line(tracker, state),
                         "12.3k/128k (10%) - 4.1k in / 892 out - $0.42")

    def test_empty_tracker_and_unknown_window(self):
        self.assertEqual(summary_line(UsageTracker(), ContextState()),
                         "0 - 0 in / 0 out")


class DetailLinesTest(unittest.TestCase):
    def _tracker(self):
        tracker = UsageTracker()
        tracker.record({"input": 5100, "output": 608})
        tracker.start_run()
        tracker.record({"input": 4100, "output": 892})
        return tracker

    def test_exact_output(self):
        state = ContextState(used=12300, window=128000, messages=6,
                             tools=3400, system=2100, history=6800)
        self.assertEqual(detail_lines(self._tracker(), state), [
            "Window:         128k",
            "Used:           12.3k (10%)",
            "Remaining:      115.7k",
            "Pressure:       ok",
            "System prompt:  2.1k",
            "Tool schemas:   3.4k",
            "Conversation:   6.8k (6 messages)",
            "Run:            4.1k in / 892 out",
            "Session:        9.2k in / 1.5k out",
        ])

    def test_optional_rows_appear_only_when_used(self):
        tracker = UsageTracker()
        tracker.record({"input": 100, "output": 10, "reasoning": 250,
                        "cache_read": 2000, "cache_write": 30, "cost": 0.0123})
        lines = detail_lines(tracker, ContextState(used=1, window=1000))
        self.assertIn("Reasoning:      250", lines)
        self.assertIn("Cache:          2k read / 30 written", lines)
        self.assertIn("Cost:           $0.01", lines)

    def test_unknown_window_is_reported_as_such(self):
        lines = detail_lines(UsageTracker(),
                             ContextState(used=900, messages=1, history=900))
        self.assertEqual(lines[0], "Window:         (unknown)")
        # No share is claimed when there is nothing to divide by: "(0%)" here
        # would read as an empty context rather than an unmeasurable one.
        self.assertEqual(lines[1], "Used:           900")
        self.assertEqual(lines[2], "Remaining:      (unknown)")
        self.assertEqual(lines[6], "Conversation:   900 (1 message)")

    def test_missing_state_or_tracker_still_renders(self):
        self.assertEqual(summary_line(None, None), "0 - 0 in / 0 out")
        self.assertEqual(detail_lines(None, None)[0], "Window:         (unknown)")

    def test_every_line_is_a_single_row(self):
        state = ContextState(used=12300, window=128000, messages=6,
                             tools=3400, system=2100, history=6800)
        for line in detail_lines(self._tracker(), state):
            self.assertNotIn("\n", line)


if __name__ == "__main__":
    unittest.main()
