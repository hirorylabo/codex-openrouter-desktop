from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from codex_openrouter import catalog  # noqa: E402

REGISTRY = json.loads((ROOT / "models/registry.json").read_text(encoding="utf-8"))["models"]


def native(slug: str, visibility: str = "list", priority: int = 1) -> dict:
    """build 6396のbundled entryの形を最小限で再現したfixture。"""
    return {
        "slug": slug,
        "display_name": slug.upper(),
        "description": "native",
        "visibility": visibility,
        "priority": priority,
        "default_reasoning_level": "low",
        "supported_reasoning_levels": [
            {"effort": "low", "description": "Fast"},
            {"effort": "high", "description": "Deep"},
        ],
        "context_window": 400000,
        "max_context_window": 400000,
        "input_modalities": ["text", "image"],
        "supports_parallel_tool_calls": True,
        "supported_in_api": True,
        "multi_agent_version": "v2",
        "additional_speed_tiers": ["fast"],
        "service_tiers": [{"id": "priority", "name": "Fast"}],
        "availability_nux": {"message": "..."},
        "upgrade": None,
        "shell_type": "shell_command",
    }


NATIVES = [
    native("gpt-hidden", visibility="hide", priority=9),
    native("gpt-first", visibility="list", priority=1),
    native("gpt-second", visibility="list", priority=2),
]


class TemplateTests(unittest.TestCase):
    def test_picks_first_listed_native_not_hidden(self):
        self.assertEqual(catalog.clone_template(NATIVES)["slug"], "gpt-first")

    def test_rejects_catalog_without_listed_native(self):
        with self.assertRaises(catalog.CatalogError):
            catalog.clone_template([native("only-hidden", visibility="hide")])


class BuildTests(unittest.TestCase):
    def setUp(self):
        self.document = catalog.build(NATIVES, REGISTRY)
        self.by_slug = {m["slug"]: m for m in self.document["models"]}

    def test_keeps_every_native_entry(self):
        for model in NATIVES:
            self.assertIn(model["slug"], self.by_slug)

    def test_adds_every_registry_model_as_listed(self):
        for slug in REGISTRY:
            self.assertEqual(self.by_slug[slug]["visibility"], "list")

    def test_openrouter_entries_are_prefixed(self):
        for slug in REGISTRY:
            self.assertTrue(self.by_slug[slug]["display_name"].startswith(catalog.OR_PREFIX))

    def test_efforts_follow_registry(self):
        for slug, spec in REGISTRY.items():
            efforts = [
                level["effort"] for level in self.by_slug[slug]["supported_reasoning_levels"]
            ]
            self.assertEqual(efforts, list(spec.get("efforts") or []))

    def test_neutralises_native_only_capability_fields(self):
        # nativeのmulti_agent_versionを継ぐと、ORモデルがnative由来の機能を主張する。
        for slug in REGISTRY:
            self.assertIsNone(self.by_slug[slug]["multi_agent_version"])
        self.assertEqual(self.by_slug["gpt-first"]["multi_agent_version"], "v2")

    def test_openrouter_priorities_sort_after_natives(self):
        highest_native = max(m["priority"] for m in NATIVES)
        for slug in REGISTRY:
            self.assertGreater(self.by_slug[slug]["priority"], highest_native)

    def test_does_not_mutate_the_template(self):
        self.assertEqual(self.by_slug["gpt-first"]["display_name"], "GPT-FIRST")

    def test_passes_its_own_contract(self):
        catalog.validate(self.document, REGISTRY)


class ValidateTests(unittest.TestCase):
    def setUp(self):
        self.document = catalog.build(NATIVES, REGISTRY)

    def _expect_error(self, document):
        with self.assertRaises(catalog.CatalogError):
            catalog.validate(document, REGISTRY)

    def test_rejects_missing_openrouter_model(self):
        victim = next(iter(REGISTRY))
        self.document["models"] = [
            m for m in self.document["models"] if m["slug"] != victim
        ]
        self._expect_error(self.document)

    def test_rejects_duplicate_slug(self):
        self.document["models"].append(dict(self.document["models"][0]))
        self._expect_error(self.document)

    def test_rejects_when_no_native_remains(self):
        self.document["models"] = [m for m in self.document["models"] if m["slug"] in REGISTRY]
        self._expect_error(self.document)

    def test_rejects_effort_drift_from_registry(self):
        slug = next(iter(REGISTRY))
        for model in self.document["models"]:
            if model["slug"] == slug:
                model["supported_reasoning_levels"] = [{"effort": "bogus", "description": "x"}]
        self._expect_error(self.document)

    def test_rejects_openrouter_model_hidden_from_picker(self):
        slug = next(iter(REGISTRY))
        for model in self.document["models"]:
            if model["slug"] == slug:
                model["visibility"] = "hide"
        self._expect_error(self.document)

    def test_rejects_empty_document(self):
        self._expect_error({"models": []})

    def test_rejects_models_outside_the_installed_profile(self):
        selected_slug = next(iter(REGISTRY))
        selected = {selected_slug: REGISTRY[selected_slug]}
        with self.assertRaises(catalog.CatalogError):
            catalog.validate(self.document, selected, REGISTRY)


class WriteTests(unittest.TestCase):
    def test_write_is_atomic_and_keeps_one_generation(self):
        import tempfile

        document = catalog.build(NATIVES, REGISTRY)
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "catalogs" / "composite.json"
            catalog.write(document, target)
            self.assertTrue(target.is_file())
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            self.assertFalse(target.with_suffix(".json.previous").exists())

            document["models"][0]["display_name"] = "CHANGED"
            catalog.write(document, target)
            previous = target.with_suffix(".json.previous")
            self.assertTrue(previous.is_file())
            self.assertNotIn("CHANGED", previous.read_text(encoding="utf-8"))
            self.assertIn("CHANGED", target.read_text(encoding="utf-8"))


class InstalledBuildTests(unittest.TestCase):
    """実機のbundled catalogに対する契約。純正appが無い環境ではskip。"""

    CODEX = Path("/Applications/ChatGPT.app/Contents/Resources/codex")

    def setUp(self):
        if not self.CODEX.is_file():
            self.skipTest("純正ChatGPT.appがありません")

    def test_builds_and_validates_against_the_installed_build(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            natives = catalog.bundled_models(self.CODEX, Path(directory))
            document = catalog.build(natives, REGISTRY)
            catalog.validate(document, REGISTRY)
            listed = [m for m in document["models"] if m.get("visibility") == "list"]
            # native(list) + OR 5件 が picker に並ぶ。
            self.assertEqual(len(listed), len(REGISTRY) + sum(
                1 for m in natives if m.get("visibility") == "list"
            ))


if __name__ == "__main__":
    unittest.main()
