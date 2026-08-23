"""OpenRouter専用のローカルguard。

`[model_providers.openrouter]` の `base_url` をここへ向ける。**chatgpt.com へは
一切接続しない**ので、案BをつぶしたCloudflareの遮断を構造的に踏まない。

存在理由は漏洩の遮断。`model_provider` の反転はプロセス全体に効くため、利用者が
選んでいない背景thread（ambient suggestions等がappにハードコードされた
`gpt-5.6-luna` で作る）まで openrouter に束縛される。実測でそれは利用者本文を
含む43KBだった。許可集合に無いmodelは**1バイトも外へ出さずに**ここで止める。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
import hmac
import json
from pathlib import Path
import socketserver
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import urllib.error
import urllib.request

from . import toolbridge

ENDPOINT = "https://openrouter.ai/api/v1/responses"
HEALTH_PATH = "/__guard/health"
MAX_BODY_BYTES = 64 * 1024 * 1024

# codexのpatch審査が宛てる内部model名。catalog側で
# `auto_review_model_override` をORモデル自身へ向けていても、
# 審査request自体はこの名前で来るためguardで既定modelへ書き換える。
AUTO_REVIEW_ALIAS = "codex-auto-review"


class GuardError(RuntimeError):
    pass


def deny_payload(model: str | None) -> bytes:
    """appに見せるエラー。内部情報は載せない。"""
    return json.dumps(
        {
            "error": {
                "type": "invalid_request_error",
                "code": "model_not_allowed",
                "message": (
                    f"model {model!r} is not routed to OpenRouter by codex-openrouter."
                    if model
                    else "request has no model field."
                ),
            }
        }
    ).encode("utf-8")


def forward_to_openrouter(
    body: bytes,
    key: str,
    metadata_enabled: bool = False,
    timeout: float = 300.0,
):
    """OpenRouterへ中継し、(status, headers, 読み出し可能なstream) を返す。"""
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }
    if metadata_enabled:
        headers["X-OpenRouter-Metadata"] = "enabled"
    request = urllib.request.Request(
        ENDPOINT,
        data=body,
        method="POST",
        headers=headers,
    )
    try:
        response = urllib.request.urlopen(request, timeout=timeout)
        return response.status, dict(response.headers), response
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers), error


class Guard:
    """許可集合・鍵の取得・中継先を束ねる。テストのため中継は差し替え可能。"""

    def __init__(
        self,
        allowed_models: Iterable[str],
        key_provider: Callable[[], str],
        log_path: Path | None = None,
        forwarder: Callable[[bytes, str, bool], tuple[int, dict, object]] = forward_to_openrouter,
        nonce: str = "",
        access_token: str = "",
        zdr_models: Iterable[str] | None = None,
        review_model: str | None = None,
    ):
        self.allowed = frozenset(allowed_models)
        # 省略時は全modelへZDRを強制する。安全側が既定で、外すのは
        # registryが「このmodelにZDR endpointは無い」と記録している場合だけ。
        self.zdr = self.allowed if zdr_models is None else frozenset(zdr_models)
        self.key_provider = key_provider
        self.log_path = log_path
        self.forwarder = forwarder
        self.nonce = nonce
        self.access_token = access_token
        # auto review審査(`codex-auto-review`)の書き換え先。catalogの
        # `auto_review_model_override` と対になる。Noneなら審査aliasも拒否。
        self.review_model = review_model
        self._lock = threading.Lock()

    def resolve_model(self, model: str | None) -> str | None:
        """審査aliasを既定modelへ写像する。それ以外はそのまま。"""
        if model == AUTO_REVIEW_ALIAS:
            return self.review_model
        return model

    def allows(self, model: str | None) -> bool:
        if model == AUTO_REVIEW_ALIAS:
            return bool(self.review_model)
        return model is not None and model in self.allowed

    def record(self, **fields) -> None:
        """model・判定・サイズだけを残す。本文と鍵は絶対に書かない。"""
        if self.log_path is None:
            return
        fields["t"] = round(time.time(), 3)
        line = json.dumps(fields, ensure_ascii=False)
        with self._lock:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")

    def prepare(self, body: bytes) -> bytes:
        """ZDRを持つmodelにだけ強制する。既存の provider 指定は尊重する。

        以前は全リクエストへ無条件に立てていた。ZDR endpointを持たないmodelでは
        それが必ず失敗を招く（OpenRouterが routing 先を見つけられない）ので、
        registryが `zdr_supported: false` と記録したmodelでは立てない。
        その判断はここではなくregistryとdoctorの側にあり、guardは従うだけにする。
        """
        return self.prepare_request(body).encode()

    def prepare_request(self, body: bytes) -> toolbridge.PreparedRequest:
        """tool契約を平坦化し、ZDRだけを追加したrequestと復元表を返す。"""
        try:
            document = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise toolbridge.ToolBridgeError("request本文が有効なJSONではありません") from exc
        if not isinstance(document, dict):
            raise toolbridge.ToolBridgeError("request本文はJSON objectである必要があります")
        prepared = toolbridge.prepare_document(document)
        document = prepared.document
        if document.get("model") not in self.zdr:
            return prepared
        provider = document.get("provider")
        if not isinstance(provider, dict):
            provider = {}
        provider["zdr"] = True
        document["provider"] = provider
        return toolbridge.PreparedRequest(document, prepared.tool_map)


def tool_telemetry(tool_map: toolbridge.ToolMap, started: float) -> dict[str, object]:
    """toolを含むupstream requestにだけ足す、本文を持たない集計値。

    `duration_ms` は上流へ投げてから応答を流し終えるまでの経過時間で、
    prompt・tool名・argumentsのどれにも依存しない。tool以外のrequestには
    何も足さない。
    """
    if not tool_map.has_tools:
        return {}
    return {
        "tool_request": True,
        "duration_ms": max(0, round((time.monotonic() - started) * 1000)),
    }


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    # SSEを小さい書き込みで流し続けるので、送信側の遅延を持ち込まない。
    disable_nagle_algorithm = True
    guard: Guard

    def log_message(self, *args) -> None:  # noqa: D401 - stderrへ出さない
        return

    def _send(self, status: int, payload: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        if self.path.startswith(HEALTH_PATH):
            payload = json.dumps({"ok": True, "nonce": self.guard.nonce}).encode("utf-8")
            self._send(200, payload, "application/json")
            return
        self._send(404, b'{"error":"not found"}', "application/json")

    def do_POST(self) -> None:
        authorization = self.headers.get("authorization", "")
        expected = f"Bearer {self.guard.access_token}"
        if not self.guard.access_token or not hmac.compare_digest(authorization, expected):
            # 認証されていないrequestの本文は読み込まない。keep-aliveで次requestの
            # 一部として解釈しないよう、このconnectionは閉じる。
            self.close_connection = True
            self._send(401, b'{"error":{"message":"local guard authentication failed"}}',
                       "application/json")
            return
        length = int(self.headers.get("content-length") or 0)
        if length > MAX_BODY_BYTES:
            self._send(413, b'{"error":"payload too large"}', "application/json")
            return
        body = self.rfile.read(length) if length else b""

        model = None
        try:
            parsed = json.loads(body)
            if isinstance(parsed, dict):
                model = parsed.get("model")
        except (json.JSONDecodeError, UnicodeDecodeError):
            model = None

        if not self.guard.allows(model):
            # ここで止める。forwarderは呼ばない。
            self.guard.record(model=model, decision="denied", bytes=len(body))
            self._send(400, deny_payload(model), "application/json")
            return

        # 審査aliasは既定modelへ書き換えてからbridgeへ渡す。
        resolved = self.guard.resolve_model(model)
        if model == AUTO_REVIEW_ALIAS:
            body = json.dumps(
                {**parsed, "model": resolved}, ensure_ascii=False
            ).encode("utf-8")
            model = resolved

        try:
            prepared = self.guard.prepare_request(body)
        except toolbridge.ToolBridgeError:
            self.guard.record(model=model, decision="bridge-denied", bytes=len(body))
            self._send(
                400,
                b'{"error":{"code":"tool_bridge_error","message":"unsupported Codex tool wire"}}',
                "application/json",
            )
            return

        try:
            key = self.guard.key_provider()
        except Exception:
            self.guard.record(model=model, decision="key-error", bytes=len(body))
            self._send(503, b'{"error":{"message":"credential unavailable"}}', "application/json")
            return

        started = time.monotonic()
        try:
            status, headers, stream = self.guard.forwarder(
                prepared.encode(), key, prepared.tool_map.has_tools
            )
        except Exception:
            self.guard.record(
                model=model,
                decision="upstream-error",
                bytes=len(body),
                **tool_telemetry(prepared.tool_map, started),
            )
            self._send(502, b'{"error":{"message":"upstream failed"}}', "application/json")
            return

        try:
            self._relay(
                status,
                headers,
                stream,
                prepared.tool_map,
                lambda summary, usage: self.guard.record(
                    model=model,
                    decision="forwarded",
                    bytes=len(body),
                    status=status,
                    **tool_telemetry(prepared.tool_map, started),
                    **(summary.log_fields() if summary is not None else {}),
                    **(usage.log_fields() if usage is not None else {}),
                ),
            )
        except toolbridge.ToolBridgeError:
            self.guard.record(
                model=model,
                decision="bridge-error",
                bytes=len(body),
                status=status,
                **tool_telemetry(prepared.tool_map, started),
            )
            self.close_connection = True
            return
    def _relay(
        self,
        status: int,
        headers: dict,
        stream,
        tool_map: toolbridge.ToolMap,
        on_complete: Callable[
            [toolbridge.RouterSummary | None, toolbridge.UsageSummary | None], None
        ],
    ) -> toolbridge.RouterSummary | None:
        content_type = headers.get("Content-Type", "application/json")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        # 長さ不明のSSEをそのまま流すためchunkedにする。
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        # `HTTPResponse.read(n)` は n バイト溜まるかレスポンス完了まで返らない
        # （chunkedでも _read_chunked が n まで貯める）。それではSSEが 8KB 単位
        # でしか届かず、短いturnは完了まで無反応になる。1回の下位読み出し分だけ
        # 返す read1 を使う。forwarder は差し替え可能なので非対応なら read へ倒す。
        read_chunk = getattr(stream, "read1", None) or stream.read
        completed = False
        bridge = (
            toolbridge.SSEBridge(tool_map)
            if tool_map.has_tools and "text/event-stream" in content_type.lower()
            else None
        )
        json_buffer = bytearray()
        try:
            while True:
                chunk = read_chunk(8192)
                if not chunk:
                    break
                if bridge is not None:
                    for transformed in bridge.feed(chunk):
                        self._write_chunk(transformed)
                elif tool_map.has_tools and "application/json" in content_type.lower():
                    json_buffer.extend(chunk)
                else:
                    self._write_chunk(chunk)
            if bridge is not None:
                for transformed in bridge.finish():
                    self._write_chunk(transformed)
            elif tool_map.has_tools and "application/json" in content_type.lower():
                if not json_buffer:
                    raise toolbridge.ToolBridgeError(
                        "OpenRouterのJSON responseが空です"
                    )
                try:
                    document = json.loads(json_buffer)
                except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                    raise toolbridge.ToolBridgeError(
                        "OpenRouterのJSON responseが不正です"
                    ) from exc
                if not isinstance(document, dict):
                    raise toolbridge.ToolBridgeError("OpenRouter responseがobjectではありません")
                usage = toolbridge.extract_usage(document)
                transformed, summary = toolbridge.transform_response_document(document, tool_map)
                self._write_chunk(json.dumps(transformed, ensure_ascii=False).encode("utf-8"))
                on_complete(summary, usage)
                completed = True
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
                return summary
            summary = bridge.summary if bridge is not None else None
            on_complete(summary, bridge.usage if bridge is not None else None)
            completed = True
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            close = getattr(stream, "close", None)
            if close is not None:
                close()
            if not completed:
                self.close_connection = True
        return bridge.summary if bridge is not None else None

    def _write_chunk(self, chunk: bytes) -> None:
        if not chunk:
            return
        self.wfile.write(f"{len(chunk):X}\r\n".encode("ascii"))
        self.wfile.write(chunk)
        self.wfile.write(b"\r\n")
        self.wfile.flush()


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    block_on_close = False


def serve(guard: Guard, host: str = "127.0.0.1", port: int = 0) -> tuple[_Server, int]:
    """guardを起動し (server, 実際のport) を返す。port=0でephemeral。"""
    handler = type("_BoundHandler", (_Handler,), {"guard": guard})
    server = _Server((host, port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, server.server_address[1]


def health_ok(port: int, nonce: str, host: str = "127.0.0.1", timeout: float = 2.0) -> bool:
    """そのportに居るのが自分のguardかを確認する。横取り検出用。"""
    url = f"http://{host}:{port}{HEALTH_PATH}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            document = json.loads(response.read())
    except (urllib.error.URLError, OSError, json.JSONDecodeError, TimeoutError):
        return False
    return document.get("ok") is True and document.get("nonce") == nonce


def free_port(host: str = "127.0.0.1") -> int:
    with socketserver.TCPServer((host, 0), None) as probe:
        return probe.server_address[1]
