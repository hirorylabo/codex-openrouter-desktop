from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from codex_openrouter import toolbridge, toolcompat


MODEL = "vendor/codex-model"
KEY = "sk-or-v1-" + "a" * 64


def spec(*, zdr: bool = True, status: str = "declared") -> dict:
    return {
        "zdr_supported": zdr,
        "supported_parameters": ["tools"],
        "tool_support": status,
        "tool_support_reason": "fixture metadata",
    }


def success(body: dict, _key: str) -> tuple[int, dict]:
    tool = body["tools"][0]
    if tool["name"].startswith("codex_bridge_"):
        output = {
            "type": "function_call",
            "name": tool["name"],
            "arguments": '{"input":"PING"}',
        }
    else:
        output = {
            "type": "function_call",
            "name": tool["name"],
            "arguments": '{"value":"PING"}',
        }
    return 200, {"output": [output]}


class MetadataTests(unittest.TestCase):
    def test_metadata_maps_to_declared_unknown_and_unsupported(self) -> None:
        self.assertEqual(toolcompat.metadata_support(["tools"])[0], "declared")
        self.assertEqual(toolcompat.metadata_support(["temperature"])[0], "unsupported")
        self.assertEqual(toolcompat.metadata_support({"tools": True})[0], "unknown")


class VerificationTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "state" / "tool-compatibility.json"

    def verify(self, requester=success, **kwargs):
        return toolcompat.verify_models(
            [MODEL],
            {MODEL: spec(**kwargs)},
            key=KEY,
            build="6720",
            cache_path=self.path,
            now=1000.0,
            requester=requester,
        )

    def test_both_canaries_produce_verified_and_private_atomic_cache(self) -> None:
        requests: list[dict] = []

        def capture(body, key):
            requests.append(body)
            return success(body, key)

        result = self.verify(capture)[0]
        self.assertEqual(result["tool_support"], "verified")
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(
            [body["tools"][0]["parameters"]["required"] for body in requests],
            [["value"], ["input"]],
        )
        self.assertTrue(all(body["provider"] == {"zdr": True} for body in requests))
        self.assertNotIn("sk-or-", self.path.read_text(encoding="utf-8"))

    def test_structured_success_and_freeform_failure_is_partial(self) -> None:
        def requester(body, key):
            if body["tools"][0]["name"].startswith("codex_bridge_"):
                return 422, {"error": "custom tools unsupported"}
            return success(body, key)

        self.assertEqual(self.verify(requester)[0]["tool_support"], "partial")

    def test_structured_failure_is_unsupported(self) -> None:
        requests = 0

        def requester(body, key):
            nonlocal requests
            requests += 1
            return 400, {"error": "tools unsupported"}

        self.assertEqual(self.verify(requester)[0]["tool_support"], "unsupported")
        self.assertEqual(1, requests)

    def test_non_zdr_canary_does_not_claim_provider_zdr(self) -> None:
        requests: list[dict] = []

        def capture(body, key):
            requests.append(body)
            return success(body, key)

        self.verify(capture, zdr=False)
        self.assertTrue(all("provider" not in body for body in requests))

    def test_router_metadata_provider_is_saved_without_pipeline_data(self) -> None:
        summary = toolbridge.RouterSummary("DeepInfra", 2, 4, 200)

        def requester(body, key):
            status, document = success(body, key)
            return status, document, summary

        result = self.verify(requester)[0]
        self.assertEqual("DeepInfra", result["tool_provider"])
        self.assertEqual(2, result["tool_provider_attempt"])
        cache = json.loads(self.path.read_text(encoding="utf-8"))["entries"][MODEL]
        self.assertEqual("DeepInfra", cache["provider"])
        self.assertEqual(2, cache["provider_attempt"])
        self.assertNotIn("pipeline", cache)

    def test_metadata_unsupported_skips_paid_canary(self) -> None:
        called = False

        def requester(_body, _key):
            nonlocal called
            called = True
            raise AssertionError("network")

        result = self.verify(requester, status="unsupported")[0]
        self.assertEqual(result["tool_support"], "unsupported")
        self.assertFalse(called)
        self.assertFalse(self.path.exists())

    def test_auth_rate_limit_server_and_transport_failures_do_not_write_cache(self) -> None:
        cases = [401, 403, 429, 500]
        for status in cases:
            with self.subTest(status=status):
                self.path.unlink(missing_ok=True)

                def requester(_body, _key, code=status):
                    return code, {}

                with self.assertRaises(toolcompat.ToolCompatibilityError):
                    self.verify(requester)
                self.assertFalse(self.path.exists())

        def transport(_body, _key):
            raise toolcompat.ToolCompatibilityError("offline")

        with self.assertRaises(toolcompat.ToolCompatibilityError):
            self.verify(transport)
        self.assertFalse(self.path.exists())

    def test_api_drift_is_not_misclassified_or_cached(self) -> None:
        with self.assertRaises(toolcompat.ToolCompatibilityError):
            self.verify(lambda _body, _key: (200, {"items": []}))
        with self.assertRaises(toolcompat.ToolCompatibilityError):
            self.verify(lambda _body, _key: (200, []))
        self.assertFalse(self.path.exists())

    def test_failure_does_not_replace_an_existing_cache(self) -> None:
        self.verify()
        before = self.path.read_bytes()
        with self.assertRaises(toolcompat.ToolCompatibilityError):
            toolcompat.verify_models(
                [MODEL],
                {MODEL: spec()},
                key=KEY,
                build="6720",
                cache_path=self.path,
                force=True,
                now=2000.0,
                requester=lambda _body, _key: (429, {}),
            )
        self.assertEqual(before, self.path.read_bytes())


class CacheTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "tool-compatibility.json"
        toolcompat._atomic_write(
            self.path,
            {
                "schema_version": 1,
                "entries": {
                    MODEL: {
                        "chatgpt_build": "6720",
                        "tool_contract_version": toolcompat.TOOL_CONTRACT_VERSION,
                        "status": "verified",
                        "reason": "fixture",
                        "verified_at": 1000.0,
                    }
                },
            },
        )

    def test_cache_key_includes_model_build_and_contract_and_ttl(self) -> None:
        self.assertIsNotNone(toolcompat.cached_result(self.path, MODEL, "6720", now=1001.0))
        self.assertIsNone(toolcompat.cached_result(self.path, "other/model", "6720", now=1001.0))
        self.assertIsNone(toolcompat.cached_result(self.path, MODEL, "6721", now=1001.0))
        self.assertIsNone(
            toolcompat.cached_result(
                self.path,
                MODEL,
                "6720",
                now=1000.0 + toolcompat.CACHE_TTL_SECONDS,
            )
        )

    def test_contract_drift_invalidates_cache(self) -> None:
        document = json.loads(self.path.read_text(encoding="utf-8"))
        document["entries"][MODEL]["tool_contract_version"] = 0
        toolcompat._atomic_write(self.path, document)
        self.assertIsNone(toolcompat.cached_result(self.path, MODEL, "6720", now=1001.0))

    def test_effective_digest_changes_when_the_24_hour_cache_expires(self) -> None:
        specs = {MODEL: spec()}
        fresh = toolcompat.compatibility_digest(specs, self.path, "6720", now=1001.0)
        expired = toolcompat.compatibility_digest(
            specs,
            self.path,
            "6720",
            now=1000.0 + toolcompat.CACHE_TTL_SECONDS,
        )
        self.assertNotEqual(fresh, expired)


if __name__ == "__main__":
    unittest.main()
