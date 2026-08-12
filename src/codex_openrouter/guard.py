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
import json
from pathlib import Path
import socketserver
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import urllib.error
import urllib.request

ENDPOINT = "https://openrouter.ai/api/v1/responses"
HEALTH_PATH = "/__guard/health"
MAX_BODY_BYTES = 64 * 1024 * 1024


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


def forward_to_openrouter(body: bytes, key: str, timeout: float = 300.0):
    """OpenRouterへ中継し、(status, headers, 読み出し可能なstream) を返す。"""
    request = urllib.request.Request(
        ENDPOINT,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        },
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
        forwarder: Callable[[bytes, str], tuple[int, dict, object]] = forward_to_openrouter,
        nonce: str = "",
    ):
        self.allowed = frozenset(allowed_models)
        self.key_provider = key_provider
        self.log_path = log_path
        self.forwarder = forwarder
        self.nonce = nonce
        self._lock = threading.Lock()

    def allows(self, model: str | None) -> bool:
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
        """ZDR強制。既存の provider 指定があれば尊重しつつ zdr を立てる。"""
        try:
            document = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return body
        if not isinstance(document, dict):
            return body
        provider = document.get("provider")
        if not isinstance(provider, dict):
            provider = {}
        provider["zdr"] = True
        document["provider"] = provider
        return json.dumps(document).encode("utf-8")


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

        try:
            key = self.guard.key_provider()
        except Exception:
            self.guard.record(model=model, decision="key-error", bytes=len(body))
            self._send(503, b'{"error":{"message":"credential unavailable"}}', "application/json")
            return

        try:
            status, headers, stream = self.guard.forwarder(self.guard.prepare(body), key)
        except Exception:
            self.guard.record(model=model, decision="upstream-error", bytes=len(body))
            self._send(502, b'{"error":{"message":"upstream failed"}}', "application/json")
            return

        self.guard.record(model=model, decision="forwarded", bytes=len(body), status=status)
        self._relay(status, headers, stream)

    def _relay(self, status: int, headers: dict, stream) -> None:
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
        try:
            while True:
                chunk = read_chunk(8192)
                if not chunk:
                    break
                self.wfile.write(f"{len(chunk):X}\r\n".encode("ascii"))
                self.wfile.write(chunk)
                self.wfile.write(b"\r\n")
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            close = getattr(stream, "close", None)
            if close is not None:
                close()


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
