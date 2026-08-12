from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import tomllib
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from codex_openrouter import configblock  # noqa: E402

# 純正appが実際に書く形に寄せたfixture。top-level keyのあとにtableが続く。
LIVE_CONFIG = """model = "gpt-5.6-sol"
model_reasoning_effort = "xhigh"
personality = "pragmatic"

[shell_environment_policy]
inherit = "core"

[projects."/workspace/example"]
trust_level = "trusted"
"""


class TopLevelTests(unittest.TestCase):
    def test_reads_existing_key(self):
        self.assertEqual(configblock.read_top_level(LIVE_CONFIG, "model"), "gpt-5.6-sol")

    def test_ignores_keys_inside_tables(self):
        self.assertIsNone(configblock.read_top_level(LIVE_CONFIG, "trust_level"))
        self.assertIsNone(configblock.read_top_level(LIVE_CONFIG, "inherit"))

    def test_upsert_replaces_in_place_without_touching_tables(self):
        updated = configblock.upsert_top_level(LIVE_CONFIG, "model", "deepseek/deepseek-v4-pro")
        self.assertEqual(
            configblock.read_top_level(updated, "model"), "deepseek/deepseek-v4-pro"
        )
        self.assertIn('[projects."/workspace/example"]', updated)
        self.assertIn('personality = "pragmatic"', updated)
        self.assertEqual(updated.count("model ="), 1)

    def test_upsert_inserts_before_first_table(self):
        updated = configblock.upsert_top_level(LIVE_CONFIG, "model_provider", "openrouter")
        self.assertEqual(configblock.read_top_level(updated, "model_provider"), "openrouter")
        self.assertLess(
            updated.index("model_provider"), updated.index("[shell_environment_policy]")
        )

    def test_upsert_is_idempotent(self):
        once = configblock.upsert_top_level(LIVE_CONFIG, "model_provider", "openrouter")
        twice = configblock.upsert_top_level(once, "model_provider", "openrouter")
        self.assertEqual(once, twice)

    def test_remove_top_level_restores_original(self):
        updated = configblock.upsert_top_level(LIVE_CONFIG, "model_provider", "openrouter")
        self.assertEqual(configblock.remove_top_level(updated, "model_provider"), LIVE_CONFIG)


class BlockTests(unittest.TestCase):
    def test_catalog_block_lands_before_first_table(self):
        body = 'model_catalog_json = "/tmp/x.json"'
        updated = configblock.insert_block(LIVE_CONFIG, "catalog", body, top_level=True)
        self.assertTrue(configblock.has_block(updated, "catalog"))
        self.assertLess(
            updated.index("model_catalog_json"), updated.index("[shell_environment_policy]")
        )

    def test_provider_block_lands_at_end(self):
        body = '[model_providers.openrouter]\nname = "OpenRouter"'
        updated = configblock.insert_block(LIVE_CONFIG, "provider", body, top_level=False)
        self.assertGreater(
            updated.index("[model_providers.openrouter]"),
            updated.index('[projects."/workspace/example"]'),
        )

    def test_insert_is_idempotent(self):
        body = 'model_catalog_json = "/tmp/x.json"'
        once = configblock.insert_block(LIVE_CONFIG, "catalog", body, top_level=True)
        twice = configblock.insert_block(once, "catalog", body, top_level=True)
        self.assertEqual(once, twice)

    def test_insert_then_remove_round_trips(self):
        for name, body, top_level in (
            ("catalog", 'model_catalog_json = "/tmp/x.json"', True),
            ("provider", '[model_providers.openrouter]\nname = "OpenRouter"', False),
        ):
            with self.subTest(name=name):
                updated = configblock.insert_block(LIVE_CONFIG, name, body, top_level=top_level)
                self.assertEqual(configblock.remove_block(updated, name), LIVE_CONFIG)

    def test_remove_missing_block_is_noop(self):
        self.assertEqual(configblock.remove_block(LIVE_CONFIG, "catalog"), LIVE_CONFIG)

    def test_removal_never_touches_surrounding_blank_lines(self):
        # appが書いた空行の並びを、block除去のついでに正規化してはいけない。
        spaced = 'model = "x"\n\n\n\n[a]\nk = 1\n\n\n[b]\nk = 2\n'
        for name, body, top_level in (
            ("catalog", 'model_catalog_json = "/tmp/x.json"', True),
            ("provider", '[model_providers.openrouter]\nname = "OpenRouter"', False),
        ):
            with self.subTest(name=name):
                updated = configblock.insert_block(spaced, name, body, top_level=top_level)
                self.assertEqual(configblock.remove_block(updated, name), spaced)

    def test_blocks_coexist_and_remove_independently(self):
        text = configblock.insert_block(
            LIVE_CONFIG, "catalog", 'model_catalog_json = "/tmp/x.json"', top_level=True
        )
        text = configblock.insert_block(
            text, "provider", '[model_providers.openrouter]\nname = "OpenRouter"', top_level=False
        )
        # 案Dの寿命: catalogだけ外し、providerは永続で残す。
        text = configblock.remove_block(text, "catalog")
        self.assertFalse(configblock.has_block(text, "catalog"))
        self.assertTrue(configblock.has_block(text, "provider"))
        self.assertIn("[model_providers.openrouter]", text)

    def test_updating_block_body_replaces_contents(self):
        text = configblock.insert_block(
            LIVE_CONFIG, "catalog", 'model_catalog_json = "/old.json"', top_level=True
        )
        text = configblock.insert_block(
            text, "catalog", 'model_catalog_json = "/new.json"', top_level=True
        )
        self.assertIn("/new.json", text)
        self.assertNotIn("/old.json", text)


class ManagedConfigTests(unittest.TestCase):
    PROVIDER = """[model_providers.openrouter]
name = "OpenRouter"
base_url = "http://127.0.0.1:0/v1"
wire_api = "responses"
"""

    def test_active_blocks_are_rebuilt_without_nesting(self):
        provider_only = configblock.insert_block(
            LIVE_CONFIG, "provider", self.PROVIDER, top_level=False
        )
        active = configblock.render_managed(
            provider_only,
            provider_body=self.PROVIDER.replace(":0/", ":49152/"),
            catalog_body='model_catalog_json = "/tmp/catalog.json"',
        )
        parsed = tomllib.loads(active)
        self.assertEqual(parsed["model_catalog_json"], "/tmp/catalog.json")
        self.assertEqual(
            parsed["model_providers"]["openrouter"]["base_url"],
            "http://127.0.0.1:49152/v1",
        )
        self.assertLess(active.index("model_catalog_json"), active.index("[shell_environment_policy]"))
        self.assertLess(active.index("model_catalog_json"), active.index("[model_providers.openrouter]"))

    def test_inactive_removes_catalog_and_keeps_stub(self):
        active = configblock.render_managed(
            LIVE_CONFIG,
            provider_body=self.PROVIDER.replace(":0/", ":49152/"),
            catalog_body='model_catalog_json = "/tmp/catalog.json"',
        )
        inactive = configblock.render_managed(active, provider_body=self.PROVIDER)
        parsed = tomllib.loads(inactive)
        self.assertNotIn("model_catalog_json", parsed)
        self.assertEqual(
            parsed["model_providers"]["openrouter"]["base_url"],
            "http://127.0.0.1:0/v1",
        )

    def test_unmarked_provider_conflict_is_rejected(self):
        conflicting = LIVE_CONFIG + "\n[model_providers.openrouter]\nname = \"mine\"\n"
        with self.assertRaises(configblock.ConfigBlockError):
            configblock.render_managed(conflicting, provider_body=self.PROVIDER)

    def test_unmarked_catalog_conflict_is_rejected(self):
        conflicting = 'model_catalog_json = "/mine.json"\n' + LIVE_CONFIG
        with self.assertRaises(configblock.ConfigBlockError):
            configblock.render_managed(conflicting, provider_body=self.PROVIDER)

    def test_duplicate_or_broken_marker_is_rejected(self):
        broken = LIVE_CONFIG + "\n# >>> codex-openrouter:provider >>>\n"
        with self.assertRaises(configblock.ConfigBlockError):
            configblock.render_managed(broken, provider_body=self.PROVIDER)

    def test_invalid_toml_is_rejected(self):
        with self.assertRaises(configblock.ConfigBlockError):
            configblock.render_managed("model = [\n", provider_body=self.PROVIDER)


class EditTests(unittest.TestCase):
    def test_edit_writes_and_preserves_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(LIVE_CONFIG, encoding="utf-8")
            path.chmod(0o600)
            changed = configblock.edit(
                path, lambda text: configblock.upsert_top_level(text, "model_provider", "openai")
            )
            self.assertTrue(changed)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(
                configblock.read_top_level(path.read_text(encoding="utf-8"), "model_provider"),
                "openai",
            )

    def test_edit_reports_no_change(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(LIVE_CONFIG, encoding="utf-8")
            self.assertFalse(configblock.edit(path, lambda text: text))

    def test_edit_never_reverts_a_concurrent_write(self):
        """mutate中にappがmodelを書いても、その変更を巻き戻してはいけない。

        巻き戻った状態は (model=native, provider=openai) のようにそれ自体は
        整合するので以後のtickでは検知できず、利用者の選択が永久に失われる。
        """
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(LIVE_CONFIG, encoding="utf-8")
            state = {"n": 0}

            def mutate(text: str) -> str:
                state["n"] += 1
                if state["n"] == 1:
                    # 判定用のmutate中に、appがpicker選択を書いた状況を作る。
                    configblock.atomic_write(
                        path, configblock.upsert_top_level(text, "model", "z-ai/glm-5.2")
                    )
                return configblock.upsert_top_level(text, "model_provider", "openrouter")

            configblock.edit(path, mutate)
            final = path.read_text(encoding="utf-8")
            self.assertEqual(configblock.read_top_level(final, "model"), "z-ai/glm-5.2")
            self.assertEqual(
                configblock.read_top_level(final, "model_provider"), "openrouter"
            )

    def test_edit_retries_when_file_changes_under_it(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(LIVE_CONFIG, encoding="utf-8")
            state = {"n": 0}

            def mutate(text: str) -> str:
                # 最初の1回だけ、読み取り後にappが書いた状況を作る。
                state["n"] += 1
                if state["n"] == 1:
                    path.write_text(LIVE_CONFIG + '\n[extra]\nk = "v"\n', encoding="utf-8")
                return configblock.upsert_top_level(text, "model_provider", "openrouter")

            self.assertTrue(configblock.edit(path, mutate))
            final = path.read_text(encoding="utf-8")
            # appの書き込みが残っていること（潰していない）。
            self.assertIn("[extra]", final)
            self.assertEqual(configblock.read_top_level(final, "model_provider"), "openrouter")


if __name__ == "__main__":
    unittest.main()
