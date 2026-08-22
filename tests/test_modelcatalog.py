"""ライブcatalogからregistryエントリを導出する規則の固定。

fixtureはOpenRouterの実responseを切り出したもの（同梱5件 + 導出の境界事例）。
規則が壊れたら、同梱registryを再現できなくなることで落ちる。
"""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from codex_openrouter import modelcatalog, pricing
from tests_support import make_paths


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
REGISTRY = json.loads((ROOT / "models/registry.json").read_text(encoding="utf-8"))


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"openrouter-{name}.json").read_text(encoding="utf-8"))


def build(**overrides) -> dict:
    arguments = {
        "models_document": _fixture("models"),
        "zdr_document": _fixture("zdr"),
        "providers_document": _fixture("providers"),
    }
    arguments.update(overrides)
    return modelcatalog.build(REGISTRY, **arguments)


class DerivationTests(unittest.TestCase):
    """導出が同梱registryを再現することを固定する。ここが本体。"""

    def setUp(self) -> None:
        self.rows = {row["id"]: row for row in build()["models"]}

    def test_derivation_reproduces_every_bundled_registry_entry(self) -> None:
        for slug, curated in REGISTRY["models"].items():
            with self.subTest(model=slug):
                self.assertIn(slug, self.rows, "同梱modelが候補から落ちています")
                self.assertEqual(modelcatalog.entry_for(self.rows[slug], curated), curated)

    def test_curated_prose_is_kept_but_everything_else_comes_from_the_api(self) -> None:
        slug = "deepseek/deepseek-v4-pro"
        derived = modelcatalog.entry_for(self.rows[slug])
        curated = REGISTRY["models"][slug]

        # 日本語の説明文だけはAPIから作れないので、同梱値が残る。
        self.assertNotEqual(derived["capability"], curated["capability"])
        self.assertEqual(
            modelcatalog.entry_for(self.rows[slug], curated)["capability"],
            curated["capability"],
        )
        # それ以外はcuratedを渡さなくても一致する。
        for key in ("efforts", "default_effort", "canonical_slug", "context_window",
                    "supports_parallel_tool_calls", "codex_modalities"):
            self.assertEqual(derived[key], curated[key], key)

    def test_efforts_are_sorted_ascending_regardless_of_api_order(self) -> None:
        self.assertEqual(
            modelcatalog.ordered_efforts(["max", "low", "high"]), ["low", "high", "max"]
        )
        self.assertEqual(modelcatalog.ordered_efforts(["xhigh", "high"]), ["high", "xhigh"])
        # 未知の語は捨てずに末尾へ。新しいeffortが増えても候補ごと消さない。
        self.assertEqual(
            modelcatalog.ordered_efforts(["ultra", "low"]), ["low", "ultra"]
        )
        self.assertEqual(modelcatalog.ordered_efforts(None), [])

    def test_vendor_prefix_is_stripped_from_display_name(self) -> None:
        self.assertEqual(modelcatalog.display_name("DeepSeek: DeepSeek V4 Pro"), "DeepSeek V4 Pro")
        self.assertEqual(modelcatalog.display_name("Claude Opus 5"), "Claude Opus 5")


class FilterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.document = build()
        self.rows = {row["id"]: row for row in self.document["models"]}

    def test_models_without_tool_calling_remain_visible_as_unsupported(self) -> None:
        document = build(
            models_document={
                "data": [
                    {
                        "id": "vendor/chat-only",
                        "name": "Vendor: Chat Only",
                        "architecture": {
                            "input_modalities": ["text"],
                            "output_modalities": ["text"],
                        },
                        "pricing": {"prompt": "0.000001", "completion": "0.000002"},
                        "supported_parameters": ["max_tokens", "temperature"],
                    }
                ]
            },
            zdr_document={"data": []},
        )
        self.assertEqual(len(document["models"]), 1)
        row = document["models"][0]
        self.assertEqual(row["tool_support"], "unsupported")
        self.assertIn("tools", row["tool_support_reason"])

    def test_unreadable_tool_metadata_remains_visible_as_unknown(self) -> None:
        model = json.loads(json.dumps(_fixture("models")["data"][0]))
        model["id"] = "vendor/unknown-tools"
        model["supported_parameters"] = {"tools": True}
        document = build(models_document={"data": [model]})
        self.assertEqual(document["models"][0]["tool_support"], "unknown")

    def test_zdr_capability_does_not_depend_on_being_able_to_price_it(self) -> None:
        """cache価格を出さないZDR providerでも、ZDRで動けることは変わらない。

        ここを混ぜると、実際にはZDRで動くmodelのZDR強制がguardで外れる。
        """
        row = self.rows["bytedance-seed/seed-2-1-turbo"]
        self.assertTrue(row["zdr_supported"])
        self.assertIsNotNone(row["zdr"])
        self.assertNotIn("cache_read", row["zdr"])
        self.assertEqual(modelcatalog.entry_for(row)["zdr_supported"], True)

    def test_free_models_are_flagged_and_have_no_zdr_endpoint(self) -> None:
        row = self.rows["liquid/lfm-2.5-2.6b:free"]
        self.assertTrue(row["free"])
        # 実測では free と ZDR は重ならない。重なるようになったら気づきたい。
        self.assertFalse(row["zdr_supported"])

    def test_training_policy_is_unknown_rather_than_guessed_for_non_zdr_models(self) -> None:
        self.assertIsNone(self.rows["liquid/lfm-2.5-2.6b:free"]["trains_on_data"])
        self.assertIs(self.rows["deepseek/deepseek-v4-pro"]["trains_on_data"], False)

    def test_provider_outage_leaves_the_catalog_usable_without_the_training_badge(self) -> None:
        document = build(providers_document={"data": "broken"})
        self.assertFalse(document["training_policy_available"])
        self.assertTrue(document["models"])
        self.assertTrue(all(row["trains_on_data"] is None for row in document["models"]))


class UsageTests(unittest.TestCase):
    def rankings(self) -> dict:
        return {
            "data": [
                {"date": "2026-08-12", "model_permaslug": "deepseek/deepseek-v4-pro-20260423",
                 "total_tokens": "100"},
                {"date": "2026-08-06", "model_permaslug": "deepseek/deepseek-v4-pro-20260423",
                 "total_tokens": "20"},
                {"date": "2026-07-20", "model_permaslug": "deepseek/deepseek-v4-pro-20260423",
                 "total_tokens": "3"},
                {"date": "2026-08-12", "model_permaslug": "other", "total_tokens": "999"},
            ]
        }

    def test_windows_accumulate_by_canonical_slug(self) -> None:
        document = build(rankings_document=self.rankings())
        rows = {row["id"]: row for row in document["models"]}
        self.assertEqual(
            rows["deepseek/deepseek-v4-pro"]["usage_tokens"],
            {"1d": "100", "7d": "120", "30d": "123"},
        )
        # 集計行 `other` は特定modelの利用量ではないので混ぜない。
        self.assertTrue(document["usage_available"])
        self.assertEqual(document["usage_matched"], 1)

    def test_models_outside_the_top_50_simply_have_no_usage(self) -> None:
        document = build(rankings_document=self.rankings())
        rows = {row["id"]: row for row in document["models"]}
        self.assertIsNone(rows["moonshotai/kimi-k3"]["usage_tokens"])

    def test_broken_rankings_join_is_visible_instead_of_silently_empty(self) -> None:
        document = build(
            rankings_document={"data": [
                {"date": "2026-08-12", "model_permaslug": "nope/nope-1", "total_tokens": "5"}
            ]},
        )
        self.assertEqual(document["usage_matched"], 0)

    def test_rankings_failure_does_not_remove_the_candidates(self) -> None:
        document = build(rankings_document={"broken": True})
        self.assertFalse(document["usage_available"])
        self.assertTrue(document["models"])


class CacheTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.paths = make_paths(Path(directory.name))
        self.paths.state_dir.mkdir(parents=True)

    def _fetch(self, **overrides):
        return mock.patch.object(
            modelcatalog, "fetch", side_effect=lambda *a, **k: build(**overrides)
        )

    def test_second_call_within_the_ttl_uses_the_cache(self) -> None:
        with self._fetch() as fetch:
            first = modelcatalog.load(self.paths, REGISTRY)
            second = modelcatalog.load(self.paths, REGISTRY)
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(first["source"], "live")
        self.assertEqual(second["source"], "cache")
        self.assertEqual(len(second["models"]), len(first["models"]))

    def test_refresh_ignores_the_ttl(self) -> None:
        with self._fetch() as fetch:
            modelcatalog.load(self.paths, REGISTRY)
            modelcatalog.load(self.paths, REGISTRY, refresh=True)
        self.assertEqual(fetch.call_count, 2)

    def test_offline_falls_back_to_the_cache_rather_than_failing(self) -> None:
        with self._fetch():
            modelcatalog.load(self.paths, REGISTRY)
        with mock.patch.object(
            modelcatalog, "fetch", side_effect=pricing.PricingUnavailableError("offline")
        ):
            document = modelcatalog.load(self.paths, REGISTRY, refresh=True)
        self.assertEqual(document["source"], "cache")
        self.assertTrue(document["models"])

    def test_offline_without_a_cache_reports_the_failure(self) -> None:
        with mock.patch.object(
            modelcatalog, "fetch", side_effect=pricing.PricingUnavailableError("offline")
        ), self.assertRaises(modelcatalog.CatalogError):
            modelcatalog.load(self.paths, REGISTRY)

    def test_cache_is_written_private(self) -> None:
        with self._fetch():
            modelcatalog.load(self.paths, REGISTRY)
        self.assertEqual(self.paths.catalog_cache.stat().st_mode & 0o777, 0o600)

    def test_cache_from_an_older_schema_is_ignored(self) -> None:
        self.paths.catalog_cache.write_text(
            json.dumps({"schema_version": 0, "models": []}), encoding="utf-8"
        )
        self.assertIsNone(modelcatalog.read_cache(self.paths.catalog_cache))

    def test_multi_router_cache_is_ignored_after_orcarouter_removal(self) -> None:
        self.paths.catalog_cache.write_text(
            json.dumps({
                "schema_version": 1,
                "models": [{"id": "orca/vendor/model", "router": "orcarouter"}],
            }),
            encoding="utf-8",
        )
        self.assertIsNone(modelcatalog.read_cache(self.paths.catalog_cache))

    def test_cache_never_carries_a_secret(self) -> None:
        with self._fetch():
            modelcatalog.load(self.paths, REGISTRY, key="sk-or-v1-" + "f" * 64)
        self.assertNotIn("sk-or-", self.paths.catalog_cache.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
