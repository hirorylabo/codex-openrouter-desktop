from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from codex_openrouter import configblock, doctor as doctor_module, guard as guard_module  # noqa: E402
from codex_openrouter.supervisor import CATALOG_BLOCK, PROVIDER_BLOCK  # noqa: E402
from tests_support import make_paths  # noqa: E402

REGISTRY = json.loads((ROOT / "models/registry.json").read_text(encoding="utf-8"))["models"]
OR_SLUG = next(iter(REGISTRY))

BASE_CONFIG = 'model = "gpt-5.6-sol"\nmodel_reasoning_effort = "low"\n'
PROVIDER_BODY = '[model_providers.openrouter]\nname = "OpenRouter"'
CATALOG_BODY = 'model_catalog_json = "/tmp/x.json"'


class DoctorTestCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.paths = make_paths(self.root)
        self.paths.shared_home.mkdir(parents=True)
        self.paths.shared_config.write_text(BASE_CONFIG, encoding="utf-8")
        self.doctor = doctor_module.Doctor()

    def write_config(self, text: str) -> None:
        configblock.atomic_write(self.paths.shared_config, text)


class ConfigCheckTests(DoctorTestCase):
    def test_missing_provider_block_is_a_failure(self):
        doctor_module.check_config(self.doctor, self.paths, REGISTRY)
        self.assertTrue(
            any("provider block" in f for f in self.doctor.failures), self.doctor.failures
        )

    def test_provider_block_present_passes(self):
        self.write_config(
            configblock.insert_block(BASE_CONFIG, PROVIDER_BLOCK, PROVIDER_BODY, top_level=False)
        )
        doctor_module.check_config(self.doctor, self.paths, REGISTRY)
        self.assertEqual(self.doctor.failures, [])

    def test_openrouter_model_without_catalog_block_is_a_failure(self):
        # catalogを外したのにmodelがOR slugのままだと、純正起動時に解決できない。
        text = configblock.insert_block(
            BASE_CONFIG, PROVIDER_BLOCK, PROVIDER_BODY, top_level=False
        )
        text = configblock.upsert_top_level(text, "model", OR_SLUG)
        text = configblock.upsert_top_level(text, "model_provider", "openrouter")
        self.write_config(text)
        doctor_module.check_config(self.doctor, self.paths, REGISTRY)
        self.assertTrue(any("catalog" in f for f in self.doctor.failures), self.doctor.failures)

    def test_provider_inconsistent_with_model_is_a_failure(self):
        text = configblock.insert_block(
            BASE_CONFIG, PROVIDER_BLOCK, PROVIDER_BODY, top_level=False
        )
        text = configblock.insert_block(text, CATALOG_BLOCK, CATALOG_BODY, top_level=True)
        # nativeモデルなのにopenrouterへ向いている。
        text = configblock.upsert_top_level(text, "model_provider", "openrouter")
        self.write_config(text)
        doctor_module.check_config(self.doctor, self.paths, REGISTRY)
        self.assertTrue(any("矛盾" in f for f in self.doctor.failures), self.doctor.failures)

    def test_key_in_config_is_a_failure(self):
        text = configblock.insert_block(
            BASE_CONFIG, PROVIDER_BLOCK, PROVIDER_BODY, top_level=False
        )
        self.write_config(text + '\napi_key = "sk-or-v1-abcdefghijklmnop"\n')
        doctor_module.check_config(self.doctor, self.paths, REGISTRY)
        self.assertTrue(any("key" in f for f in self.doctor.failures), self.doctor.failures)


class CatalogCheckTests(DoctorTestCase):
    def test_missing_catalog_is_a_warning_not_a_failure(self):
        doctor_module.check_catalog(self.doctor, self.paths, REGISTRY)
        self.assertEqual(self.doctor.failures, [])

    def test_broken_catalog_is_a_failure(self):
        self.paths.composite_catalog.parent.mkdir(parents=True, exist_ok=True)
        self.paths.composite_catalog.write_text('{"models": []}', encoding="utf-8")
        doctor_module.check_catalog(self.doctor, self.paths, REGISTRY)
        self.assertTrue(self.doctor.failures)


class GuardCheckTests(DoctorTestCase):
    def test_free_port_is_reported_as_stopped(self):
        port = guard_module.free_port()
        doctor_module.check_guard(self.doctor, self.paths, port=port)
        self.assertEqual(self.doctor.failures, [])

    def test_running_guard_passes(self):
        instance = guard_module.Guard(REGISTRY, key_provider=lambda: "k", nonce="n")
        server, port = guard_module.serve(instance)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        doctor_module.check_guard(self.doctor, self.paths, port=port)
        self.assertEqual(self.doctor.failures, [])

    def test_foreign_listener_on_the_port_is_a_failure(self):
        import socketserver
        import threading

        class Silent(socketserver.BaseRequestHandler):
            def handle(self):
                self.request.recv(1024)
                self.request.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}")

        server = socketserver.TCPServer(("127.0.0.1", 0), Silent)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        doctor_module.check_guard(self.doctor, self.paths, port=port)
        self.assertTrue(self.doctor.failures)


if __name__ == "__main__":
    unittest.main()
