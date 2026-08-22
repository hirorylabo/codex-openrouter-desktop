from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from codex_openrouter import toolbridge  # noqa: E402


def fixture(build: str = "6849") -> dict:
    return json.loads(
        (ROOT / f"tests/fixtures/codex-tool-wire-{build}.json").read_text(
            encoding="utf-8"
        )
    )


def request(build: str = "6849") -> dict:
    return {
        "model": "example/model",
        "input": "Use a tool.",
        "tools": fixture(build)["tools"],
        "stream": True,
    }


def event(document: dict) -> bytes:
    return b"data: " + json.dumps(document, separators=(",", ":")).encode() + b"\n\n"


class RequestBridgeTests(unittest.TestCase):
    def test_latest_and_previous_fixtures_use_the_pinned_contract(self) -> None:
        for build in ("6849", "6720"):
            with self.subTest(build=build):
                document = fixture(build)
                self.assertEqual(1, document["schema_version"])
                self.assertEqual(toolbridge.TOOL_CONTRACT_VERSION, document["tool_contract_version"])
                prepared = toolbridge.prepare_document(request(build))
                self.assertTrue(prepared.tool_map.has_tools)

    def test_function_passes_and_custom_namespace_become_plain_functions(self) -> None:
        prepared = toolbridge.prepare_document(request())
        tools = prepared.document["tools"]
        self.assertEqual("plain_status", tools[0]["name"])
        self.assertEqual("function", tools[0]["type"])
        # top-level custom は名前を保ち、strictを付けずに落とす。
        self.assertEqual("apply_patch", tools[1]["name"])
        self.assertEqual(["content"], tools[1]["parameters"]["required"])
        self.assertNotIn("strict", tools[1])
        # namespace配下は平坦化する。元のschemaはそのまま。
        self.assertEqual("functions__exec", tools[2]["name"])
        self.assertEqual(["cmd"], tools[2]["parameters"]["required"])
        self.assertNotIn("sort", prepared.document.get("provider", {}))

    def test_tool_choice_and_multi_turn_items_round_trip_to_forward_names(self) -> None:
        document = request()
        document["tool_choice"] = {"type": "custom", "name": "apply_patch"}
        document["input"] = [
            {
                "type": "custom_tool_call",
                "id": "ctc_1",
                "call_id": "call_1",
                "name": "apply_patch",
                "input": "*** Begin Patch\n*** End Patch",
            },
            {"type": "custom_tool_call_output", "call_id": "call_1", "output": "Done"},
            {
                "type": "function_call",
                "id": "fc_2",
                "call_id": "call_2",
                "namespace": "functions",
                "name": "exec",
                "arguments": '{"cmd":"pwd"}',
            },
        ]
        prepared = toolbridge.prepare_document(document)
        self.assertEqual(
            {"type": "function", "name": "apply_patch"},
            prepared.document["tool_choice"],
        )
        self.assertEqual("function_call", prepared.document["input"][0]["type"])
        self.assertEqual(
            {"content": "*** Begin Patch\n*** End Patch"},
            json.loads(prepared.document["input"][0]["arguments"]),
        )
        self.assertEqual("function_call_output", prepared.document["input"][1]["type"])
        self.assertEqual("functions__exec", prepared.document["input"][2]["name"])
        self.assertNotIn("namespace", prepared.document["input"][2])

    def test_duplicate_reserved_and_unknown_tool_shapes_fail_closed(self) -> None:
        duplicate = request()
        duplicate["tools"].append(duplicate["tools"][0])
        with self.assertRaises(toolbridge.ToolBridgeError):
            toolbridge.prepare_document(duplicate)
        # 平坦化した名前がtop-level functionと衝突する形。名前空間が潰れるので、
        # どちらを優先するか推測せずに止める。
        collision = request()
        collision["tools"][0]["name"] = "functions__exec"
        with self.assertRaises(toolbridge.ToolBridgeError):
            toolbridge.prepare_document(collision)
        unsupported = request()
        unsupported["tools"] = [{"type": "web_search"}]
        with self.assertRaises(toolbridge.ToolBridgeError):
            toolbridge.prepare_document(unsupported)

        duplicate_namespace = request()
        duplicate_namespace["tools"].append(duplicate_namespace["tools"][-1])
        with self.assertRaises(toolbridge.ToolBridgeError):
            toolbridge.prepare_document(duplicate_namespace)


class WireContractRegressionTests(unittest.TestCase):
    """実測が否定した契約を固定する。

    2026-08-22にOpenRouterへ直接測った結果:
    - `type:"custom"` をそのまま送ると tool call が一度も返らない（status 200・error なし）
    - custom→function 変換に `strict:true` + `additionalProperties:false` を付けると
      apply_patch は 0/4。外すと 3〜4/4
    根拠: task/0822-toolbridge-fix-plan.md
    """

    def custom_tool(self) -> dict:
        return {
            "type": "custom",
            "name": "apply_patch",
            "description": "Apply a unified patch.",
            "format": {
                "type": "grammar",
                "syntax": "lark",
                "definition": 'start: "*** Begin Patch" LF\n',
            },
        }

    def test_bridged_custom_tool_carries_no_strict_contract(self) -> None:
        document = {"model": "m", "input": "go", "tools": [self.custom_tool()]}
        tool = toolbridge.prepare_document(document).document["tools"][0]
        self.assertNotIn("strict", tool)
        self.assertNotIn("additionalProperties", tool["parameters"])
        self.assertEqual(["content"], tool["parameters"]["required"])
        self.assertEqual(["content"], list(tool["parameters"]["properties"]))

    def test_grammar_is_folded_into_the_description(self) -> None:
        document = {"model": "m", "input": "go", "tools": [self.custom_tool()]}
        tool = toolbridge.prepare_document(document).document["tools"][0]
        self.assertIn("Apply a unified patch.", tool["description"])
        self.assertIn("Format:", tool["description"])
        self.assertIn("```lark", tool["description"])
        self.assertIn("*** Begin Patch", tool["description"])

    def test_absent_grammar_adds_nothing(self) -> None:
        bare = self.custom_tool()
        bare.pop("format")
        document = {"model": "m", "input": "go", "tools": [bare]}
        tool = toolbridge.prepare_document(document).document["tools"][0]
        self.assertEqual("Apply a unified patch.", tool["description"])

    def test_tool_names_survive_and_namespaces_flatten(self) -> None:
        prepared = toolbridge.prepare_document(request())
        names = [tool["name"] for tool in prepared.document["tools"]]
        self.assertEqual(["plain_status", "apply_patch", "functions__exec"], names)

    def test_lite_additional_tools_engage_the_bridge(self) -> None:
        """`use_responses_lite` 形式でもBridgeが起動する。

        この形式ではtop-level `tools` が無く、tool定義は
        `input[0].additional_tools` に載る。ここでreturnしていたのが
        実機gate 2が落ちた1つ目の原因。
        """
        document = {
            "model": "m",
            "instructions": "",
            "input": [
                {
                    "type": "additional_tools",
                    "role": "developer",
                    "tools": [self.custom_tool()],
                },
                {"type": "message", "role": "user", "content": "go"},
            ],
        }
        prepared = toolbridge.prepare_document(document)
        self.assertTrue(prepared.tool_map.has_tools)
        # 変換結果は元の置き場所へ戻す。top-level tools を新設しない。
        self.assertNotIn("tools", prepared.document)
        bridged = prepared.document["input"][0]["tools"][0]
        self.assertEqual("function", bridged["type"])
        self.assertEqual("apply_patch", bridged["name"])
        self.assertNotIn("strict", bridged)

    def test_lite_and_classic_cannot_both_carry_tools(self) -> None:
        document = {
            "model": "m",
            "tools": [self.custom_tool()],
            "input": [
                {
                    "type": "additional_tools",
                    "role": "developer",
                    "tools": [self.custom_tool()],
                }
            ],
        }
        with self.assertRaises(toolbridge.ToolBridgeError):
            toolbridge.prepare_document(document)


class ResponseBridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.prepared = toolbridge.prepare_document(request())

    def test_blocking_parallel_namespace_and_custom_calls_restore(self) -> None:
        document = {
            "output": [
                {
                    "type": "function_call",
                    "id": "fc_exec",
                    "call_id": "call_exec",
                    "name": "functions__exec",
                    "arguments": '{"cmd":"pwd"}',
                    "status": "completed",
                },
                {
                    "type": "function_call",
                    "id": "fc_patch",
                    "call_id": "call_patch",
                    "name": "apply_patch",
                    "arguments": '{"content":"*** Begin Patch\\n*** End Patch"}',
                    "status": "completed",
                },
            ]
        }
        restored, summary = toolbridge.transform_response_document(
            document, self.prepared.tool_map
        )
        self.assertIsNone(summary)
        by_id = {item["id"]: item for item in restored["output"]}
        self.assertEqual("functions", by_id["fc_exec"]["namespace"])
        self.assertEqual("exec", by_id["fc_exec"]["name"])
        self.assertEqual("custom_tool_call", by_id["fc_patch"]["type"])
        self.assertEqual("apply_patch", by_id["fc_patch"]["name"])
        self.assertEqual("*** Begin Patch\n*** End Patch", by_id["fc_patch"]["input"])
        self.assertEqual("call_patch", by_id["fc_patch"]["call_id"])

    def _stream(self) -> bytes:
        patch_arguments = '{"content":"*** Begin Patch\\n*** End Patch"}'
        documents = [
            {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {
                    "type": "function_call",
                    "id": "fc_patch",
                    "call_id": "call_patch",
                    "name": "apply_patch",
                    "arguments": "",
                },
            },
            {
                "type": "response.output_item.added",
                "output_index": 1,
                "item": {
                    "type": "function_call",
                    "id": "fc_exec",
                    "call_id": "call_exec",
                    "name": "functions__exec",
                    "arguments": "",
                },
            },
            {
                "type": "response.function_call_arguments.delta",
                "item_id": "fc_patch",
                "output_index": 0,
                "sequence_number": 3,
                "delta": patch_arguments[:12],
            },
            {
                "type": "response.function_call_arguments.delta",
                "item_id": "fc_exec",
                "output_index": 1,
                "sequence_number": 4,
                "delta": '{"cmd":"pwd"}',
            },
            {
                "type": "response.function_call_arguments.delta",
                "item_id": "fc_patch",
                "output_index": 0,
                "sequence_number": 5,
                "delta": patch_arguments[12:],
            },
            {
                "type": "response.function_call_arguments.done",
                "item_id": "fc_exec",
                "output_index": 1,
                "sequence_number": 6,
                "name": "functions__exec",
                "arguments": '{"cmd":"pwd"}',
            },
            {
                "type": "response.output_item.done",
                "output_index": 1,
                "item": {
                    "type": "function_call",
                    "id": "fc_exec",
                    "call_id": "call_exec",
                    "name": "functions__exec",
                    "arguments": '{"cmd":"pwd"}',
                },
            },
            {
                "type": "response.function_call_arguments.done",
                "item_id": "fc_patch",
                "output_index": 0,
                "sequence_number": 8,
                "name": "apply_patch",
                "arguments": patch_arguments,
            },
            {
                "type": "response.output_item.done",
                "output_index": 0,
                "item": {
                    "type": "function_call",
                    "id": "fc_patch",
                    "call_id": "call_patch",
                    "name": "apply_patch",
                    "arguments": patch_arguments,
                },
            },
            {
                "type": "response.completed",
                "response": {"output": []},
                "openrouter_metadata": {
                    "attempts": [{"provider_name": "DeepInfra", "status": 200}],
                    "endpoints": [{}, {}],
                    "future_field": {"ignored": True},
                },
            },
        ]
        return b"".join(event(item) for item in documents) + b"data: [DONE]\n\n"

    def test_sse_is_identical_at_every_byte_boundary(self) -> None:
        raw = self._stream()

        def transformed(step: int) -> tuple[bytes, toolbridge.RouterSummary | None]:
            bridge = toolbridge.SSEBridge(self.prepared.tool_map)
            output: list[bytes] = []
            for offset in range(0, len(raw), step):
                output.extend(bridge.feed(raw[offset : offset + step]))
            output.extend(bridge.finish())
            return b"".join(output), bridge.summary

        baseline, baseline_summary = transformed(len(raw))
        for step in (1, 2, 3, 7, 19, 64):
            with self.subTest(step=step):
                self.assertEqual((baseline, baseline_summary), transformed(step))
        text = baseline.decode()
        self.assertIn('"type":"custom_tool_call"', text)
        self.assertIn('"type":"response.custom_tool_call_input.delta"', text)
        self.assertIn('"namespace":"functions"', text)
        self.assertNotIn("openrouter_metadata", text)
        self.assertEqual("DeepInfra", baseline_summary.provider)
        self.assertEqual(1, baseline_summary.provider_attempt)
        self.assertEqual(2, baseline_summary.candidate_count)
        self.assertEqual(200, baseline_summary.status)

    def test_unknown_malformed_and_incomplete_sse_fail_closed(self) -> None:
        unknown = toolbridge.SSEBridge(self.prepared.tool_map)
        with self.assertRaises(toolbridge.ToolBridgeError):
            unknown.feed(
                event(
                    {
                        "type": "response.output_item.added",
                        "output_index": 0,
                        "item": {
                            "type": "function_call",
                            "id": "fc_x",
                            "call_id": "call_x",
                            "name": "codex_bridge_9999",
                        },
                    }
                )
            )
        malformed = toolbridge.SSEBridge(self.prepared.tool_map)
        with self.assertRaises(toolbridge.ToolBridgeError):
            malformed.feed(b"data: {not-json}\n\n")
        incomplete = toolbridge.SSEBridge(self.prepared.tool_map)
        incomplete.feed(
            event(
                {
                    "type": "response.output_item.added",
                    "output_index": 0,
                    "item": {
                        "type": "function_call",
                        "id": "fc_patch",
                        "call_id": "call_patch",
                        "name": "apply_patch",
                    },
                }
            )
        )
        with self.assertRaises(toolbridge.ToolBridgeError):
            incomplete.finish()
        partial = toolbridge.SSEBridge(self.prepared.tool_map)
        partial.feed(b"data: {")
        with self.assertRaises(toolbridge.ToolBridgeError):
            partial.finish()

        no_done = toolbridge.SSEBridge(self.prepared.tool_map)
        no_done.feed(event({"type": "response.completed", "response": {"output": []}}))
        with self.assertRaises(toolbridge.ToolBridgeError):
            no_done.finish()

        malformed_arguments = toolbridge.SSEBridge(self.prepared.tool_map)
        malformed_arguments.feed(
            event(
                {
                    "type": "response.output_item.added",
                    "output_index": 0,
                    "item": {
                        "type": "function_call",
                        "id": "fc_plain",
                        "call_id": "call_plain",
                        "name": "plain_status",
                        "arguments": "",
                    },
                }
            )
        )
        with self.assertRaises(toolbridge.ToolBridgeError):
            malformed_arguments.feed(
                event(
                    {
                        "type": "response.function_call_arguments.done",
                        "item_id": "fc_plain",
                        "output_index": 0,
                        "name": "plain_status",
                        "arguments": "{",
                    }
                )
            )


class RouterMetadataTests(unittest.TestCase):
    def test_retry_unknown_fields_and_missing_metadata_are_nonfatal(self) -> None:
        document = {
            "output": [],
            "openrouter_metadata": {
                "attempts": [
                    {"provider_name": "First", "status": 503},
                    {"provider_name": "Second", "attempt": 2, "status": "success"},
                ],
                "candidates": [{}, {}, {}],
                "pipeline": {"must_not_be_retained": "secret-like-body"},
            },
        }
        summary = toolbridge.extract_router_metadata(document)
        self.assertEqual({}, {key: value for key, value in document.items() if key != "output"})
        self.assertEqual("Second", summary.provider)
        self.assertEqual(2, summary.provider_attempt)
        self.assertEqual(3, summary.candidate_count)
        self.assertEqual("success", summary.status)
        self.assertIsNone(toolbridge.extract_router_metadata({"output": []}))

    def test_documented_endpoints_shape_is_reduced_without_pipeline_data(self) -> None:
        document = {
            "openrouter_metadata": {
                "attempt": 2,
                "endpoints": {
                    "total": 4,
                    "available": [
                        {"provider": "First", "selected": False},
                        {"provider": "Second", "selected": True},
                    ],
                },
                "attempts": [
                    {"provider": "First", "status": 503},
                    {"provider": "Second", "status": 200},
                ],
                "pipeline": [{"type": "guardrail", "data": {"private": "discard"}}],
            }
        }
        summary = toolbridge.extract_router_metadata(document)
        self.assertEqual({}, document)
        self.assertEqual("Second", summary.provider)
        self.assertEqual(2, summary.provider_attempt)
        self.assertEqual(4, summary.candidate_count)
        self.assertEqual(200, summary.status)


class UsageTelemetryTests(unittest.TestCase):
    """responseに実在する整数のtoken数だけを取り出す。推測も再計算もしない。"""

    def test_responses_usage_shape_is_extracted(self) -> None:
        summary = toolbridge.extract_usage(
            {
                "usage": {
                    "input_tokens": 12,
                    "output_tokens": 5,
                    "input_tokens_details": {"cached_tokens": 8},
                    "total_tokens": 17,
                }
            }
        )
        self.assertEqual(12, summary.input_tokens)
        self.assertEqual(5, summary.output_tokens)
        self.assertEqual(8, summary.cached_tokens)
        self.assertEqual(
            {"input_tokens": 12, "output_tokens": 5, "cached_tokens": 8},
            summary.log_fields(),
        )

    def test_nested_response_usage_is_extracted(self) -> None:
        summary = toolbridge.extract_usage(
            {"type": "response.completed", "response": {"usage": {"output_tokens": 3}}}
        )
        self.assertEqual({"output_tokens": 3}, summary.log_fields())

    def test_unknown_or_non_integer_shapes_are_omitted(self) -> None:
        self.assertIsNone(toolbridge.extract_usage({"usage": {"input_tokens": "12"}}))
        self.assertIsNone(toolbridge.extract_usage({"usage": {"output_tokens": 1.5}}))
        self.assertIsNone(toolbridge.extract_usage({"usage": {"input_tokens": -1}}))
        self.assertIsNone(toolbridge.extract_usage({"usage": {"input_tokens": True}}))
        self.assertIsNone(toolbridge.extract_usage({"usage": {"prompt_tokens": 9}}))
        self.assertIsNone(toolbridge.extract_usage({"usage": []}))
        self.assertIsNone(toolbridge.extract_usage({}))

    def test_usage_never_carries_text_fields(self) -> None:
        summary = toolbridge.extract_usage(
            {
                "usage": {
                    "input_tokens": 4,
                    "prompt": "canary-usage-prompt",
                    "input_tokens_details": {"cached_tokens": 0, "text": "canary"},
                }
            }
        )
        self.assertEqual({"input_tokens": 4, "cached_tokens": 0}, summary.log_fields())

    def test_sse_bridge_keeps_usage_from_completed_event(self) -> None:
        prepared = toolbridge.prepare_document(
            {"model": "m", "tools": [{"type": "custom", "name": "apply_patch"}]}
        )
        bridge = toolbridge.SSEBridge(prepared.tool_map)
        bridge.feed(
            event(
                {
                    "type": "response.completed",
                    "response": {
                        "output": [],
                        "usage": {"input_tokens": 7, "output_tokens": 2},
                    },
                }
            )
        )
        bridge.feed(b"data: [DONE]\n\n")
        bridge.finish()
        self.assertEqual(
            {"input_tokens": 7, "output_tokens": 2}, bridge.usage.log_fields()
        )


class BuildAllowlistTests(unittest.TestCase):
    def test_build_allowlist_is_exactly_latest_and_previous(self) -> None:
        path = ROOT / "models/tool-wire-builds.json"
        self.assertEqual(("6849", "6720"), toolbridge.supported_builds(path))
        toolbridge.assert_supported_build(path, "6720")
        with self.assertRaises(toolbridge.ToolBridgeError):
            toolbridge.assert_supported_build(path, "7000")


if __name__ == "__main__":
    unittest.main()
