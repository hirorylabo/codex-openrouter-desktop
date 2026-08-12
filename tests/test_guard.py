from __future__ import annotations

import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
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
        )
        self.server, self.port = guard_module.serve(self.guard)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def post(self, document) -> tuple[int, bytes]:
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/v1/responses",
            data=json.dumps(document).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json", "Connection": "close"},
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.read()

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


if __name__ == "__main__":
    unittest.main()
