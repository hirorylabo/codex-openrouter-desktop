from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest import mock
import urllib.error


ROOT = Path(__file__).resolve().parents[1]


def load_doctor_template():
    source = (ROOT / "portable/templates/codex-openrouter-doctor.py.in").read_text(
        encoding="utf-8"
    )
    rendered = source.replace("@@PYTHON@@", "/usr/bin/python3").replace(
        "@@USER_HOME@@", "/tmp/codex-openrouter-test-home"
    )
    temporary = tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False)
    try:
        temporary.write(rendered)
        temporary.close()
        spec = importlib.util.spec_from_file_location("tested_doctor", temporary.name)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        return module
    finally:
        Path(temporary.name).unlink(missing_ok=True)


class RepositoryTests(unittest.TestCase):
    def test_no_packaged_secrets_apps_asar_or_runtime_databases(self) -> None:
        forbidden_suffixes = {".asar", ".db", ".sqlite", ".sqlite3"}
        forbidden_names = {"auth.json", ".env", "Cookies", "Login Data"}
        problems = []
        for path in ROOT.rglob("*"):
            if ".git" in path.parts or "node_modules" in path.parts or "__pycache__" in path.parts:
                continue
            if path.is_dir() and path.suffix == ".app":
                problems.append(str(path.relative_to(ROOT)))
            if path.is_file() and (path.suffix in forbidden_suffixes or path.name in forbidden_names):
                problems.append(str(path.relative_to(ROOT)))
        self.assertEqual([], problems)

    def test_no_openrouter_key_literal(self) -> None:
        key_pattern = re.compile(rb"sk-or-v1-[A-Za-z0-9_-]{20,}")
        matches = []
        for path in ROOT.rglob("*"):
            if not path.is_file() or any(part in {".git", "node_modules", "__pycache__"} for part in path.parts):
                continue
            if key_pattern.search(path.read_bytes()):
                matches.append(str(path.relative_to(ROOT)))
        self.assertEqual([], matches)

    def test_registry_profile_and_adapter_inventories_are_consistent(self) -> None:
        registry = json.loads((ROOT / "models/registry.json").read_text(encoding="utf-8"))
        profile = json.loads((ROOT / "profiles/default.json").read_text(encoding="utf-8"))
        adapters = json.loads((ROOT / "adapters/index.json").read_text(encoding="utf-8"))
        self.assertEqual(1, registry["schema_version"])
        self.assertEqual(set(profile["models"]), set(registry["models"]))
        self.assertGreaterEqual(len(adapters["adapters"]), 1)
        self.assertTrue(all(adapter["patch_strategy"] == "exact" for adapter in adapters["adapters"]))

    def test_upstream_license_contract_is_unlicense(self) -> None:
        manifest = json.loads((ROOT / "portable/manifest.json").read_text(encoding="utf-8"))
        upstream = manifest["upstream_patcher"]
        self.assertEqual("Unlicense", upstream["license"])
        self.assertRegex(upstream["license_sha256"], r"^[0-9a-f]{64}$")

    def test_network_doctor_verifies_request_zdr_generation_provider(self) -> None:
        source = (ROOT / "portable/templates/codex-openrouter-doctor.py.in").read_text(encoding="utf-8")
        self.assertIn('provider: dict[str, object] = {"zdr": True}', source)
        self.assertIn('provider["order"] = provider_tags', source)
        self.assertIn('provider["allow_fallbacks"] = False', source)
        self.assertIn('"X-Generation-Id"', source)
        self.assertIn('metadata.get("provider_name")', source)
        self.assertIn("/api/v1/endpoints/zdr", source)

    def test_network_request_pins_active_zdr_provider_tags(self) -> None:
        doctor = load_doctor_template()

        class Response:
            status = 200
            headers = {"X-Generation-Id": "gen-test"}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return b'{"model":"example/model"}'

        with mock.patch.object(doctor.urllib.request, "urlopen", return_value=Response()) as urlopen:
            status, body, generation_id = doctor.request_model(
                "secret", "example/model", "high", ["provider-a", "provider-b"]
            )

        payload = json.loads(urlopen.call_args.args[0].data)
        self.assertEqual(200, status)
        self.assertEqual("example/model", body["model"])
        self.assertEqual("gen-test", generation_id)
        self.assertEqual(
            {
                "zdr": True,
                "order": ["provider-a", "provider-b"],
                "allow_fallbacks": False,
            },
            payload["provider"],
        )

    def test_generation_metadata_retries_eventual_404(self) -> None:
        doctor = load_doctor_template()
        not_found = urllib.error.HTTPError(
            "https://openrouter.ai/api/v1/generation?id=gen-test",
            404,
            "Not Found",
            {},
            None,
        )
        with (
            mock.patch.object(
                doctor,
                "authenticated_json",
                side_effect=[not_found, not_found, {"data": {"provider_name": "Example"}}],
            ) as request,
            mock.patch.object(doctor.time, "sleep") as sleep,
        ):
            metadata = doctor.generation_metadata("secret", "gen-test")
        self.assertEqual("Example", metadata["provider_name"])
        self.assertEqual(3, request.call_count)
        self.assertEqual([mock.call(2), mock.call(2)], sleep.call_args_list)

    def test_generation_metadata_does_not_retry_non_404(self) -> None:
        doctor = load_doctor_template()
        unauthorized = urllib.error.HTTPError(
            "https://openrouter.ai/api/v1/generation?id=gen-test",
            401,
            "Unauthorized",
            {},
            None,
        )
        with (
            mock.patch.object(doctor, "authenticated_json", side_effect=unauthorized),
            mock.patch.object(doctor.time, "sleep") as sleep,
            self.assertRaises(urllib.error.HTTPError),
        ):
            doctor.generation_metadata("secret", "gen-test")
        sleep.assert_not_called()

    def test_runtime_process_match_excludes_wrapper_commands(self) -> None:
        doctor = load_doctor_template()
        executable = doctor.OPENROUTER_APP / "Contents/MacOS/ChatGPT"
        real = f"  123 {executable} --user-data-dir=/tmp/user-data"
        wrapper = f"  456 /bin/zsh -lc echo {executable}"
        self.assertEqual(
            [(123, f"{executable} --user-data-dir=/tmp/user-data")],
            doctor.matching_processes(f"{real}\n{wrapper}\n", executable),
        )

    def test_launcher_process_match_is_anchored_to_command_start(self) -> None:
        source = (ROOT / "portable/templates/codex-openrouter-app.zsh.in").read_text(
            encoding="utf-8"
        )
        self.assertIn("codex_openrouter.processes", source)
        self.assertIn('--executable "$EXECUTABLE"', source)
        self.assertNotIn("/bin/ps -axo pid=,command=", source)

    def test_runtime_rebuild_has_no_build_specific_literals(self) -> None:
        source = (ROOT / "portable/templates/codex-openrouter-rebuild.zsh.in").read_text(encoding="utf-8")
        self.assertIn("$ACTIVE_ADAPTER", source)
        self.assertIn('active adapter is not exactly present in index', source)
        self.assertNotIn("patch_build_6321.py", source)
        self.assertNotIn('EXPECTED_BUILD="6321"', source)

    def test_upgrade_validates_staging_files_with_final_runtime_paths(self) -> None:
        renderer = (ROOT / "portable/render_runtime.py").read_text(encoding="utf-8")
        upgrade = (ROOT / "src/codex_openrouter/upgrade.py").read_text(encoding="utf-8")
        doctor = (ROOT / "portable/templates/codex-openrouter-doctor.py.in").read_text(
            encoding="utf-8"
        )
        self.assertIn('parser.add_argument("--runtime-home", type=Path)', renderer)
        self.assertIn('"--runtime-home",', upgrade)
        self.assertIn('"CODEX_OPENROUTER_RUNTIME_HOME": str(paths.codex_home)', upgrade)
        self.assertIn("RUNTIME_CATALOG = RUNTIME_HOME", doctor)

    def test_desktop_launcher_has_a_generated_project_icon(self) -> None:
        info = (ROOT / "portable/launcher/Info.plist").read_text(encoding="utf-8")
        installer = (ROOT / "portable/install.sh").read_text(encoding="utf-8")
        self.assertIn("CFBundleIconFile", info)
        self.assertIn("AppIcon", info)
        self.assertIn("build_icon.zsh", installer)
        self.assertTrue((ROOT / "portable/launcher/CreateLauncherIcon.swift").is_file())


if __name__ == "__main__":
    unittest.main()
