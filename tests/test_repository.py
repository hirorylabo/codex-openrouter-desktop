from __future__ import annotations

import json
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]


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
        self.assertEqual(1, len(adapters["adapters"]))
        self.assertEqual("exact", adapters["adapters"][0]["patch_strategy"])

    def test_upstream_license_contract_is_unlicense(self) -> None:
        manifest = json.loads((ROOT / "portable/manifest.json").read_text(encoding="utf-8"))
        upstream = manifest["upstream_patcher"]
        self.assertEqual("Unlicense", upstream["license"])
        self.assertRegex(upstream["license_sha256"], r"^[0-9a-f]{64}$")

    def test_network_doctor_verifies_request_zdr_generation_provider(self) -> None:
        source = (ROOT / "portable/templates/codex-openrouter-doctor.py.in").read_text(encoding="utf-8")
        self.assertIn('"provider": {"zdr": True}', source)
        self.assertIn('"X-Generation-Id"', source)
        self.assertIn('metadata.get("provider_name")', source)
        self.assertIn("/api/v1/endpoints/zdr", source)


if __name__ == "__main__":
    unittest.main()
