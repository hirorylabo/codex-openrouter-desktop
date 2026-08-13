from __future__ import annotations

from decimal import Decimal
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from codex_openrouter import pricing  # noqa: E402

REGISTRY = json.loads((ROOT / "models/registry.json").read_text(encoding="utf-8"))
SLUGS = list(REGISTRY["models"])


def models_payload() -> dict:
    # live価格はper-token。per-Mへ換算されることを確かめたいので小さい値を使う。
    return {
        "data": [
            {"id": slug, "pricing": {"prompt": "0.000001", "completion": "0.000002",
                                     "input_cache_read": "0.0000005"}}
            for slug in SLUGS
        ]
    }


def zdr_payload() -> dict:
    data = []
    for slug in SLUGS:
        data.append({"model_id": slug, "status": 0, "provider_name": "Cheap",
                     "pricing": {"prompt": "0.0000008", "completion": "0.0000016",
                                 "input_cache_read": "0.0000004"}})
        data.append({"model_id": slug, "status": 0, "provider_name": "Pricey",
                     "pricing": {"prompt": "0.000009", "completion": "0.000009",
                                 "input_cache_read": "0.000009"}})
        data.append({"model_id": slug, "status": 1, "provider_name": "Down",
                     "pricing": {"prompt": "0.0000001", "completion": "0.0000001",
                                 "input_cache_read": "0.0000001"}})
    return {"data": data}


class ParseTests(unittest.TestCase):
    def test_headline_is_scaled_to_per_million(self):
        prices = pricing.parse_headline(models_payload(), REGISTRY)
        self.assertEqual(prices[SLUGS[0]]["input"], Decimal("1"))
        self.assertEqual(prices[SLUGS[0]]["output"], Decimal("2"))

    def test_headline_rejects_missing_model(self):
        payload = {"data": [{"id": "someone/else", "pricing": {"prompt": "0.1"}}]}
        with self.assertRaises(pricing.PricingUnavailableError):
            pricing.parse_headline(payload, REGISTRY)

    def test_zdr_picks_cheapest_active_endpoint(self):
        prices = pricing.parse_zdr(zdr_payload(), REGISTRY)
        price, provider = prices[SLUGS[0]]["input"]
        self.assertEqual(provider, "Cheap")
        self.assertEqual(price, Decimal("0.8"))

    def test_zdr_ignores_inactive_endpoints(self):
        # status!=0 の "Down" が最安だが選ばれてはいけない。
        prices = pricing.parse_zdr(zdr_payload(), REGISTRY)
        self.assertNotEqual(prices[SLUGS[0]]["input"][1], "Down")

    def test_zdr_requires_an_active_endpoint(self):
        payload = {"data": [{"model_id": SLUGS[0], "status": 1, "provider_name": "Down",
                             "pricing": {"prompt": "0.1"}}]}
        with self.assertRaises(pricing.PricingUnavailableError):
            pricing.parse_zdr(payload, REGISTRY)

    def test_price_out_of_range_is_rejected(self):
        with self.assertRaises(pricing.PricingError):
            pricing.validate_price("x", "999999")

    def test_provider_name_must_be_sane(self):
        for bad in ("", "   ", "x" * 65):
            with self.subTest(bad=bad), self.assertRaises(pricing.PricingError):
                pricing.validate_provider_name(bad)


class FallbackTests(unittest.TestCase):
    def test_registry_fallback_covers_every_model(self):
        prices = pricing.fallback_prices(REGISTRY)
        for slug in SLUGS:
            self.assertIn(slug, prices["headline"])
            self.assertIn(slug, prices["zdr"])
            self.assertIsInstance(prices["zdr"][slug]["input"][0], Decimal)
            self.assertTrue(prices["zdr"][slug]["input"][1])

    def test_fallback_is_not_scaled_again(self):
        # registryの値は既にper-M。二重にscaleすると桁が壊れる。
        prices = pricing.fallback_prices(REGISTRY)
        spec = REGISTRY["models"][SLUGS[0]]
        self.assertEqual(
            prices["headline"][SLUGS[0]]["input"], Decimal(spec["fallback_headline"]["input"])
        )

    def test_resolve_falls_back_when_network_fails(self):
        with mock.patch.object(pricing, "fetch_json",
                               side_effect=pricing.PricingUnavailableError("offline")):
            prices = pricing.resolve(REGISTRY, None)
        self.assertEqual(prices["source"], "fallback")


class StateTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.state = Path(self.directory.name) / "price-state.json"

    def test_first_run_fetches(self):
        self.assertTrue(pricing.should_fetch(REGISTRY["price_refresh"], self.state))

    def test_success_is_cached_for_the_ttl(self):
        pricing.write_refresh_state(self.state, "success", time.time())
        self.assertFalse(pricing.should_fetch(REGISTRY["price_refresh"], self.state))
        old = time.time() - REGISTRY["price_refresh"]["success_ttl_seconds"] - 1
        pricing.write_refresh_state(self.state, "success", old)
        self.assertTrue(pricing.should_fetch(REGISTRY["price_refresh"], self.state))

    def test_failure_uses_the_shorter_backoff(self):
        pricing.write_refresh_state(self.state, "failure", time.time())
        self.assertFalse(pricing.should_fetch(REGISTRY["price_refresh"], self.state))
        old = time.time() - REGISTRY["price_refresh"]["failure_backoff_seconds"] - 1
        pricing.write_refresh_state(self.state, "failure", old)
        self.assertTrue(pricing.should_fetch(REGISTRY["price_refresh"], self.state))

    def test_resolve_records_failure(self):
        with mock.patch.object(pricing, "fetch_json",
                               side_effect=pricing.PricingUnavailableError("offline")):
            pricing.resolve(REGISTRY, self.state)
        self.assertEqual(json.loads(self.state.read_text())["result"], "failure")


class DescribeTests(unittest.TestCase):
    def test_description_carries_headline_and_zdr(self):
        prices = pricing.fallback_prices(REGISTRY)
        slug = SLUGS[0]
        text = pricing.describe(slug, REGISTRY["models"][slug], prices, REGISTRY)
        self.assertIn("通常headline", text)
        self.assertIn("ZDR稼働endpoint最安", text)
        self.assertIn(slug, text)
        self.assertIn(REGISTRY["models"][slug]["canonical_slug"], text)
        self.assertIn("IN $", text)

    def test_trailing_zeros_are_trimmed(self):
        self.assertEqual(pricing.decimal_label(Decimal("1.500")), "1.5")
        self.assertEqual(pricing.decimal_label(Decimal("2.000")), "2")


if __name__ == "__main__":
    unittest.main()
