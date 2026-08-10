from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def load_refresh(home: Path):
    (home / "model-catalogs").mkdir(parents=True)
    registry = json.loads((ROOT / "models/registry.json").read_text(encoding="utf-8"))
    profile = {
        "schema_version": 1,
        "name": "subset",
        "models": ["minimax/minimax-m3"],
        "default_model": "minimax/minimax-m3",
        "default_effort": None,
    }
    (home / "registry.json").write_text(json.dumps(registry), encoding="utf-8")
    (home / "profile.json").write_text(json.dumps(profile), encoding="utf-8")
    source = (ROOT / "portable/templates/codex-openrouter-refresh.py.in").read_text(encoding="utf-8")
    rendered = source.replace("@@USER_HOME@@", str(home.parent)).replace("@@PYTHON@@", "/usr/bin/python3")
    module_path = home / "refresh.py"
    module_path.write_text(rendered, encoding="utf-8")
    spec = importlib.util.spec_from_file_location("tested_refresh", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    with mock.patch.dict(os.environ, {"CODEX_OPENROUTER_HOME": str(home)}, clear=False):
        spec.loader.exec_module(module)
    return module


class RefreshTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary.name) / "home"
        self.home.mkdir()
        self.module = load_refresh(self.home)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_runtime_contract_selects_only_profile_models(self) -> None:
        self.assertEqual(["minimax/minimax-m3"], list(self.module.MODEL_SPECS))
        prices = self.module.fallback_price_metadata()
        self.assertEqual({"minimax/minimax-m3"}, set(prices["headline"]))

    def test_zdr_prices_require_active_endpoint_for_each_component(self) -> None:
        payload = {
            "data": [
                {
                    "model_id": "minimax/minimax-m3",
                    "status": 0,
                    "provider_name": "Provider A",
                    "pricing": {
                        "prompt": "0.0000003",
                        "completion": "0.0000012",
                        "input_cache_read": "0.00000006",
                    },
                }
            ]
        }
        parsed = self.module.parse_zdr_metadata(payload)
        self.assertEqual("Provider A", parsed["minimax/minimax-m3"]["input"][1])
        payload["data"][0]["status"] = 1
        with self.assertRaises(self.module.RefreshUnavailableError):
            self.module.parse_zdr_metadata(payload)

    def test_refresh_ttl_and_failure_backoff(self) -> None:
        state = self.home / "state.json"
        self.module.write_refresh_state(state, {}, 1000, "success", success=True)
        self.assertEqual((False, "last successful refresh is less than 24 hours old"), self.module.refresh_decision(state, 1001))
        self.module.write_refresh_state(state, {}, 1000, "network-error")
        self.assertEqual((False, "network retry backoff is active"), self.module.refresh_decision(state, 1001))
        self.assertTrue(self.module.refresh_decision(state, 5000)[0])


if __name__ == "__main__":
    unittest.main()
