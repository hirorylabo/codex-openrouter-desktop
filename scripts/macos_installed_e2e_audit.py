#!/usr/bin/env python3
"""導入済みDesktop E2Eの1 turnをJSONLからfail-closedで監査する。"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import sys
from typing import Any


PENDING = 2


def _normalized(text: object) -> str:
    return text.replace("\\_", "_") if isinstance(text, str) else ""


def _timestamp(value: object) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _read_records(path: Path, marker: str) -> list[dict[str, Any]]:
    records = []
    with path.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                if marker in _normalized(line):
                    raise ValueError(f"marker行が不正なJSONです: {path}:{number}") from exc
                continue
            if isinstance(record, dict):
                records.append(record)
    return records


def _payload(record: dict[str, Any]) -> dict[str, Any]:
    payload = record.get("payload")
    return payload if isinstance(payload, dict) else {}


def _is_event(record: dict[str, Any], event_type: str) -> bool:
    return record.get("type") == "event_msg" and _payload(record).get("type") == event_type


def _marker_turns(
    sessions_root: Path, marker: str, started_after: float
) -> list[tuple[Path, list[dict[str, Any]], int]]:
    matches = []
    for path in sessions_root.rglob("*.jsonl"):
        try:
            if path.stat().st_mtime < started_after - 1:
                continue
            records = _read_records(path, marker)
        except OSError:
            continue
        for index, record in enumerate(records):
            if not _is_event(record, "user_message"):
                continue
            message = _normalized(_payload(record).get("message"))
            if marker not in message:
                continue
            when = _timestamp(record.get("timestamp"))
            if when is None or when < started_after:
                continue
            if message.count(marker) != 1:
                raise ValueError("unique markerが同じuser message内で重複しています")
            matches.append((path, records, index))
    return matches


def _turn_records(
    records: list[dict[str, Any]], marker_index: int
) -> tuple[str, list[dict[str, Any]]] | None:
    turn_id = None
    for record in records[:marker_index]:
        if _is_event(record, "task_started"):
            turn_id = _payload(record).get("turn_id")
        elif _is_event(record, "task_complete"):
            turn_id = None
    if not isinstance(turn_id, str) or not turn_id:
        raise ValueError("markerに対応するtask_startedがありません")

    turn = []
    for record in records[marker_index + 1 :]:
        if _is_event(record, "user_message") or _is_event(record, "task_started"):
            raise ValueError("対象turnの完了前に次のturnが始まりました")
        turn.append(record)
        if _is_event(record, "task_complete"):
            if _payload(record).get("turn_id") != turn_id:
                raise ValueError("task_completeのturn_idが一致しません")
            return turn_id, turn
    return None


def _tool_calls(turn: list[dict[str, Any]]) -> list[dict[str, Any]]:
    calls = []
    for record in turn:
        if record.get("type") != "response_item":
            continue
        payload = _payload(record)
        if payload.get("type") in {"function_call", "custom_tool_call"}:
            calls.append(payload)
    return calls


def _arguments(call: dict[str, Any]) -> dict[str, Any]:
    raw = call.get("arguments", call.get("input"))
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        raise ValueError("tool argumentsがありません")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("tool argumentsが不正なJSONです") from exc
    if not isinstance(parsed, dict):
        raise ValueError("tool argumentsがobjectではありません")
    return parsed


def _call_output(turn: list[dict[str, Any]], call_id: object) -> str:
    outputs = []
    for record in turn:
        if record.get("type") != "response_item":
            continue
        payload = _payload(record)
        if payload.get("type") not in {"function_call_output", "custom_tool_call_output"}:
            continue
        if payload.get("call_id") == call_id:
            outputs.append(payload.get("output"))
    if len(outputs) != 1 or not isinstance(outputs[0], str):
        raise ValueError("対応するtool outputがちょうど1件ではありません")
    return outputs[0]


def _require_call(call: dict[str, Any], name: str) -> dict[str, Any]:
    if call.get("name") != name or call.get("namespace") != "functions":
        actual = f"{call.get('namespace')}.{call.get('name')}"
        raise ValueError(f"期待したfunctions.{name}ではありません: {actual}")
    return _arguments(call)


def _audit_pwd(call: dict[str, Any], turn: list[dict[str, Any]], workspace: str) -> None:
    if _require_call(call, "exec_command") != {"cmd": "pwd"}:
        raise ValueError("pwd gateのargumentsがexact一致しません")
    output = _call_output(turn, call.get("call_id"))
    lines = output.splitlines()
    if "Output:" in lines:
        output_lines = lines[lines.index("Output:") + 1 :]
        if "Process exited with code 0" not in lines:
            raise ValueError("pwdがexit 0ではありません")
    else:
        output_lines = lines
    if output_lines != [workspace]:
        raise ValueError(f"pwd outputがexact workspaceではありません: {output_lines!r}")


def _audit_patch(
    call: dict[str, Any], turn: list[dict[str, Any]], workspace: str, expected_marker: str
) -> None:
    del turn
    marker_file = Path(workspace) / "toolbridge-e2e.txt"
    expected_content = f"{expected_marker}\n"
    expected_patch = "\n".join(
        (
            "*** Begin Patch",
            f"*** Add File: {marker_file}",
            f"+{expected_content.rstrip()}",
            "*** End Patch",
        )
    )
    arguments = _require_call(call, "apply_patch")
    if set(arguments) != {"patch"} or arguments["patch"].rstrip("\n") != expected_patch:
        raise ValueError("apply_patchの内容または対象pathがexact一致しません")
    if marker_file.is_symlink() or not marker_file.is_file():
        raise ValueError("apply_patch後のmarker fileがありません")
    if marker_file.read_bytes() != expected_content.encode():
        raise ValueError("marker fileのreadbackがexact一致しません")
    if list(Path(workspace).iterdir()) != [marker_file]:
        raise ValueError("workspaceにmarker file以外の内容があります")


def _audit_namespace(call: dict[str, Any], turn: list[dict[str, Any]]) -> None:
    if _require_call(call, "list_mcp_resources") != {}:
        raise ValueError("namespace gateのargumentsが空objectではありません")
    output = _call_output(turn, call.get("call_id"))
    try:
        document = json.loads(output)
    except json.JSONDecodeError as exc:
        raise ValueError("namespace tool outputがJSONではありません") from exc
    if not isinstance(document, dict) or not isinstance(document.get("resources"), list):
        raise ValueError("namespace tool outputにresources配列がありません")


def audit(
    sessions_root: Path,
    started_after: float,
    workspace: str,
    marker: str,
    gate: str,
    expected_content: str | None = None,
) -> dict[str, object]:
    try:
        matches = _marker_turns(sessions_root, marker, started_after)
        if not matches:
            return {"status": "pending", "gate": gate, "reason": "marker待ち"}
        if len(matches) != 1:
            raise ValueError(f"unique markerを持つturnが{len(matches)}件あります")

        path, records, marker_index = matches[0]
        meta = next((record for record in records if record.get("type") == "session_meta"), None)
        if meta is None:
            raise ValueError("session_metaがありません")
        payload = _payload(meta)
        expected_meta = {
            "originator": "Codex Desktop",
            "model_provider": "openrouter",
            "cwd": workspace,
        }
        for key, expected in expected_meta.items():
            if payload.get(key) != expected:
                raise ValueError(f"session_meta.{key}が不一致です: {payload.get(key)!r}")

        target = _turn_records(records, marker_index)
        if target is None:
            return {
                "status": "pending",
                "gate": gate,
                "session": str(path),
                "reason": "task_complete待ち",
            }
        turn_id, turn = target
        calls = _tool_calls(turn)
        if len(calls) != 1:
            raise ValueError(f"対象turnのtool callが{len(calls)}件です")
        if gate == "pwd":
            _audit_pwd(calls[0], turn, workspace)
        elif gate == "apply-patch":
            if not expected_content:
                raise ValueError("apply-patch gateにexpected contentがありません")
            _audit_patch(calls[0], turn, workspace, expected_content)
        elif gate == "namespace":
            _audit_namespace(calls[0], turn)
        else:
            raise ValueError(f"未知のgateです: {gate}")
        return {
            "status": "pass",
            "gate": gate,
            "session": str(path),
            "session_id": payload.get("id", payload.get("session_id")),
            "turn_id": turn_id,
        }
    except (OSError, ValueError) as exc:
        return {"status": "fail", "gate": gate, "reason": str(exc)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sessions-root", type=Path, required=True)
    parser.add_argument("--started-after", type=float, required=True)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--marker", required=True)
    parser.add_argument("--gate", choices=("pwd", "apply-patch", "namespace"), required=True)
    parser.add_argument("--expected-content")
    args = parser.parse_args()
    result = audit(
        args.sessions_root,
        args.started_after,
        args.workspace,
        args.marker,
        args.gate,
        args.expected_content,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return {"pass": 0, "fail": 1, "pending": PENDING}[str(result["status"])]


if __name__ == "__main__":
    sys.exit(main())
