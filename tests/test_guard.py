from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from codex_openrouter import guard as guard_module  # noqa: E402

ALLOWED = {"deepseek/deepseek-v4-pro", "z-ai/glm-5.2"}
# 実測で巻き込まれたnative slug。appにハードコードされている。
COLLATERAL = "gpt-5.6-luna"


class RecordingForwarder:
    """呼ばれたら記録する。拒否経路で呼ばれないことを証明するために使う。"""

    def __init__(self, status: int = 200, payload: bytes = b"data: ok\n\n"):
        self.calls: list[tuple[bytes, str]] = []
        self.status = status
        self.payload = payload

    def __call__(self, body: bytes, key: str):
        self.calls.append((body, key))
        return self.status, {"Content-Type": "text/event-stream"}, io.BytesIO(self.payload)


class GuardTestCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.log = Path(self.directory.name) / "guard.log"
        self.forwarder = RecordingForwarder()
        self.guard = guard_module.Guard(
            allowed_models=ALLOWED,
            key_provider=lambda: "sk-or-test-key",
            log_path=self.log,
            forwarder=self.forwarder,
            nonce="test-nonce",
            access_token="local-token",
        )
        self.server, self.port = guard_module.serve(self.guard)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def post(self, document) -> tuple[int, bytes]:
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/v1/responses",
            data=json.dumps(document).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": "Bearer local-token",
                "Content-Type": "application/json",
                "Connection": "close",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            try:
                return error.code, error.read()
            finally:
                error.close()

    def log_records(self) -> list[dict]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines() if line.strip()]


class AllowlistTests(GuardTestCase):
    def test_allowed_model_is_forwarded(self):
        status, payload = self.post({"model": "deepseek/deepseek-v4-pro", "input": "hi"})
        self.assertEqual(status, 200)
        self.assertIn(b"data: ok", payload)
        self.assertEqual(len(self.forwarder.calls), 1)

    def test_native_slug_is_never_forwarded(self):
        status, payload = self.post({"model": COLLATERAL, "input": "secret user text"})
        self.assertEqual(status, 400)
        self.assertIn(b"model_not_allowed", payload)
        # 本丸: 1バイトも外へ出ていない。
        self.assertEqual(self.forwarder.calls, [])

    def test_unknown_slug_is_never_forwarded(self):
        status, _ = self.post({"model": "someone/else", "input": "x"})
        self.assertEqual(status, 400)
        self.assertEqual(self.forwarder.calls, [])

    def test_missing_model_is_never_forwarded(self):
        status, _ = self.post({"input": "x"})
        self.assertEqual(status, 400)
        self.assertEqual(self.forwarder.calls, [])

    def test_every_registry_model_is_allowed(self):
        registry = json.loads((ROOT / "models/registry.json").read_text(encoding="utf-8"))["models"]
        allowing = guard_module.Guard(registry, key_provider=lambda: "k")
        for slug in registry:
            self.assertTrue(allowing.allows(slug))
        self.assertFalse(allowing.allows(COLLATERAL))

    def test_missing_local_auth_is_rejected_before_forwarding(self):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/v1/responses",
            data=json.dumps(
                {"model": "deepseek/deepseek-v4-pro", "input": "must-not-be-read"}
            ).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json", "Connection": "close"},
        )
        try:
            urllib.request.urlopen(request, timeout=10)
        except urllib.error.HTTPError as error:
            self.assertEqual(error.code, 401)
            error.close()
        else:
            self.fail("unauthenticated request unexpectedly succeeded")
        self.assertEqual(self.forwarder.calls, [])
        self.assertEqual(self.log_records(), [])


class LoggingTests(GuardTestCase):
    def test_denied_request_is_logged_without_body(self):
        self.post({"model": COLLATERAL, "input": "canary-phrase-7731"})
        records = self.log_records()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["model"], COLLATERAL)
        self.assertEqual(records[0]["decision"], "denied")
        self.assertNotIn("canary-phrase-7731", self.log.read_text())

    def test_forwarded_request_logs_no_key(self):
        self.post({"model": "z-ai/glm-5.2", "input": "hi"})
        text = self.log.read_text()
        self.assertIn("forwarded", text)
        self.assertNotIn("sk-or-test-key", text)


class ZdrTests(GuardTestCase):
    def test_forwarded_body_forces_zdr(self):
        self.post({"model": "z-ai/glm-5.2", "input": "hi"})
        body = json.loads(self.forwarder.calls[0][0])
        self.assertIs(body["provider"]["zdr"], True)

    def test_existing_provider_settings_are_preserved(self):
        self.post(
            {"model": "z-ai/glm-5.2", "input": "hi", "provider": {"allow_fallbacks": False}}
        )
        body = json.loads(self.forwarder.calls[0][0])
        self.assertIs(body["provider"]["zdr"], True)
        self.assertIs(body["provider"]["allow_fallbacks"], False)


class FailureTests(GuardTestCase):
    def test_credential_failure_does_not_forward(self):
        self.guard.key_provider = lambda: (_ for _ in ()).throw(RuntimeError("no keychain"))
        status, _ = self.post({"model": "z-ai/glm-5.2", "input": "hi"})
        self.assertEqual(status, 503)
        self.assertEqual(self.forwarder.calls, [])

    def test_upstream_failure_is_reported_as_502(self):
        def explode(body, key):
            raise OSError("connection reset")

        self.guard.forwarder = explode
        status, _ = self.post({"model": "z-ai/glm-5.2", "input": "hi"})
        self.assertEqual(status, 502)

    def test_upstream_error_status_is_relayed(self):
        self.guard.forwarder = RecordingForwarder(status=429, payload=b'{"error":"rate"}')
        status, payload = self.post({"model": "z-ai/glm-5.2", "input": "hi"})
        self.assertEqual(status, 429)
        self.assertIn(b"rate", payload)


class HealthTests(GuardTestCase):
    def test_health_confirms_our_guard(self):
        self.assertTrue(guard_module.health_ok(self.port, "test-nonce"))

    def test_health_rejects_wrong_nonce(self):
        # ポート横取りの検出。別プロセスが同じportに居たら起動を中止する。
        self.assertFalse(guard_module.health_ok(self.port, "other-nonce"))

    def test_health_on_dead_port_is_false(self):
        free = guard_module.free_port()
        self.assertFalse(guard_module.health_ok(free, "test-nonce"))


class _SlowUpstreamHandler(BaseHTTPRequestHandler):
    """SSEを小刻みに吐く上流。guardが貯め込まないことを見るために使う。"""

    chunks = 20
    interval = 0.1

    def log_message(self, *args):
        return

    def do_POST(self):
        length = int(self.headers.get("content-length") or 0)
        self.rfile.read(length)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        for index in range(self.chunks):
            payload = f"data: token {index}\n\n".encode("utf-8")
            self.wfile.write(f"{len(payload):X}\r\n".encode("ascii"))
            self.wfile.write(payload)
            self.wfile.write(b"\r\n")
            self.wfile.flush()
            time.sleep(self.interval)
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()


class StreamingRelayTests(unittest.TestCase):
    """中継経路を実HTTPResponseで通す。

    他のテストは forwarder を `io.BytesIO` で差し替えるので、`read` と `read1`
    の差が出ない。`HTTPResponse.read(n)` は n バイト溜まるかレスポンス完了まで
    返らないため、それを使うとSSEが 8KB 単位でしか届かない（実測では総計2.0秒の
    ストリームが完了後に一度だけ届いた）。上流を自前で立てて実測する。
    """

    def setUp(self):
        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), _SlowUpstreamHandler)
        self.upstream.daemon_threads = True
        threading.Thread(target=self.upstream.serve_forever, daemon=True).start()
        self.addCleanup(self.upstream.server_close)
        self.addCleanup(self.upstream.shutdown)
        endpoint = f"http://127.0.0.1:{self.upstream.server_address[1]}/api/v1/responses"
        patcher = mock.patch.object(guard_module, "ENDPOINT", endpoint)
        patcher.start()
        self.addCleanup(patcher.stop)

        guard = guard_module.Guard(
            allowed_models=ALLOWED,
            key_provider=lambda: "sk-or-test-key",
            nonce="test-nonce",
            access_token="local-token",
        )
        self.server, self.port = guard_module.serve(guard)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def test_stream_is_relayed_incrementally_and_intact(self):
        total = _SlowUpstreamHandler.chunks * _SlowUpstreamHandler.interval
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/v1/responses",
            data=json.dumps({"model": "z-ai/glm-5.2", "input": "hi"}).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": "Bearer local-token",
                "Content-Type": "application/json",
                "Connection": "close",
            },
        )
        started = time.monotonic()
        with urllib.request.urlopen(request, timeout=30) as response:
            first = response.read1(8192)
            first_at = time.monotonic() - started
            body = first
            while True:
                chunk = response.read1(8192)
                if not chunk:
                    break
                body += chunk

        # 上流が吐き終わるより十分早く最初のトークンが届くこと。
        self.assertTrue(first)
        self.assertLess(first_at, total / 4)
        expected = b"".join(
            f"data: token {index}\n\n".encode("utf-8")
            for index in range(_SlowUpstreamHandler.chunks)
        )
        self.assertEqual(expected, body)


if __name__ == "__main__":
    unittest.main()
