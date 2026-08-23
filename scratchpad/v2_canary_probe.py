"""V2: canary 3パターン実測 (sort/zdr/max_tokens の効果確認)."""
import json
import subprocess
import sys
import urllib.request
import urllib.error

sys.path.insert(0, "src")
from codex_openrouter import toolbridge

MODEL = "deepseek/deepseek-v4-flash-0731"
ENDPOINT = "https://openrouter.ai/api/v1/responses"


def get_key():
    out = subprocess.run(
        ["/Users/hk/.local/bin/codex-openrouter-credential", "get"],
        capture_output=True, text=True,
    )
    if out.returncode == 0 and out.stdout.strip():
        return out.stdout.strip()
    raise SystemExit(f"credential helper failed: {out.stderr[:200]}")


def body(*, sort: bool, zdr: bool, max_tokens: int, freeform: bool = False):
    name = "codex_freeform_probe" if freeform else "codex_structured_probe"
    if freeform:
        tool = {
            "type": "custom",
            "name": name,
            "description": "Return the raw text PING and do not answer normally.",
        }
    else:
        tool = {
            "type": "function",
            "name": name,
            "description": "Return the fixed value PING and do not answer normally.",
            "parameters": {
                "type": "object",
                "properties": {"value": {"type": "string", "enum": ["PING"]}},
                "required": ["value"],
                "additionalProperties": False,
            },
            "strict": True,
        }
    b = {
        "model": MODEL,
        "input": f"Call {name} exactly once with PING. Do not return a message.",
        "tools": [tool],
        "tool_choice": {"type": tool["type"], "name": name},
        "max_output_tokens": max_tokens,
    }
    if sort or zdr:
        provider = {}
        if zdr:
            provider["zdr"] = True
        if sort:
            provider["sort"] = "price"
        b["provider"] = provider
    return b


def call(b, key):
    prepared = toolbridge.prepare_document(b)
    req = urllib.request.Request(
        ENDPOINT,
        data=json.dumps(prepared.document).encode(),
        method="POST",
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "X-OpenRouter-Metadata": "enabled",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            doc = json.load(resp)
            status = resp.status
    except urllib.error.HTTPError as e:
        try:
            doc = json.loads(e.read())
        except Exception:
            doc = {}
        status = e.code
    except Exception as e:
        return -1, {"error": repr(e)}
    # restore via bridge to check name integrity
    try:
        restored, _ = toolbridge.transform_response_document(doc, prepared.tool_map)
    except toolbridge.ToolBridgeError as exc:
        return status, {"bridge_error": str(exc), "output_types": [
            i.get("type") for i in doc.get("output", []) if isinstance(i, dict)
        ]}
    ok = False
    for item in restored.get("output", []):
        if not isinstance(item, dict):
            continue
        want_t = "custom_tool_call" if b["tools"][0]["type"] == "custom" else "function_call"
        want_n = b["tools"][0]["name"]
        if item.get("type") == want_t and item.get("name") == want_n:
            raw = item.get("input") if want_t == "custom_tool_call" else item.get("arguments")
            try:
                ok = json.loads(raw) == {"value": "PING"} if isinstance(raw, str) else raw.strip() == "PING"
            except Exception:
                ok = False
    usage = doc.get("usage") or {}
    return status, {
        "ping_ok": ok,
        "total_tokens": usage.get("total_tokens"),
        "provider": (doc.get("provider") or "")[:40],
    }


def main():
    key = get_key()
    patterns = {
        "(a) no-sort/no-zdr/256 structured": body(sort=False, zdr=False, max_tokens=256),
        "(b) sort+zdr/256 structured": body(sort=True, zdr=True, max_tokens=256),
        "(c) sort only/256 structured": body(sort=True, zdr=False, max_tokens=256),
    }
    results = {}
    for label, b in patterns.items():
        runs = []
        for i in range(3):
            status, info = call(b, key)
            runs.append((status, info))
            print(f"{label} run{i+1}: HTTP {status} {json.dumps(info, ensure_ascii=False)[:160]}", flush=True)
        results[label] = runs
    print("\n=== SUMMARY ===")
    for label, runs in results.items():
        oks = sum(1 for s, i in runs if s == 200 and i.get("ping_ok"))
        print(f"{label}: {oks}/3 PING")


if __name__ == "__main__":
    main()
