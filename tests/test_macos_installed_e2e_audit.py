from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import time
import unittest


from scripts import macos_installed_e2e_audit as audit_module


class InstalledE2EAuditTests(unittest.TestCase):
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
        )

        result = self.audit(marker, "pwd")

        self.assertEqual("pass", result["status"])
        self.assertEqual("session-pwd", result["session_id"])

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
        self.write_session("patch", marker, [self.call("apply_patch", {"patch": patch})])

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
                self.output(json.dumps({"resources": []})),
            ],
        )

        result = self.audit(marker, "namespace")

        self.assertEqual("pass", result["status"])

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


if __name__ == "__main__":
    unittest.main()
