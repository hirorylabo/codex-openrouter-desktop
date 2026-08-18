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
    """build 6720のbundled entryの形を最小限で再現したfixture。"""
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
        "include_apps_usage_instructions": True,
        "node_repl_auto_review_required": False,
        "node_repl_disabled": False,
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

    def test_known_fields_cover_the_current_template(self):
        self.assertEqual(catalog.unknown_template_fields(NATIVES), [])

    def test_reports_fields_the_stock_build_added(self):
        # 純正appがフィールドを増やすとcloneが黙って継ぐ。継承そのものは止めず、
        # 気づけるようにする。
        grown = native("gpt-grown") | {"brand_new_capability": True}
        self.assertEqual(
            catalog.unknown_template_fields([grown]), ["brand_new_capability"]
        )


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

    def test_neutralised_values_keep_the_native_type(self):
        """boolフィールドをNoneで潰さないこと。

        codexのcatalog deserializerは型を要求し、boolをnullにすると
        `invalid type: null, expected a boolean` でcatalog全体を拒否する。
        1件でも型を外すとpickerからORモデルが丸ごと消える。
        """
        for slug in REGISTRY:
            value = self.by_slug[slug]["include_apps_usage_instructions"]
            self.assertIs(value, False)
        self.assertIs(
            self.by_slug["gpt-first"]["include_apps_usage_instructions"], True
        )

    def test_does_not_disable_the_local_js_repl(self):
        """否定形フィールドを「無効化」側へ倒さないこと。

        `node_repl_disabled` の中和は、native側が将来この値を反転させても
        cloneが追随しないよう固定するのが目的で、無効化が目的ではない。
        `True` にするとテンプレートの `tool_mode: code_mode_only` と噛み合って
        ORモデルからツールを丸ごと奪いかねない。
        """
        for slug in REGISTRY:
            self.assertIs(self.by_slug[slug]["node_repl_disabled"], False)
            self.assertIs(self.by_slug[slug]["node_repl_auto_review_required"], False)

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

    def test_rejects_unneutralised_native_only_field(self):
        slug = next(iter(REGISTRY))
        for model in self.document["models"]:
            if model["slug"] == slug:
                model["include_apps_usage_instructions"] = True
        self._expect_error(self.document)

    def test_rejects_native_only_field_neutralised_to_the_wrong_type(self):
        # `0 == False` なので、値だけを見るとbool中和漏れを見逃す。
        slug = next(iter(REGISTRY))
        for model in self.document["models"]:
            if model["slug"] == slug:
                model["include_apps_usage_instructions"] = 0
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


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        import tempfile

        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "state" / "clone-template.json"
        self.previous = catalog.previous_path(self.path)

    def test_records_the_template_of_the_build_it_ran_on(self):
        catalog.snapshot_template(NATIVES, self.path, version="26.1", build="6720")
        saved = catalog.read_snapshot(self.path)
        self.assertEqual(saved["build"], "6720")
        self.assertEqual(saved["version"], "26.1")
        self.assertEqual(saved["template"]["slug"], "gpt-first")
        self.assertFalse(self.previous.exists())

    def test_same_build_does_not_rotate(self):
        # profile変更でcatalogを組み直すたびにrotateすると、`.previous` が同じbuildで
        # 埋まって比較対象の旧buildが消える。
        catalog.snapshot_template(NATIVES, self.path, version="26.1", build="6720")
        catalog.snapshot_template(NATIVES, self.path, version="26.1", build="6720")
        self.assertFalse(self.previous.exists())

    def test_new_build_rotates_the_old_one(self):
        catalog.snapshot_template(NATIVES, self.path, version="26.1", build="6662")
        grown = [n | {"brand_new_capability": True} for n in NATIVES]
        catalog.snapshot_template(grown, self.path, version="26.2", build="6720")
        self.assertEqual(catalog.read_snapshot(self.previous)["build"], "6662")
        self.assertEqual(catalog.read_snapshot(self.path)["build"], "6720")

    def test_reports_field_names_that_moved(self):
        catalog.snapshot_template(NATIVES, self.path, version="26.1", build="6662")
        snapshot = catalog.read_snapshot(self.path)
        changed = [
            n | {"brand_new_capability": True, "context_window": 1}
            for n in NATIVES
        ]
        for entry in changed:
            entry.pop("shell_type")
        self.assertEqual(
            catalog.template_field_drift(snapshot, changed),
            {
                "added": ["brand_new_capability"],
                "removed": ["shell_type"],
                "changed": ["context_window"],
            },
        )

    def test_identical_template_reports_no_drift(self):
        catalog.snapshot_template(NATIVES, self.path, version="26.1", build="6662")
        drift = catalog.template_field_drift(catalog.read_snapshot(self.path), NATIVES)
        self.assertEqual(drift, {"added": [], "removed": [], "changed": []})

    def test_generate_records_the_snapshot_after_the_catalog_lands(self):
        from unittest import mock

        output = Path(self.directory.name) / "catalogs" / "composite.json"
        with mock.patch.object(catalog, "bundled_models", return_value=NATIVES), \
             mock.patch.object(catalog.pricing, "resolve", return_value=None):
            catalog.generate(
                Path("/nonexistent/codex"),
                Path(self.directory.name),
                ROOT / "models/registry.json",
                output,
                snapshot=self.path,
                build_id=("26.1", "6720"),
            )
        self.assertTrue(output.is_file())
        self.assertEqual(catalog.read_snapshot(self.path)["build"], "6720")

    def test_generate_without_a_snapshot_path_writes_nothing(self):
        from unittest import mock

        output = Path(self.directory.name) / "catalogs" / "composite.json"
        with mock.patch.object(catalog, "bundled_models", return_value=NATIVES), \
             mock.patch.object(catalog.pricing, "resolve", return_value=None):
            catalog.generate(
                Path("/nonexistent/codex"),
                Path(self.directory.name),
                ROOT / "models/registry.json",
                output,
            )
        self.assertFalse(self.path.exists())

    def test_unreadable_snapshot_is_not_an_error(self):
        # snapshotは診断の補助でしかない。読めないことでdoctorや生成を落とさない。
        self.assertIsNone(catalog.read_snapshot(self.path))
        self.path.parent.mkdir(parents=True)
        self.path.write_text("{ not json", encoding="utf-8")
        self.assertIsNone(catalog.read_snapshot(self.path))
        self.path.write_text('{"build": "6720"}', encoding="utf-8")
        self.assertIsNone(catalog.read_snapshot(self.path))


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

    def test_installed_template_has_no_unknown_fields(self):
        """実機buildのテンプレートが既知集合を超えていないこと。

        超えていたら、cloneがその新フィールドをOpenRouter entryへ黙って
        引き継いでいる。`catalog.NATIVE_ONLY_FIELDS` に足すか、既知集合へ
        足して「継がせてよい」と明示するかを判断してから緑に戻すこと。
        """
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            natives = catalog.bundled_models(self.CODEX, Path(directory))
        self.assertEqual(catalog.unknown_template_fields(natives), [])

    def test_installed_codex_accepts_the_generated_catalog(self):
        """組み上げたcompositeを実機codexが読めること。

        `validate` は自前の契約しか見ない。中和値の型を外すと codex 側が
        catalog 全体を拒否してpickerからORモデルが消えるが、それはここでしか
        捕まらない。
        """
        import subprocess
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            home.mkdir()
            natives = catalog.bundled_models(self.CODEX, home)
            composite = Path(directory) / "composite.json"
            composite.write_text(
                json.dumps(catalog.build(natives, REGISTRY)), encoding="utf-8"
            )
            result = subprocess.run(
                [
                    str(self.CODEX),
                    "debug",
                    "models",
                    "-c",
                    f"model_catalog_json={composite}",
                ],
                env={"CODEX_HOME": str(home), "PATH": "/usr/bin:/bin"},
                text=True,
                capture_output=True,
                timeout=60,
            )
        self.assertEqual(result.returncode, 0, result.stderr.strip()[:400])
        loaded = {m["slug"] for m in json.loads(result.stdout)["models"]}
        self.assertTrue(set(REGISTRY) <= loaded)


if __name__ == "__main__":
    unittest.main()
