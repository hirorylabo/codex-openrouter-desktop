from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from codex_openrouter import configblock, watcher as watcher_module  # noqa: E402

REGISTRY = json.loads((ROOT / "models/registry.json").read_text(encoding="utf-8"))["models"]

BASE_CONFIG = """model = "gpt-5.6-sol"
model_reasoning_effort = "xhigh"

[shell_environment_policy]
inherit = "core"
"""


class WatcherTestCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.config = Path(self.directory.name) / "config.toml"
        self.config.write_text(BASE_CONFIG, encoding="utf-8")
        self.watcher = watcher_module.Watcher(self.config, REGISTRY, poll_seconds=0.01)

    def provider(self) -> str | None:
        return configblock.read_top_level(
            self.config.read_text(encoding="utf-8"), "model_provider"
        )

    def select(self, model: str) -> None:
        """appがpicker選択をconfigへ書く動きを模す。"""
        text = configblock.upsert_top_level(
            self.config.read_text(encoding="utf-8"), "model", model
        )
        configblock.atomic_write(self.config, text)


class MappingTests(WatcherTestCase):
    def test_openrouter_models_map_to_openrouter(self):
        for slug in REGISTRY:
            self.assertEqual(
                self.watcher.desired_provider(slug), watcher_module.OPENROUTER_PROVIDER
            )

    def test_native_models_map_to_openai(self):
        for slug in ("gpt-5.6-sol", "gpt-5.6-luna", "gpt-5.5", "codex-auto-review"):
            self.assertEqual(self.watcher.desired_provider(slug), watcher_module.NATIVE_PROVIDER)

    def test_unknown_and_missing_map_to_openai(self):
        # 安全側。未知slugをopenrouterに倒すとnative本文が外へ出うる。
        self.assertEqual(self.watcher.desired_provider("who/knows"), "openai")
        self.assertEqual(self.watcher.desired_provider(None), "openai")


class SyncTests(WatcherTestCase):
    def test_sync_sets_openrouter_for_openrouter_model(self):
        self.select("deepseek/deepseek-v4-pro")
        self.assertTrue(self.watcher.sync_once())
        self.assertEqual(self.provider(), "openrouter")

    def test_sync_restores_openai_for_native_model(self):
        self.select("deepseek/deepseek-v4-pro")
        self.watcher.sync_once()
        self.select("gpt-5.6-sol")
        self.assertTrue(self.watcher.sync_once())
        self.assertEqual(self.provider(), "openai")

    def test_sync_is_idempotent(self):
        self.select("z-ai/glm-5.2")
        self.assertTrue(self.watcher.sync_once())
        self.assertFalse(self.watcher.sync_once())

    def test_sync_preserves_other_config(self):
        self.select("z-ai/glm-5.2")
        self.watcher.sync_once()
        text = self.config.read_text(encoding="utf-8")
        self.assertIn("[shell_environment_policy]", text)
        self.assertIn('model_reasoning_effort = "xhigh"', text)

    def test_missing_config_does_not_raise(self):
        self.config.unlink()
        self.assertFalse(self.watcher.sync_once())


class LoopTests(WatcherTestCase):
    def test_background_loop_follows_selection(self):
        thread, stop = self.watcher.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(stop.set)
        # 実運用ではwatcherはapp起動より前から回っている。最初のtickを終えてから
        # 選択を始めることで、その状態を再現する。
        deadline = time.time() + 2
        while time.time() < deadline and self.provider() is None:
            time.sleep(0.01)

        self.select("moonshotai/kimi-k3")
        deadline = time.time() + 5
        while time.time() < deadline and self.provider() != "openrouter":
            time.sleep(0.02)
        self.assertEqual(self.provider(), "openrouter")

        self.select("gpt-5.6-terra")
        deadline = time.time() + 5
        while time.time() < deadline and self.provider() != "openai":
            time.sleep(0.02)
        self.assertEqual(self.provider(), "openai")

    def test_loop_stops_on_event(self):
        thread, stop = self.watcher.start()
        stop.set()
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())


class LostUpdateTests(WatcherTestCase):
    def test_selection_survives_under_realistic_timing(self):
        """watcherが定常状態のとき、appのmodel書き込みを巻き戻さないこと。

        実運用の条件を模す: watcherは先に回っており、利用者のクリック起点の
        書き込みはpoll周期と非同期に来る。
        """
        import random

        for _ in range(15):
            self.config.write_text(BASE_CONFIG, encoding="utf-8")
            thread, stop = self.watcher.start()
            try:
                deadline = time.time() + 2
                while time.time() < deadline and self.provider() is None:
                    time.sleep(0.005)
                time.sleep(random.uniform(0, 0.05))
                self.select("moonshotai/kimi-k3")
                deadline = time.time() + 2
                while time.time() < deadline and self.provider() != "openrouter":
                    time.sleep(0.005)
            finally:
                stop.set()
                thread.join(2)
            text = self.config.read_text(encoding="utf-8")
            self.assertEqual(configblock.read_top_level(text, "model"), "moonshotai/kimi-k3")
            self.assertEqual(configblock.read_top_level(text, "model_provider"), "openrouter")


class ConcurrencyTests(WatcherTestCase):
    def test_watcher_does_not_clobber_concurrent_app_writes(self):
        """appがconfigへ書き続けている間もwatcherが追随し、appの内容を消さない。"""
        self.select("deepseek/deepseek-v4-flash-0731")
        stop = threading.Event()

        def app_writes():
            index = 0
            while not stop.is_set() and index < 20:
                # appもlockを取る想定にする（自前writer同士の直列化の検証）。
                configblock.edit(
                    self.config,
                    lambda text, i=index: text
                    if f"[plugins.p{i}]" in text
                    else text + f'\n[plugins.p{i}]\nenabled = true\n',
                )
                index += 1
                time.sleep(0.005)

        writer = threading.Thread(target=app_writes, daemon=True)
        writer.start()
        # writerが20件書き終えるまで、watcherを回し続ける。
        deadline = time.time() + 10
        while writer.is_alive() and time.time() < deadline:
            self.watcher.sync_once()
            time.sleep(0.005)
        stop.set()
        writer.join(timeout=3)
        self.watcher.sync_once()

        text = self.config.read_text(encoding="utf-8")
        self.assertEqual(configblock.read_top_level(text, "model_provider"), "openrouter")
        self.assertIn("[plugins.p0]", text)
        # appの書き込みが1件も失われていないこと。
        self.assertIn("[plugins.p19]", text)
        # watcherがmodelを巻き戻していないこと。
        self.assertEqual(
            configblock.read_top_level(text, "model"), "deepseek/deepseek-v4-flash-0731"
        )


if __name__ == "__main__":
    unittest.main()
