from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from codex_openrouter import configblock, doctor as doctor_module, guard as guard_module  # noqa: E402
from codex_openrouter.profile import resolve_profile  # noqa: E402
from codex_openrouter.supervisor import (  # noqa: E402
    CATALOG_BLOCK,
    PROVIDER_BLOCK,
    State,
    provider_block_body,
)
from tests_support import make_paths  # noqa: E402

REGISTRY = json.loads((ROOT / "models/registry.json").read_text(encoding="utf-8"))["models"]
OR_SLUG = next(iter(REGISTRY))

BASE_CONFIG = 'model = "gpt-5.6-sol"\nmodel_reasoning_effort = "low"\n'
PROVIDER_BODY = provider_block_body(0)
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
        doctor_module.check_config(self.doctor, self.paths, REGISTRY, set(REGISTRY))
        self.assertTrue(
            any("provider block" in f for f in self.doctor.failures), self.doctor.failures
        )

    def test_provider_block_present_passes(self):
        self.write_config(
            configblock.insert_block(BASE_CONFIG, PROVIDER_BLOCK, PROVIDER_BODY, top_level=False)
        )
        doctor_module.check_config(self.doctor, self.paths, REGISTRY, set(REGISTRY))
        self.assertEqual(self.doctor.failures, [])

    def test_openrouter_model_without_catalog_block_is_a_failure(self):
        # catalogを外したのにmodelがOR slugのままだと、純正起動時に解決できない。
        text = configblock.insert_block(
            BASE_CONFIG, PROVIDER_BLOCK, PROVIDER_BODY, top_level=False
        )
        text = configblock.upsert_top_level(text, "model", OR_SLUG)
        text = configblock.upsert_top_level(text, "model_provider", "openrouter")
        self.write_config(text)
        doctor_module.check_config(self.doctor, self.paths, REGISTRY, set(REGISTRY))
        self.assertTrue(any("catalog" in f for f in self.doctor.failures), self.doctor.failures)

    def test_provider_inconsistent_with_model_is_a_failure(self):
        text = configblock.insert_block(
            BASE_CONFIG, PROVIDER_BLOCK, PROVIDER_BODY, top_level=False
        )
        text = configblock.insert_block(text, CATALOG_BLOCK, CATALOG_BODY, top_level=True)
        # nativeモデルなのにopenrouterへ向いている。
        text = configblock.upsert_top_level(text, "model_provider", "openrouter")
        self.write_config(text)
        doctor_module.check_config(self.doctor, self.paths, REGISTRY, set(REGISTRY))
        self.assertTrue(any("矛盾" in f for f in self.doctor.failures), self.doctor.failures)

    def test_key_in_config_is_a_failure(self):
        text = configblock.insert_block(
            BASE_CONFIG, PROVIDER_BLOCK, PROVIDER_BODY, top_level=False
        )
        self.write_config(text + '\napi_key = "sk-or-v1-abcdefghijklmnop"\n')
        doctor_module.check_config(self.doctor, self.paths, REGISTRY, set(REGISTRY))
        self.assertTrue(any("key" in f for f in self.doctor.failures), self.doctor.failures)


class CatalogCheckTests(DoctorTestCase):
    def test_missing_catalog_is_a_warning_not_a_failure(self):
        doctor_module.check_catalog(self.doctor, self.paths, REGISTRY, set(REGISTRY))
        self.assertEqual(self.doctor.failures, [])

    def test_broken_catalog_is_a_failure(self):
        self.paths.composite_catalog.parent.mkdir(parents=True, exist_ok=True)
        self.paths.composite_catalog.write_text('{"models": []}', encoding="utf-8")
        doctor_module.check_catalog(self.doctor, self.paths, REGISTRY, set(REGISTRY))
        self.assertTrue(self.doctor.failures)


class TemplateCheckTests(DoctorTestCase):
    """cloneテンプレートのdrift検知。純正appの更新でここが鳴る。"""

    TEMPLATE = {"slug": "gpt-x", "display_name": "X", "visibility": "list"}

    def _run_with(self, natives):
        with mock.patch.object(
            doctor_module.catalog_module, "bundled_models", return_value=natives
        ):
            doctor_module.check_catalog_template(self.doctor, self.paths)

    def test_known_template_passes(self):
        self._run_with([self.TEMPLATE])
        self.assertEqual(self.doctor.failures, [])
        self.assertEqual(self.doctor.warnings, [])

    def test_new_field_is_a_warning_not_a_failure(self):
        # 未知フィールドは即座に有害とは限らない。継がせるか中和するかは人間が決める。
        self._run_with([self.TEMPLATE | {"brand_new_capability": True}])
        self.assertEqual(self.doctor.failures, [])
        self.assertTrue(
            any("brand_new_capability" in w for w in self.doctor.warnings),
            self.doctor.warnings,
        )

    def test_unreadable_stock_build_is_a_warning_not_a_failure(self):
        # 純正app不在は check_stock の担当。ここで二重に落とさない。
        doctor_module.check_catalog_template(self.doctor, self.paths)
        self.assertEqual(self.doctor.failures, [])
        self.assertTrue(self.doctor.warnings)


class TemplateSnapshotCheckTests(DoctorTestCase):
    """前回catalogを組んだbuildとの差分。純正appの更新から次回起動までの窓で鳴る。"""

    TEMPLATE = {"slug": "gpt-x", "display_name": "X", "visibility": "list"}

    def write_snapshot(self, build: str, template: dict) -> None:
        self.paths.state_dir.mkdir(parents=True, exist_ok=True)
        self.paths.clone_template_snapshot.write_text(
            json.dumps({"schema_version": 1, "version": "26.1", "build": build,
                        "template": template}),
            encoding="utf-8",
        )

    def _run_with(self, natives, build="6720"):
        with mock.patch.object(doctor_module, "stock_build_id", return_value=("26.2", build)):
            doctor_module.check_catalog_template_snapshot(self.doctor, self.paths, natives)

    def test_no_snapshot_says_nothing(self):
        # 初回起動前はsnapshotが無い。そのことを利用者へ報告しても行動に繋がらない。
        self._run_with([self.TEMPLATE])
        self.assertEqual(self.doctor.warnings, [])

    def test_same_build_says_nothing(self):
        self.write_snapshot("6720", self.TEMPLATE)
        self._run_with([self.TEMPLATE | {"brand_new_capability": True}])
        self.assertEqual(self.doctor.warnings, [])

    def test_new_build_reports_the_moved_field_names(self):
        self.write_snapshot("6662", self.TEMPLATE)
        self._run_with([self.TEMPLATE | {"brand_new_capability": True}])
        self.assertEqual(self.doctor.failures, [])
        self.assertTrue(
            any("brand_new_capability" in w and "6662" in w for w in self.doctor.warnings),
            self.doctor.warnings,
        )

    def test_new_build_with_identical_template_says_nothing(self):
        self.write_snapshot("6662", self.TEMPLATE)
        self._run_with([self.TEMPLATE])
        self.assertEqual(self.doctor.warnings, [])


class ToolWireBuildCheckTests(DoctorTestCase):
    def test_latest_build_is_accepted(self):
        with mock.patch.object(
            doctor_module, "stock_build_id", return_value=("26.818", "6962")
        ):
            doctor_module.check_tool_wire_build(
                self.doctor, self.paths, ROOT / "models/registry.json"
            )
        self.assertEqual([], self.doctor.failures)

    def test_unknown_build_blocks_only_openrouter_compatibility(self):
        with mock.patch.object(
            doctor_module, "stock_build_id", return_value=("26.900", "7000")
        ):
            doctor_module.check_tool_wire_build(
                self.doctor, self.paths, ROOT / "models/registry.json"
            )
        self.assertTrue(any("互換確認待ち" in item for item in self.doctor.failures))


class ManifestCheckTests(DoctorTestCase):
    def setUp(self):
        super().setUp()
        self.profile = resolve_profile(ROOT / "models/registry.json", ROOT / "profiles/default.json")
        self.paths.state_dir.mkdir(parents=True, exist_ok=True)

    def write_manifest(self, document: dict) -> None:
        self.paths.install_manifest.write_text(json.dumps(document), encoding="utf-8")

    def test_matching_digest_passes(self):
        self.write_manifest({"schema_version": 5, "profile_digest": self.profile.digest})
        doctor_module.check_manifest(self.doctor, self.paths, self.profile)
        self.assertEqual(self.doctor.failures, [])

    def test_drifted_digest_is_a_failure(self):
        self.write_manifest({"schema_version": 5, "profile_digest": "0" * 64})
        doctor_module.check_manifest(self.doctor, self.paths, self.profile)
        self.assertTrue(self.doctor.failures)

    def test_older_manifest_without_a_digest_is_a_warning(self):
        self.write_manifest({"schema_version": 4})
        doctor_module.check_manifest(self.doctor, self.paths, self.profile)
        self.assertEqual(self.doctor.failures, [])

    def test_missing_manifest_is_a_warning(self):
        doctor_module.check_manifest(self.doctor, self.paths, self.profile)
        self.assertEqual(self.doctor.failures, [])


class SecretScanTests(DoctorTestCase):
    def test_exported_key_is_a_warning_not_a_failure(self):
        """他ツール用のexportは正当。これでinstallを止めてはいけない。"""
        import os

        launcher = self.paths.bin_dir / "codex-openrouter-app"
        launcher.parent.mkdir(parents=True, exist_ok=True)
        launcher.write_text("unset OPENROUTER_API_KEY CODEX_ACCESS_TOKEN\n")
        previous = os.environ.get("OPENROUTER_API_KEY")
        os.environ["OPENROUTER_API_KEY"] = "sk-or-testonlyvalue"
        self.addCleanup(
            lambda: os.environ.__setitem__("OPENROUTER_API_KEY", previous)
            if previous is not None
            else os.environ.pop("OPENROUTER_API_KEY", None)
        )
        doctor_module.check_secret_scan(self.doctor, self.paths)
        # process argumentsの検査はマシン全体の状態に依存するので、
        # ここでは「環境変数とランチャーについてのfailureが無いこと」だけを見る。
        self.assertEqual(
            [f for f in self.doctor.failures if "OPENROUTER_API_KEY" in f], []
        )

    def test_launcher_that_keeps_the_key_is_a_failure(self):
        launcher = self.paths.bin_dir / "codex-openrouter-app"
        launcher.parent.mkdir(parents=True, exist_ok=True)
        launcher.write_text("exec ChatGPT\n")
        doctor_module.check_secret_scan(self.doctor, self.paths)
        self.assertTrue(any("渡ります" in f for f in self.doctor.failures), self.doctor.failures)


class GuardCheckTests(DoctorTestCase):
    def test_free_port_is_reported_as_stopped(self):
        doctor_module.check_guard(self.doctor, self.paths)
        self.assertEqual(self.doctor.failures, [])

    def test_running_guard_passes(self):
        instance = guard_module.Guard(
            REGISTRY, key_provider=lambda: "k", nonce="n", access_token="local"
        )
        server, port = guard_module.serve(instance)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        State(active=True, guard_port=port, guard_nonce="n").save(self.paths.supervisor_state)
        doctor_module.check_guard(self.doctor, self.paths)
        self.assertEqual(self.doctor.failures, [])

    def test_active_state_without_listener_is_a_failure(self):
        port = guard_module.free_port()
        State(active=True, guard_port=port, guard_nonce="n").save(
            self.paths.supervisor_state
        )
        doctor_module.check_guard(self.doctor, self.paths)
        self.assertTrue(self.doctor.failures)

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
        State(active=True, guard_port=port, guard_nonce="ours").save(
            self.paths.supervisor_state
        )
        doctor_module.check_guard(self.doctor, self.paths)
        self.assertTrue(self.doctor.failures)


if __name__ == "__main__":
    unittest.main()
