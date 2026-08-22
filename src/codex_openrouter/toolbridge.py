"""Codex Responses tool契約をOpenRouterのstructured functionへ橋渡しする。

このモジュールはHTTP・Keychain・profileを知らない純粋変換層である。OpenRouterへ
見せるのは通常のfunctionだけとし、Codex固有のnamespace/customはrequest単位の
復元表を使ってResponses SSEへ戻す。壊れたcallを推測で補わない。
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any

# wire を変えたら上げる。toolcompat の cache 判定にも使われるので、
# 古い契約下で測った結果（build 6849 の "freeform非互換"）が自動で失効する。
TOOL_CONTRACT_VERSION = 3

# namespace配下のtoolを平坦化するときの区切り。元の意味を保ったまま
# top-levelとの衝突を避ける。Codex appが自前のnamespace toolに使う形と同じ。
NAMESPACE_DELIMITER = "__"

# custom toolをfunctionへ落とすときの唯一の引数名。
# 2026-08-22の実測: `content` で 4/4、`patch` で 3/4。LiteLLMのCodex向け
# bridgeも同じ名前を使う。task/0822-toolbridge-fix-plan.md
CUSTOM_INPUT_FIELD = "content"


class ToolBridgeError(RuntimeError):
    """変換不能または不正なtool wire。呼び出しを実行させず停止する。"""


@dataclass(frozen=True)
class ToolTarget:
    kind: str
    name: str
    namespace: str | None = None
    input_field: str | None = None


@dataclass(frozen=True)
class ToolMap:
    transformed: dict[str, ToolTarget] = field(default_factory=dict)
    original: dict[tuple[str | None, str], str] = field(default_factory=dict)
    passthrough: frozenset[str] = frozenset()

    @property
    def has_tools(self) -> bool:
        return bool(self.transformed or self.passthrough)

    def target_for_response(self, name: object) -> ToolTarget | None:
        if not isinstance(name, str) or not name:
            raise ToolBridgeError("tool callに有効なnameがありません")
        target = self.transformed.get(name)
        if target is not None:
            return target
        if name not in self.passthrough:
            raise ToolBridgeError(f"requestに無いtool callです: {name}")
        return None


@dataclass(frozen=True)
class PreparedRequest:
    document: dict[str, Any]
    tool_map: ToolMap

    def encode(self) -> bytes:
        return json.dumps(self.document, ensure_ascii=False).encode("utf-8")


@dataclass(frozen=True)
class RouterSummary:
    provider: str | None = None
    provider_attempt: int | None = None
    candidate_count: int | None = None
    status: str | int | None = None

    def log_fields(self) -> dict[str, str | int]:
        fields: dict[str, str | int] = {}
        for name in ("provider", "provider_attempt", "candidate_count"):
            value = getattr(self, name)
            if value is not None:
                fields[name] = value
        if self.status is not None:
            fields["router_status"] = self.status
        return fields


@dataclass(frozen=True)
class UsageSummary:
    """responseが自分で申告したtoken数だけ。本文も推定値も持たない。"""

    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_tokens: int | None = None

    def log_fields(self) -> dict[str, int]:
        fields: dict[str, int] = {}
        for name in ("input_tokens", "output_tokens", "cached_tokens"):
            value = getattr(self, name)
            if value is not None:
                fields[name] = value
        return fields


def extract_usage(document: dict[str, Any]) -> UsageSummary | None:
    """Responses契約に実在する非負整数のtoken数だけを返す。

    `prompt_tokens` のようなChat Completions形や、非整数・未知shapeは
    読み替えずに省略する。合計や差分の再計算もしない。
    """
    if not isinstance(document, dict):
        return None
    usage = document.get("usage")
    if not isinstance(usage, dict):
        response = document.get("response")
        usage = response.get("usage") if isinstance(response, dict) else None
    if not isinstance(usage, dict):
        return None
    details = usage.get("input_tokens_details")
    cached = _safe_int(details.get("cached_tokens")) if isinstance(details, dict) else None
    if cached is None:
        cached = _safe_int(usage.get("cached_tokens"))
    summary = UsageSummary(
        _safe_int(usage.get("input_tokens")),
        _safe_int(usage.get("output_tokens")),
        cached,
    )
    return summary if summary.log_fields() else None


def supported_builds(path: Path) -> tuple[str, ...]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ToolBridgeError("tool wire互換build一覧を読めません") from exc
    if (
        not isinstance(document, dict)
        or document.get("schema_version") != 1
        or document.get("tool_contract_version") != TOOL_CONTRACT_VERSION
        or not isinstance(document.get("builds"), list)
    ):
        raise ToolBridgeError("tool wire互換build一覧の契約が不正です")
    builds = tuple(item.get("build") for item in document["builds"] if isinstance(item, dict))
    if len(builds) != 2 or not all(isinstance(build, str) and build for build in builds):
        raise ToolBridgeError("tool wire互換buildは最新版と直前buildの2件が必要です")
    if len(set(builds)) != 2:
        raise ToolBridgeError("tool wire互換buildが重複しています")
    return builds


def assert_supported_build(path: Path, build: str) -> None:
    if build not in supported_builds(path):
        raise ToolBridgeError(
            f"ChatGPT build {build} はtool契約の互換確認待ちです。"
            "純正ChatGPT.appは通常どおり利用できます。"
        )


def _name(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ToolBridgeError(f"{label}に有効なnameがありません")
    return value


def _grammar_suffix(tool: dict[str, Any]) -> str:
    """custom toolのgrammarをdescriptionへ畳む。

    Chat Completionsのfunctionにgrammarを載せる場所は無いので、modelが読める
    唯一の場所へ入れる。定義が無ければ何も足さない。
    """
    fmt = tool.get("format")
    if not isinstance(fmt, dict):
        return ""
    definition = fmt.get("definition")
    if not isinstance(definition, str) or not definition:
        return ""
    syntax = fmt.get("syntax")
    return f"\n\nFormat:\n```{syntax if isinstance(syntax, str) else ''}\n{definition}\n```"


def _description(tool: dict[str, Any], target: ToolTarget) -> str:
    """元のdescriptionを正本とし、grammarだけを足す。

    以前はここへ日本語のprefixを付けていたが、変換後もtool名を保つように
    なった（`_forwarded_name`）ので、名前を言い直す情報価値が無い。
    """
    original = tool.get("description")
    base = original if isinstance(original, str) and original else ""
    return base + (_grammar_suffix(tool) if target.kind == "custom" else "")


def _bridged_function(tool: dict[str, Any], forwarded: str, target: ToolTarget) -> dict[str, Any]:
    """custom toolを、resellerが確実に扱えるplainなfunctionへ落とす。

    `strict` と `additionalProperties` は**付けない**。2026-08-22にOpenRouterへ
    直接測った結果、`strict:true` を付けた形は apply_patch を 0/4 でしか
    引き出せず、外すと 3〜4/4 になった。structured outputsを公称するendpointが
    DeepSeekでは 22/30 しか無く、`sort:"price"` は残りを普通に引く。
    """
    if target.kind == "function":
        result = deepcopy(tool)
        result["name"] = forwarded
        result["description"] = _description(tool, target)
        return result
    field_name = target.input_field
    assert field_name is not None
    return {
        "type": "function",
        "name": forwarded,
        "description": _description(tool, target),
        "parameters": {
            "type": "object",
            "properties": {
                field_name: {
                    "type": "string",
                    "description": f"The {target.name} content following the specified format",
                }
            },
            "required": [field_name],
        },
    }


def _forwarded_name(target: ToolTarget, used: set[str]) -> str:
    """変換後の名前。意味を保つため元の名前を捨てない。

    top-levelはそのまま、namespace配下は `<namespace>__<name>` に平坦化する。
    衝突は名前空間の潰れなので、推測で回避せず停止する。
    """
    candidate = (
        f"{target.namespace}{NAMESPACE_DELIMITER}{target.name}"
        if target.namespace
        else target.name
    )
    if candidate in used:
        raise ToolBridgeError(f"変換済みtool名が既存toolと衝突します: {candidate}")
    used.add(candidate)
    return candidate


def _tool_group(result: dict[str, Any]) -> tuple[str, int | None, list[Any]] | None:
    """tool定義の在り処を返す。無ければ None。

    codexにはtool送信形式が2つある（`codex-rs/core/src/client.rs`）。classicは
    top-levelの `tools`、responses-lite（`use_responses_lite = true`）は
    `input[]` の `{"type":"additional_tools", ...}` に載せる。lite形式を見て
    いなかったのが実機gate 2の1つ目の原因で、Bridgeが一度も起動しなかった。

    catalogのflagではなくpayloadの形で判定するので、テンプレートが将来
    `use_responses_lite` を反転しても追随できる。
    """
    groups: list[tuple[str, int | None, list[Any]]] = []
    tools = result.get("tools")
    if tools is not None:
        if not isinstance(tools, list):
            raise ToolBridgeError("toolsは配列である必要があります")
        groups.append(("tools", None, tools))
    items = result.get("input")
    if isinstance(items, list):
        for index, item in enumerate(items):
            if not isinstance(item, dict) or item.get("type") != "additional_tools":
                continue
            nested = item.get("tools")
            if not isinstance(nested, list):
                raise ToolBridgeError("additional_toolsのtoolsは配列である必要があります")
            groups.append(("input", index, nested))
    if not groups:
        return None
    if len(groups) > 1:
        # classicとliteが同時に来る形をcodexは送らない。どちらを正本とみなすかを
        # 推測する場面なので、変換せずに止める。
        raise ToolBridgeError("tool定義がtop-levelとadditional_toolsの両方にあります")
    return groups[0]


def prepare_document(document: dict[str, Any]) -> PreparedRequest:
    if not isinstance(document, dict):
        raise ToolBridgeError("Responses requestはJSON objectである必要があります")
    result = deepcopy(document)
    group = _tool_group(result)
    if group is None:
        return PreparedRequest(result, ToolMap())
    location, location_index, tools = group

    used: set[str] = set()
    identities: set[tuple[str | None, str]] = set()
    passthrough: set[str] = set()
    transformed: dict[str, ToolTarget] = {}
    original: dict[tuple[str | None, str], str] = {}
    output: list[dict[str, Any]] = []
    namespaces: set[str] = set()

    # 通常function名を先に予約する。生成名との衝突をtool順に依存させない。
    for tool in tools:
        if not isinstance(tool, dict):
            raise ToolBridgeError("tool definitionはobjectである必要があります")
        if tool.get("type") == "function":
            name = _name(tool.get("name"), "function")
            if name in used:
                raise ToolBridgeError(f"function名が重複しています: {name}")
            used.add(name)

    def add(tool: dict[str, Any], namespace: str | None) -> None:
        kind = tool.get("type")
        if kind not in {"function", "custom"}:
            raise ToolBridgeError(f"未対応のCodex tool型です: {kind!r}")
        name = _name(tool.get("name"), "tool")
        identity = (namespace, name)
        if identity in identities:
            label = f"{namespace}.{name}" if namespace else name
            raise ToolBridgeError(f"tool名が重複しています: {label}")
        identities.add(identity)

        if kind == "function" and namespace is None:
            passthrough.add(name)
            output.append(deepcopy(tool))
            return

        target = ToolTarget(
            kind=kind,
            name=name,
            namespace=namespace,
            input_field=CUSTOM_INPUT_FIELD if kind == "custom" else None,
        )
        forwarded = _forwarded_name(target, used)
        transformed[forwarded] = target
        original[identity] = forwarded
        output.append(_bridged_function(tool, forwarded, target))

    for tool in tools:
        if tool.get("type") != "namespace":
            add(tool, None)
            continue
        namespace = _name(tool.get("name"), "namespace")
        if namespace in namespaces:
            raise ToolBridgeError(f"namespace名が重複しています: {namespace}")
        namespaces.add(namespace)
        children = tool.get("tools")
        if not isinstance(children, list) or not children:
            raise ToolBridgeError(f"namespace {namespace} のtoolsが空または不正です")
        for child in children:
            if not isinstance(child, dict):
                raise ToolBridgeError(f"namespace {namespace} のchildがobjectではありません")
            add(child, namespace)

    tool_map = ToolMap(transformed, original, frozenset(passthrough))
    # 元の置き場所へ戻す。lite形式にtop-level `tools` を新設すると、
    # codexが送っていない形をupstreamへ見せることになる。
    if location == "tools":
        result["tools"] = output
    else:
        assert location_index is not None
        result["input"][location_index]["tools"] = output
    _transform_request_input(result, tool_map)
    if "tool_choice" in result:
        result["tool_choice"] = _transform_tool_choice(result["tool_choice"], tool_map)
    return PreparedRequest(result, tool_map)


def _lookup_original(tool_map: ToolMap, namespace: object, name: object) -> str | None:
    parsed_name = _name(name, "tool call")
    parsed_namespace = namespace if isinstance(namespace, str) and namespace else None
    forwarded = tool_map.original.get((parsed_namespace, parsed_name))
    if forwarded is not None:
        return forwarded
    if parsed_namespace is None and parsed_name in tool_map.passthrough:
        return None
    label = f"{parsed_namespace}.{parsed_name}" if parsed_namespace else parsed_name
    raise ToolBridgeError(f"request inputが未定義toolを参照しています: {label}")


def _transform_request_input(document: dict[str, Any], tool_map: ToolMap) -> None:
    items = document.get("input")
    if not isinstance(items, list):
        return
    transformed: list[Any] = []
    for raw in items:
        if not isinstance(raw, dict):
            transformed.append(raw)
            continue
        item = deepcopy(raw)
        kind = item.get("type")
        if kind not in {"function_call", "custom_tool_call", "custom_tool_call_output"}:
            transformed.append(item)
            continue
        if kind == "custom_tool_call_output":
            item["type"] = "function_call_output"
            transformed.append(item)
            continue
        forwarded = _lookup_original(tool_map, item.get("namespace"), item.get("name"))
        if forwarded is None:
            transformed.append(item)
            continue
        item["name"] = forwarded
        item.pop("namespace", None)
        if kind == "custom_tool_call":
            target = tool_map.transformed[forwarded]
            raw_input = item.pop("input", None)
            if not isinstance(raw_input, str):
                raise ToolBridgeError("custom_tool_call inputはstringである必要があります")
            item["type"] = "function_call"
            item["arguments"] = json.dumps(
                {target.input_field: raw_input}, ensure_ascii=False, separators=(",", ":")
            )
        transformed.append(item)
    document["input"] = transformed


def _transform_tool_choice(value: Any, tool_map: ToolMap) -> Any:
    if not isinstance(value, dict):
        return value
    result = deepcopy(value)
    if isinstance(result.get("tools"), list):
        result["tools"] = [_transform_tool_choice(item, tool_map) for item in result["tools"]]
        return result
    if result.get("type") not in {"function", "custom"}:
        return result
    forwarded = _lookup_original(tool_map, result.get("namespace"), result.get("name"))
    if forwarded is not None:
        result["type"] = "function"
        result["name"] = forwarded
        result.pop("namespace", None)
    return result


def _unwrap_custom(arguments: object, target: ToolTarget) -> str:
    if not isinstance(arguments, str):
        raise ToolBridgeError("変換済みcustom callのargumentsがstringではありません")
    try:
        parsed = json.loads(arguments)
    except json.JSONDecodeError as exc:
        raise ToolBridgeError("変換済みcustom callのargumentsが不完全JSONです") from exc
    field_name = target.input_field
    if not isinstance(parsed, dict) or set(parsed) != {field_name}:
        raise ToolBridgeError("変換済みcustom callのarguments契約が不正です")
    raw = parsed[field_name]
    if not isinstance(raw, str):
        raise ToolBridgeError("変換済みcustom callのinputがstringではありません")
    return raw


def _validate_function_arguments(arguments: object) -> None:
    if not isinstance(arguments, str):
        raise ToolBridgeError("function callのargumentsがstringではありません")
    try:
        parsed = json.loads(arguments)
    except json.JSONDecodeError as exc:
        raise ToolBridgeError("function callのargumentsが不完全JSONです") from exc
    if not isinstance(parsed, dict):
        raise ToolBridgeError("function callのargumentsがJSON objectではありません")


def transform_output_item(item: dict[str, Any], tool_map: ToolMap) -> dict[str, Any]:
    result = deepcopy(item)
    if result.get("type") != "function_call":
        return result
    target = tool_map.target_for_response(result.get("name"))
    if target is None:
        _validate_function_arguments(result.get("arguments"))
        return result
    result["name"] = target.name
    if target.namespace:
        result["namespace"] = target.namespace
    if target.kind == "custom":
        result["type"] = "custom_tool_call"
        result["input"] = _unwrap_custom(result.pop("arguments", None), target)
    else:
        _validate_function_arguments(result.get("arguments"))
    return result


def extract_router_metadata(document: dict[str, Any]) -> RouterSummary | None:
    """OpenRouter metadataを除去し、安全な集計値だけ返す。"""
    metadata = document.pop("openrouter_metadata", None)
    response = document.get("response")
    if isinstance(response, dict):
        nested = response.pop("openrouter_metadata", None)
        if metadata is None:
            metadata = nested
    if not isinstance(metadata, dict):
        return None

    attempts = metadata.get("attempts")
    attempt_rows = attempts if isinstance(attempts, list) else []
    last = next((row for row in reversed(attempt_rows) if isinstance(row, dict)), {})
    provider = _safe_text(
        last.get("provider_name")
        or last.get("provider")
        or metadata.get("provider_name")
        or metadata.get("provider")
    )
    attempt = _safe_int(metadata.get("attempt") or last.get("attempt"))
    if attempt is None and attempt_rows:
        attempt = len(attempt_rows)
    candidate_count = _safe_int(metadata.get("candidate_count"))
    if candidate_count is None:
        endpoints = metadata.get("endpoints")
        if isinstance(endpoints, dict):
            candidate_count = _safe_int(endpoints.get("total"))
            if candidate_count is None and isinstance(endpoints.get("available"), list):
                candidate_count = len(endpoints["available"])
        if candidate_count is None:
            for key in ("candidates", "endpoints"):
                if isinstance(metadata.get(key), list):
                    candidate_count = len(metadata[key])
                    break
    status = last.get("status", metadata.get("status"))
    if not isinstance(status, (str, int)) or isinstance(status, bool):
        status = None
    if isinstance(status, str):
        status = status[:64]
    return RouterSummary(provider, attempt, candidate_count, status)


def _safe_text(value: object) -> str | None:
    return value[:128] if isinstance(value, str) and value else None


def _safe_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def transform_response_document(
    document: dict[str, Any], tool_map: ToolMap
) -> tuple[dict[str, Any], RouterSummary | None]:
    result = deepcopy(document)
    summary = extract_router_metadata(result)
    output = result.get("output")
    if isinstance(output, list):
        result["output"] = [
            transform_output_item(item, tool_map) if isinstance(item, dict) else item
            for item in output
        ]
    return result, summary


@dataclass
class _CallState:
    target: ToolTarget | None
    arguments: list[str] = field(default_factory=list)
    arguments_done: bool = False
    output_done: bool = False
    first_delta_sequence: int | None = None


class SSEBridge:
    """任意のbyte境界で届くResponses SSEを逐次変換する。"""

    def __init__(self, tool_map: ToolMap):
        self.tool_map = tool_map
        self.buffer = b""
        self.calls: dict[str, _CallState] = {}
        self.summary: RouterSummary | None = None
        self.usage: UsageSummary | None = None
        self.saw_done = False

    def feed(self, chunk: bytes) -> list[bytes]:
        self.buffer += chunk
        output: list[bytes] = []
        while True:
            boundary = _event_boundary(self.buffer)
            if boundary is None:
                break
            end, separator_size = boundary
            block = self.buffer[:end]
            self.buffer = self.buffer[end + separator_size :]
            output.extend(self._event(block))
        return output

    def finish(self) -> list[bytes]:
        if self.buffer.strip():
            raise ToolBridgeError("SSEがevent境界の途中で切断されました")
        incomplete = [item for item, state in self.calls.items() if not state.output_done]
        if incomplete:
            raise ToolBridgeError("SSE tool lifecycleが完了していません: " + ", ".join(incomplete))
        if not self.saw_done:
            raise ToolBridgeError("SSEが[DONE]より前に切断されました")
        return []

    def _event(self, block: bytes) -> list[bytes]:
        lines = block.replace(b"\r\n", b"\n").split(b"\n")
        data_lines = [line[5:].lstrip(b" ") for line in lines if line.startswith(b"data:")]
        if not data_lines:
            return [block + b"\n\n"]
        payload = b"\n".join(data_lines)
        if payload == b"[DONE]":
            incomplete = [item for item, state in self.calls.items() if not state.output_done]
            if incomplete:
                raise ToolBridgeError("[DONE]より前にtool lifecycleが完了していません")
            self.saw_done = True
            return [b"data: [DONE]\n\n"]
        try:
            event = json.loads(payload)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ToolBridgeError("tool requestのSSE dataがJSONではありません") from exc
        if not isinstance(event, dict):
            raise ToolBridgeError("tool requestのSSE eventがobjectではありません")
        events = self._transform_event(event)
        return [
            b"data: " + json.dumps(item, ensure_ascii=False, separators=(",", ":")).encode() + b"\n\n"
            for item in events
        ]

    def _transform_event(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        summary = extract_router_metadata(event)
        if summary is not None:
            self.summary = summary
        usage = extract_usage(event)
        if usage is not None:
            self.usage = usage
        kind = event.get("type")
        if kind == "response.output_item.added":
            return [self._added(event)]
        if kind == "response.function_call_arguments.delta":
            return self._arguments_delta(event)
        if kind == "response.function_call_arguments.done":
            return self._arguments_done(event)
        if kind == "response.output_item.done":
            return [self._output_done(event)]
        if kind == "response.completed":
            result = deepcopy(event)
            response = result.get("response")
            if isinstance(response, dict):
                transformed, nested_summary = transform_response_document(response, self.tool_map)
                result["response"] = transformed
                if nested_summary is not None:
                    self.summary = nested_summary
            return [result]
        if kind in {"response.failed", "response.incomplete", "error"}:
            if any(not state.output_done for state in self.calls.values()):
                raise ToolBridgeError("tool callの途中でSSEが失敗または不完全になりました")
        return [event]

    def _added(self, event: dict[str, Any]) -> dict[str, Any]:
        result = deepcopy(event)
        item = result.get("item")
        if not isinstance(item, dict) or item.get("type") != "function_call":
            return result
        item_id = _name(item.get("id") or result.get("item_id"), "function call item")
        if item_id in self.calls:
            raise ToolBridgeError(f"function call itemが重複しています: {item_id}")
        target = self.tool_map.target_for_response(item.get("name"))
        self.calls[item_id] = _CallState(target)
        if target is None:
            return result
        item["name"] = target.name
        if target.namespace:
            item["namespace"] = target.namespace
        if target.kind == "custom":
            item["type"] = "custom_tool_call"
            item.pop("arguments", None)
            item["input"] = ""
        return result

    def _state(self, event: dict[str, Any]) -> tuple[str, _CallState]:
        item_id = _name(event.get("item_id"), "SSE event item")
        state = self.calls.get(item_id)
        if state is None:
            raise ToolBridgeError(f"addedより先にtool delta/doneが来ました: {item_id}")
        return item_id, state

    def _arguments_delta(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        _item_id, state = self._state(event)
        delta = event.get("delta")
        if not isinstance(delta, str):
            raise ToolBridgeError("function arguments deltaがstringではありません")
        if state.arguments_done:
            raise ToolBridgeError("arguments.doneより後にdeltaが来ました")
        state.arguments.append(delta)
        if state.target is None or state.target.kind == "function":
            return [event]
        if state.first_delta_sequence is None and isinstance(event.get("sequence_number"), int):
            state.first_delta_sequence = event["sequence_number"]
        return []

    def _arguments_done(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        _item_id, state = self._state(event)
        if state.arguments_done:
            raise ToolBridgeError("function arguments doneが重複しています")
        state.arguments_done = True
        target = state.target
        arguments = event.get("arguments")
        accumulated = "".join(state.arguments)
        if accumulated and arguments != accumulated:
            raise ToolBridgeError("function arguments deltaとdoneが一致しません")
        if target is None:
            _validate_function_arguments(arguments)
            return [event]
        if target.kind == "function":
            _validate_function_arguments(arguments)
            result = deepcopy(event)
            result["name"] = target.name
            if target.namespace:
                result["namespace"] = target.namespace
            return [result]

        raw = _unwrap_custom(arguments, target)
        delta = {
            key: value
            for key, value in event.items()
            if key not in {"type", "arguments", "name"}
        }
        delta["type"] = "response.custom_tool_call_input.delta"
        delta["delta"] = raw
        if state.first_delta_sequence is not None:
            delta["sequence_number"] = state.first_delta_sequence
        done = {
            key: value
            for key, value in event.items()
            if key not in {"type", "arguments", "name"}
        }
        done["type"] = "response.custom_tool_call_input.done"
        done["input"] = raw
        return [delta, done]

    def _output_done(self, event: dict[str, Any]) -> dict[str, Any]:
        result = deepcopy(event)
        item = result.get("item")
        if not isinstance(item, dict) or item.get("type") != "function_call":
            return result
        item_id = _name(item.get("id") or result.get("item_id"), "function call item")
        state = self.calls.get(item_id)
        if state is None:
            raise ToolBridgeError(f"output_item.doneより前にaddedがありません: {item_id}")
        if state.output_done:
            raise ToolBridgeError(f"output_item.doneが重複しています: {item_id}")
        if not state.arguments_done:
            raise ToolBridgeError("tool callのarguments.doneが欠落しています")
        result["item"] = transform_output_item(item, self.tool_map)
        state.output_done = True
        return result


def _event_boundary(buffer: bytes) -> tuple[int, int] | None:
    lf = buffer.find(b"\n\n")
    crlf = buffer.find(b"\r\n\r\n")
    candidates = [(lf, 2), (crlf, 4)]
    candidates = [candidate for candidate in candidates if candidate[0] >= 0]
    return min(candidates) if candidates else None
