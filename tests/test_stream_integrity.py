"""Offline behavior tests: compile real proxy functions without server startup."""
import ast
import http.client
import json
import os
from pathlib import Path
import re
import time
import types
import unittest
import uuid
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / "wb_proxy.py"


def load_proxy():
    tree = ast.parse(SOURCE.read_text())
    # Function bodies only: no account discovery, filesystem writes or network.
    names = {"aggregate_stream", "iter_sse_lines", "strip_data_prefix",
             "repair_leaked_tool_calls", "estimate_tokens", "_new_id",
             "normalize_usage_cache_aliases", "_best_cached_tokens", "is_truncated_arguments",
             "_cache_observation", "_merge_usage_frame",
             "cleanup_orphan_tool_calls", "stream_responses_events",
             "_responses_usage", "stamp_namespace", "apply_namespace_to_calls",
             "_unwrap_custom_input", "restriction_is_account_level",
             "web_tools_active", "parse_dsml_tool_calls", "resolve_namespaced_name"}
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef)
             and (n.name in names or n.name.startswith("_stream_"))]
    ns = dict(json=json, os=os, time=time, uuid=uuid, re=re, http=http,
              SSE_READ_BLOCK=65536, _NS_SEP="__",
              _RESTRICTED_403_CODE_RE=re.compile(r'"code"\s*:\s*"?11140"?'),
              _RESTRICTION_403_MARKERS=("unusual activity", "账号存在异常行为", "已被限制"),
              CJK_RE=re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]"))
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(SOURCE), "exec"), ns)
    return ns


def chunk(delta=None, finish=None, **extra):
    return ("data: " + json.dumps(dict(choices=[dict(delta=delta or {},
            finish_reason=finish)], **extra)) + "\n\n").encode()


DONE = b"data: [DONE]\n\n"


class StreamIntegrityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.proxy = load_proxy()

    def aggregate(self, rows):
        return self.proxy["aggregate_stream"](rows, "logical-model", "test-id")

    def test_aggregate_requires_terminal_evidence(self):
        for rows in ([], [b": ping\n"], [chunk({"content": "partial"})],
                     [chunk({"tool_calls": [{"index": 0, "function": {
                         "name": "run", "arguments": "{}"}}]})]):
            with self.subTest(rows=rows):
                with self.assertRaisesRegex(ValueError, "terminal|empty|incomplete"):
                    self.aggregate(rows)

    def test_tool_identity_and_name_fragments(self):
        cases = [
            ([{"id": "a", "function": {"name": "lookup", "arguments": "{"}},
              {"id": "a", "function": {"name": "lookup", "arguments": "}"}}],
             ["lookup"]),
            ([{"index": 0, "id": "a", "function": {"name": "get_"}},
              {"index": 0, "function": {"name": "weather", "arguments": "{}"}}],
             ["get_weather"]),
            ([{"index": 0, "id": "a", "function": {"name": "get_"}},
              {"index": 0, "function": {"name": "get_weather", "arguments": "{}"}}],
             ["get_weather"]),
            ([{"id": "a", "function": {"name": "first", "arguments": "{"}},
              {"id": "b", "function": {"name": "second", "arguments": "{}"}},
              {"id": "a", "function": {"arguments": "}"}}], ["first", "second"]),
            ([{"id": "a", "function": {"name": "run", "arguments": "{"}},
              {"function": {"arguments": "}"}}], ["run"]),
            ([{"id": "a", "function": {"name": "run", "arguments": "{"}},
              {"index": 4, "id": "a", "function": {"arguments": " "}},
              {"index": 4, "function": {"arguments": "}"}}], ["run"]),
        ]
        for calls, expected in cases:
            with self.subTest(calls=calls):
                result = self.aggregate([chunk({"tool_calls": [c]}) for c in calls]
                                        + [chunk(finish="tool_calls"), DONE])
                out = result["choices"][0]["message"]["tool_calls"]
                self.assertEqual([c["function"]["name"] for c in out], expected)
                self.assertEqual(len(out), len(expected))
                for call in out:
                    self.assertEqual(json.loads(call["function"]["arguments"]), {})

    def test_history_drops_invalid_nameless_pairs_but_keeps_custom(self):
        bad = [
            {"id": "bad", "type": "function", "function": {"arguments": "{}"}},
            {"id": "bad", "type": "function", "function": {"name": "  "}},
        ]
        for call in bad:
            messages = [{"role": "assistant", "content": "keep", "tool_calls": [call]},
                        {"role": "tool", "tool_call_id": "bad", "content": "bad"}]
            out, changed = self.proxy["cleanup_orphan_tool_calls"](messages)
            self.assertTrue(changed)
            self.assertEqual(out, [{"role": "assistant", "content": "keep"}])
        for call in [{"id": "ok", "type": "custom", "custom": {"name": "edit", "input": "x"}},
                     {"id": "ok", "type": "function", "function": {"name": "run", "namespace": "shell"}}]:
            messages = [{"role": "assistant", "tool_calls": [call]},
                        {"role": "tool", "tool_call_id": "ok", "content": "ok"}]
            out, changed = self.proxy["cleanup_orphan_tool_calls"](messages)
            self.assertFalse(changed)
            self.assertEqual(out, messages)

    def responses(self, rows, **holder):
        frames = list(self.proxy["stream_responses_events"](rows, "logical-model", holder))
        return [json.loads(f.decode().split("data: ", 1)[1]) for f in frames]

    def test_responses_incomplete_event_matches_status(self):
        for rows, reason in [([chunk({"content": "partial"}, "length"), DONE], "max_output_tokens"),
                             ([chunk({"content": "partial"})], "upstream_stream_incomplete"),
                             ([], "upstream_stream_incomplete")]:
            with self.subTest(rows=rows):
                events = self.responses(rows)
                terminal = events[-1]
                self.assertEqual(terminal["type"], "response.incomplete")
                self.assertEqual(terminal["response"]["status"], "incomplete")
                self.assertEqual(terminal["response"]["incomplete_details"]["reason"], reason)
                self.assertNotIn("response.completed", [e["type"] for e in events])

    def test_responses_buffers_tools_until_name_and_preserves_fragments(self):
        cases = [
            ([{"index": 0, "id": "a", "function": {"arguments": "{\"input\":\"hello\"}"}},
              {"index": 0, "function": {"name": "edit"}}], "edit", "custom_tool_call"),
            ([{"id": "a", "function": {"name": "get_"}},
              {"id": "a", "function": {"name": "weather", "arguments": "{}"}}],
             "get_weather", "function_call"),
            ([{"id": "a", "function": {"name": "run", "arguments": "{"}},
              {"id": "a", "function": {"name": "run", "arguments": "}"}}], "run", "function_call"),
        ]
        for calls, name, kind in cases:
            with self.subTest(name=name):
                events = self.responses([chunk({"tool_calls": [c]}) for c in calls]
                                        + [chunk(finish="tool_calls"), DONE],
                                        custom_names=["edit"], namespace_map={name: "shell"})
                added = [e["item"] for e in events if e["type"] == "response.output_item.added"]
                self.assertEqual(len(added), 1)
                self.assertEqual((added[0]["name"], added[0]["type"], added[0]["namespace"]),
                                 (name, kind, "shell"))
                final = events[-1]["response"]["output"]
                self.assertEqual(len(final), 1)
                self.assertEqual(final[0]["name"], name)
                self.assertEqual(final[0]["call_id"], "a")
                if kind == "custom_tool_call":
                    self.assertEqual(final[0]["input"], "hello")
                else:
                    self.assertEqual(final[0]["arguments"], "{}")

    def test_responses_never_completes_nameless_tool(self):
        events = self.responses([chunk({"tool_calls": [{"index": 0, "id": "a",
                                 "function": {"arguments": "{}"}}]}), DONE])
        self.assertEqual(events[-1]["type"], "response.incomplete")
        self.assertFalse(any(o["type"] in ("function_call", "custom_tool_call")
                             for o in events[-1]["response"]["output"]))

    def test_late_call_id_keeps_buffered_arguments(self):
        rows = [chunk({"tool_calls": [{"index": 0, "function": {"arguments": "{"}}]}),
                chunk({"tool_calls": [{"index": 0, "id": "late", "function": {"name": "run", "arguments": "}"}}]}),
                chunk(finish="tool_calls"), DONE]
        call = self.aggregate(rows)["choices"][0]["message"]["tool_calls"][0]
        self.assertEqual((call["id"], call["function"]["arguments"]), ("late", "{}"))
        events = self.responses(rows)
        call = events[-1]["response"]["output"][0]
        self.assertEqual((call["call_id"], call["arguments"]), ("late", "{}"))

    def test_empty_terminal_only_stream_is_not_a_completion(self):
        for rows in ([DONE], [chunk(usage={"total_tokens": 4}), DONE]):
            # chunk helper includes an empty choice: role/usage-only isn't output.
            with self.subTest(rows=rows):
                with self.assertRaisesRegex(ValueError, "empty"):
                    self.aggregate(rows)
                self.assertEqual(self.responses(rows)[-1]["type"], "response.incomplete")

    def test_empty_tool_placeholder_preserves_no_tool_stop(self):
        rows = [chunk({"content": "ok", "tool_calls": [{"index": 0, "function": {}}]}, "stop"), DONE]
        result = self.aggregate(rows)["choices"][0]
        self.assertEqual(result["finish_reason"], "stop")
        self.assertNotIn("tool_calls", result["message"])
        self.assertEqual(self.responses(rows)[-1]["type"], "response.completed")

    def test_proxy_delegates_restriction_to_accounts_with_http_status(self):
        seen = []
        def classify(code, detail):
            seen.append((code, detail))
            return detail == "account restriction"
        accounts = types.SimpleNamespace(restriction_is_account_level=classify)
        with patch.dict(self.proxy, wb_accounts=accounts):
            for detail, expected in [("account restriction", True),
                                     ('{"code":11140,"msg":"model denied"}', False),
                                     ("content moderation", False)]:
                self.assertEqual(self.proxy["restriction_is_account_level"](detail), expected)
        self.assertEqual(seen, [(403, "account restriction"),
                               (403, '{"code":11140,"msg":"model denied"}'),
                               (403, "content moderation")])

    def test_proxy_version_identifiers_are_release_version(self):
        tree = ast.parse(SOURCE.read_text())
        version_values = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == "server_version":
                        version_values.append(ast.literal_eval(node.value))
            if isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values):
                    if (isinstance(key, ast.Constant) and key.value == "version"
                            and isinstance(value, ast.Constant) and isinstance(value.value, str)):
                        version_values.append(value.value)
        self.assertEqual(len(version_values), 2)
        self.assertTrue(all("0.2.0" in value for value in version_values), version_values)

    def test_legacy_function_call_argument_continuations_are_retained(self):
        rows = [chunk({"function_call": {"name": "run", "arguments": "{"}}),
                chunk({"function_call": {"arguments": "\"x\":1}"}}),
                chunk(finish="function_call"), DONE]
        call = self.aggregate(rows)["choices"][0]["message"]["tool_calls"][0]
        self.assertEqual(call["function"], {"name": "run", "arguments": '{"x":1}'})

    def test_responses_usage_after_done_and_finish_without_done(self):
        usage = {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5}
        events = self.responses([chunk({"content": "ok"}, "stop")])
        self.assertEqual(events[-1]["type"], "response.completed")
        events = self.responses([chunk({"content": "ok"}), DONE, chunk(usage=usage)])
        self.assertEqual(events[-1]["response"]["usage"]["total_tokens"], 5)
        with patch.dict(os.environ, WB_STOP_AT_SSE_DONE="1"):
            events = self.responses([chunk({"content": "ok"}), DONE, chunk(usage=usage)])
        self.assertNotEqual(events[-1]["response"]["usage"]["total_tokens"], 5)

    def test_responses_drops_truncated_tool_before_added(self):
        events = self.responses([chunk({"tool_calls": [{"index": 0, "id": "a",
                                 "function": {"name": "run", "arguments": "{"}}]}, "length"), DONE])
        self.assertEqual(events[-1]["type"], "response.incomplete")
        self.assertFalse(any(e["type"].startswith("response.function_call_arguments") for e in events))

    def test_internal_tools_stay_internal_with_late_fragmented_name(self):
        tools = types.SimpleNamespace(is_internal_tool=lambda name: name == "web_search",
                                      WEB_FETCH_NAME="web_fetch", query_args=lambda args: args.get("q"),
                                      url_arg=lambda args: args.get("url"))
        holder = {"base_body": {"_web_tools": True}}
        rows = [chunk({"tool_calls": [{"id": "a", "function": {"arguments": '{"q":"test"}'}}]}),
                chunk({"tool_calls": [{"id": "a", "function": {"name": "web_"}}]}),
                chunk({"tool_calls": [{"id": "a", "function": {"name": "search"}}]}),
                chunk(finish="tool_calls"), DONE]
        with patch.dict(self.proxy, wb_webtools=tools):
            frames = list(self.proxy["stream_responses_events"](rows, "model", holder))
        events = [json.loads(f.decode().split("data: ", 1)[1]) for f in frames]
        self.assertTrue(holder["suppress_completion"])
        self.assertEqual(holder["internal_calls"], [{"name": "web_search", "arguments": '{"q":"test"}'}])
        self.assertFalse(any(e.get("item", {}).get("type") == "function_call" for e in events))
        self.assertNotIn("response.completed", [e["type"] for e in events])

    def test_aggregate_terminal_and_post_done_usage_compatibility(self):
        usage = dict(prompt_tokens=2, completion_tokens=3, total_tokens=5)
        for rows in ([chunk({"content": "ok"}, "stop")],
                     [chunk({"content": "ok"}), DONE, chunk(usage=usage)]):
            result = self.aggregate(rows)
            self.assertEqual(result["choices"][0]["finish_reason"], "stop")
            self.assertEqual(result["model"], "logical-model")
        self.assertEqual(result["usage"]["total_tokens"], 5)


if __name__ == "__main__":
    unittest.main()
