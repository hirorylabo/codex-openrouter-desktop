from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import time
import unittest


from scripts import macos_installed_e2e_audit as audit_module


class AuditSessionTestCase(unittest.TestCase):
    """JSONL sessionを組み立てるだけの土台。testは持たない。"""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "sessions"
        self.root.mkdir()
        self.workspace = Path(self.temporary.name) / "workspace"
        self.workspace.mkdir()
        self.started_after = time.time() - 10
        self.timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    def record(self, outer_type: str, payload: dict, *, timestamp: str | None = None) -> dict:
        return {
            "timestamp": timestamp or self.timestamp,
            "type": outer_type,
            "payload": payload,
        }

    def write_session(
        self,
        name: str,
        marker: str,
        calls: list[dict],
        *,
        cwd: str | None = None,
        provider: str = "openrouter",
        originator: str = "Codex Desktop",
        complete: bool = True,
        meta_timestamp: str | None = None,
        final: str | None = None,
    ) -> Path:
        turn_id = f"turn-{name}"
        records = [
            self.record(
                "session_meta",
                {
                    "id": f"session-{name}",
                    "timestamp": meta_timestamp or self.timestamp,
                    "cwd": cwd or str(self.workspace),
                    "originator": originator,
                    "model_provider": provider,
                },
                timestamp=meta_timestamp,
            ),
            self.record("event_msg", {"type": "task_started", "turn_id": turn_id}),
            self.record(
                "event_msg",
                {"type": "user_message", "message": marker.replace("_", "\\_")},
            ),
        ]
        records.extend(self.record("response_item", call) for call in calls)
        if final is not None:
            records.append(
                self.record("event_msg", {"type": "agent_message", "message": final})
            )
        if complete:
            records.append(
                self.record("event_msg", {"type": "task_complete", "turn_id": turn_id})
            )
        path = self.root / f"{name}.jsonl"
        path.write_text(
            "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
            encoding="utf-8",
        )
        return path

    @staticmethod
    def call(name: str, arguments: dict, call_id: str = "call-1") -> dict:
        return {
            "type": "function_call",
            "name": name,
            "namespace": "functions",
            "arguments": json.dumps(arguments),
            "call_id": call_id,
        }

    @staticmethod
    def output(value: str, call_id: str = "call-1") -> dict:
        return {"type": "function_call_output", "call_id": call_id, "output": value}

    def audit(self, marker: str, gate: str, expected_content: str | None = None) -> dict:
        return audit_module.audit(
            self.root,
            self.started_after,
            str(self.workspace),
            marker,
            gate,
            expected_content,
        )


class InstalledE2EAuditTests(AuditSessionTestCase):
    def test_pwd_passes_only_with_exact_session_and_output(self) -> None:
        marker = "OR_E2E_PWD_RUN1"
        output = (
            "Chunk ID: test\nProcess exited with code 0\nOutput:\n"
            f"{self.workspace}\n"
        )
        self.write_session(
            "pwd",
            marker,
            [self.call("exec_command", {"cmd": "pwd"}), self.output(output)],
            final=str(self.workspace),
        )

        result = self.audit(marker, "pwd")

        self.assertEqual("pass", result["status"])
        self.assertEqual("session-pwd", result["session_id"])

    def test_pwd_without_matching_final_answer_fails(self) -> None:
        marker = "OR_E2E_PWD_NO_FINAL"
        output = f"Process exited with code 0\nOutput:\n{self.workspace}\n"
        self.write_session(
            "pwd-no-final",
            marker,
            [self.call("exec_command", {"cmd": "pwd"}), self.output(output)],
            final="/somewhere/else",
        )

        result = self.audit(marker, "pwd")

        self.assertEqual("fail", result["status"])
        self.assertIn("最終回答", result["reason"])

    def test_resumed_thread_with_old_meta_and_wrong_cwd_fails_immediately(self) -> None:
        marker = "OR_E2E_PWD_RESUMED"
        old = "2025-01-01T00:00:00Z"
        self.write_session(
            "resumed",
            marker,
            [self.call("exec_command", {"cmd": "pwd"})],
            cwd="/wrong/persisted/cwd",
            meta_timestamp=old,
        )

        result = self.audit(marker, "pwd")

        self.assertEqual("fail", result["status"])
        self.assertIn("session_meta.cwd", result["reason"])

    def test_duplicate_marker_sessions_fail_closed(self) -> None:
        marker = "OR_E2E_DUPLICATE"
        self.write_session("one", marker, [])
        self.write_session("two", marker, [])

        result = self.audit(marker, "pwd")

        self.assertEqual("fail", result["status"])
        self.assertIn("2件", result["reason"])

    def test_incomplete_turn_is_pending(self) -> None:
        marker = "OR_E2E_PENDING"
        self.write_session(
            "pending",
            marker,
            [self.call("exec_command", {"cmd": "pwd"})],
            complete=False,
        )

        result = self.audit(marker, "pwd")

        self.assertEqual("pending", result["status"])
        self.assertEqual("task_complete待ち", result["reason"])

    def test_extra_tool_or_retry_fails(self) -> None:
        marker = "OR_E2E_EXTRA"
        self.write_session(
            "extra",
            marker,
            [
                self.call("exec_command", {"cmd": "pwd"}, "call-1"),
                self.call("exec_command", {"cmd": "pwd"}, "call-2"),
            ],
        )

        result = self.audit(marker, "pwd")

        self.assertEqual("fail", result["status"])
        self.assertIn("2件", result["reason"])

    def test_apply_patch_passes_with_one_exact_call_and_external_readback(self) -> None:
        marker = "OR_E2E_PATCH_RUN1"
        content = "OR_TOOLBRIDGE_E2E_RUN1"
        target = self.workspace / "toolbridge-e2e.txt"
        target.write_text(content + "\n", encoding="utf-8")
        patch = "\n".join(
            (
                "*** Begin Patch",
                f"*** Add File: {target}",
                f"+{content}",
                "*** End Patch",
            )
        )
        self.write_session(
            "patch",
            marker,
            [self.call("apply_patch", {"patch": patch})],
            final=content,
        )

        result = self.audit(marker, "apply-patch", content)

        self.assertEqual("pass", result["status"])

    def test_apply_patch_shell_fallback_is_not_accepted(self) -> None:
        marker = "OR_E2E_PATCH_FALLBACK"
        content = "OR_TOOLBRIDGE_E2E_FALLBACK"
        target = self.workspace / "toolbridge-e2e.txt"
        target.write_text(content + "\n", encoding="utf-8")
        patch = "\n".join(
            (
                "*** Begin Patch",
                f"*** Add File: {target}",
                f"+{content}",
                "*** End Patch",
            )
        )
        self.write_session(
            "fallback",
            marker,
            [
                self.call("apply_patch", {"patch": patch}, "patch-call"),
                self.call("exec_command", {"cmd": f"printf x > {target}"}, "shell-call"),
            ],
        )

        result = self.audit(marker, "apply-patch", content)

        self.assertEqual("fail", result["status"])
        self.assertIn("2件", result["reason"])

    def test_namespace_passes_only_with_resources_document(self) -> None:
        marker = "OR_E2E_NAMESPACE_RUN1"
        self.write_session(
            "namespace",
            marker,
            [
                self.call("list_mcp_resources", {}),
                self.output(json.dumps({"resources": [{"uri": "a"}, {"uri": "b"}]})),
            ],
            final="2",
        )

        result = self.audit(marker, "namespace")

        self.assertEqual("pass", result["status"])

    def test_namespace_final_count_must_match_tool_output(self) -> None:
        marker = "OR_E2E_NAMESPACE_COUNT"
        self.write_session(
            "namespace-count",
            marker,
            [
                self.call("list_mcp_resources", {}),
                self.output(json.dumps({"resources": [{"uri": "a"}]})),
            ],
            final="7",
        )

        result = self.audit(marker, "namespace")

        self.assertEqual("fail", result["status"])
        self.assertIn("最終回答", result["reason"])

    def test_namespace_unavailable_is_not_replaced_or_accepted(self) -> None:
        marker = "OR_E2E_NAMESPACE_UNAVAILABLE"
        self.write_session(
            "namespace-error",
            marker,
            [
                self.call("list_mcp_resources", {}),
                self.output("node_repl is unavailable for this model"),
            ],
        )

        result = self.audit(marker, "namespace")

        self.assertEqual("fail", result["status"])
        self.assertIn("JSONではありません", result["reason"])


class DependentAndParallelGateTests(AuditSessionTestCase):
    """gate 4/5。先行結果への依存と、順序に依存しない並行callをexact監査する。"""

    TOKEN = "OR_DEP_0123456789abcdef"

    def setUp(self) -> None:
        super().setUp()
        self.source = self.workspace / audit_module.DEPENDENT_SOURCE_NAME

    def dependent_calls(self, first: str, second: str) -> list[dict]:
        return [
            self.call("exec_command", {"cmd": first}, "call-1"),
            self.output(f"Process exited with code 0\nOutput:\n{self.TOKEN}\n", "call-1"),
            self.call("exec_command", {"cmd": second}, "call-2"),
            self.output(f"Process exited with code 0\nOutput:\n{self.TOKEN}\n", "call-2"),
        ]

    def test_dependent_gate_passes_when_second_call_uses_first_result(self) -> None:
        marker = "OR_E2E_DEPENDENT_RUN1"
        self.write_session(
            "dependent",
            marker,
            self.dependent_calls(f"cat {self.source}", f"echo {self.TOKEN}"),
            final=self.TOKEN,
        )

        result = self.audit(marker, "dependent", self.TOKEN)

        self.assertEqual("pass", result["status"])

    def test_dependent_gate_rejects_second_call_that_ignores_first_result(self) -> None:
        marker = "OR_E2E_DEPENDENT_GUESS"
        self.write_session(
            "dependent-guess",
            marker,
            self.dependent_calls(f"cat {self.source}", "echo OR_DEP_guessed"),
            final=self.TOKEN,
        )

        result = self.audit(marker, "dependent", self.TOKEN)

        self.assertEqual("fail", result["status"])
        self.assertIn("2回目", result["reason"])

    def test_dependent_gate_rejects_single_call_turn(self) -> None:
        marker = "OR_E2E_DEPENDENT_SINGLE"
        self.write_session(
            "dependent-single",
            marker,
            [
                self.call("exec_command", {"cmd": f"cat {self.source}"}, "call-1"),
                self.output(f"Output:\n{self.TOKEN}\n", "call-1"),
            ],
            final=self.TOKEN,
        )

        result = self.audit(marker, "dependent", self.TOKEN)

        self.assertEqual("fail", result["status"])
        self.assertIn("1件", result["reason"])

    def parallel_calls(self, order: tuple[str, str]) -> list[dict]:
        outputs = {
            "pwd": str(self.workspace),
            f"cat {self.source}": self.TOKEN,
        }
        records: list[dict] = []
        for index, command in enumerate(order, 1):
            call_id = f"call-{index}"
            records.append(self.call("exec_command", {"cmd": command}, call_id))
            records.append(
                self.output(
                    f"Process exited with code 0\nOutput:\n{outputs[command]}\n", call_id
                )
            )
        return records

    def test_parallel_gate_is_order_independent(self) -> None:
        forward = ("pwd", f"cat {self.source}")
        for name, order in (("forward", forward), ("reverse", forward[::-1])):
            with self.subTest(order=name):
                marker = f"OR_E2E_PARALLEL_{name.upper()}"
                self.write_session(
                    f"parallel-{name}",
                    marker,
                    self.parallel_calls(order),
                    final=f"{self.workspace}\n{self.TOKEN}",
                )

                result = self.audit(marker, "parallel", self.TOKEN)

                self.assertEqual("pass", result["status"], result.get("reason"))

    def test_parallel_gate_rejects_duplicate_command(self) -> None:
        marker = "OR_E2E_PARALLEL_DUPLICATE"
        self.write_session(
            "parallel-duplicate",
            marker,
            self.parallel_calls(("pwd", "pwd")),
            final=f"{self.workspace}\n{self.TOKEN}",
        )

        result = self.audit(marker, "parallel", self.TOKEN)

        self.assertEqual("fail", result["status"])
        self.assertIn("重複", result["reason"])

    def test_parallel_gate_rejects_write_command(self) -> None:
        marker = "OR_E2E_PARALLEL_WRITE"
        self.write_session(
            "parallel-write",
            marker,
            [
                self.call("exec_command", {"cmd": "pwd"}, "call-1"),
                self.output(
                    f"Process exited with code 0\nOutput:\n{self.workspace}\n", "call-1"
                ),
                self.call(
                    "exec_command", {"cmd": f"printf x > {self.source}"}, "call-2"
                ),
                self.output("Process exited with code 0\nOutput:\n", "call-2"),
            ],
            final=f"{self.workspace}\n{self.TOKEN}",
        )

        result = self.audit(marker, "parallel", self.TOKEN)

        self.assertEqual("fail", result["status"])
        self.assertIn("想定外", result["reason"])

    def test_parallel_gate_requires_both_values_in_final_answer(self) -> None:
        marker = "OR_E2E_PARALLEL_FINAL"
        self.write_session(
            "parallel-final",
            marker,
            self.parallel_calls(("pwd", f"cat {self.source}")),
            final=str(self.workspace),
        )

        result = self.audit(marker, "parallel", self.TOKEN)

        self.assertEqual("fail", result["status"])
        self.assertIn("最終回答", result["reason"])


class HarnessContractTests(unittest.TestCase):
    def test_cli_exposes_exactly_five_gates(self) -> None:
        self.assertEqual(
            ("pwd", "apply-patch", "namespace", "dependent", "parallel"),
            audit_module.GATES,
        )

    def test_harness_prompts_never_reveal_dependent_token(self) -> None:
        script = (
            Path(__file__).resolve().parents[1] / "scripts/macos_installed_e2e.zsh"
        ).read_text(encoding="utf-8")
        prompts = [
            line
            for line in script.splitlines()
            if "print -r --" in line and "marker" in line
        ]
        self.assertTrue(prompts)
        for line in prompts:
            self.assertNotIn("E2E_DEPENDENT_CONTENT", line)


if __name__ == "__main__":
    unittest.main()
