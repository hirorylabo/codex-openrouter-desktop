from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest import mock
import urllib.parse
from types import SimpleNamespace

from codex_openrouter import auth
from codex_openrouter import cli
from codex_openrouter import openrouter
from codex_openrouter.profile import ResolvedProfile
from codex_openrouter.profile import (
    ProfileError,
    parse_apply_payload,
    resolve_apply_payload,
    resolve_profile,
    select_profile_path,
)


ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "models/registry.json"


class ProfileTests(unittest.TestCase):
    def write_profile(self, document: dict) -> tuple[tempfile.TemporaryDirectory[str], Path]:
        temporary = tempfile.TemporaryDirectory()
        path = Path(temporary.name) / "profile.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        return temporary, path

    def test_default_profile_resolves_exact_registry_subset(self) -> None:
        profile = resolve_profile(REGISTRY, ROOT / "profiles/default.json")
        self.assertEqual(5, len(profile.models))
        self.assertEqual("deepseek/deepseek-v4-pro", profile.default_model)
        self.assertEqual(set(profile.models), set(profile.registry))

    def test_custom_profile_can_use_verified_subset(self) -> None:
        temporary, path = self.write_profile(
            {
                "schema_version": 1,
                "name": "small",
                "models": ["minimax/minimax-m3"],
                "default_model": "minimax/minimax-m3",
            }
        )
        self.addCleanup(temporary.cleanup)
        profile = resolve_profile(REGISTRY, path)
        self.assertIsNone(profile.default_effort)
        self.assertEqual(64, len(profile.digest))

    def test_omitted_profile_preserves_installed_but_explicit_default_replaces_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_default = root / "default.json"
            installed = root / "installed.json"
            legacy = root / "legacy.json"
            for path in (source_default, installed, legacy):
                path.write_text("{}", encoding="utf-8")
            self.assertEqual(
                installed,
                select_profile_path(
                    argument=None,
                    source_default=source_default,
                    installed=installed,
                    legacy=legacy,
                ),
            )
            self.assertEqual(
                source_default,
                select_profile_path(
                    argument="default",
                    source_default=source_default,
                    installed=installed,
                    legacy=legacy,
                ),
            )

    def test_model_order_is_normalised_to_the_registry(self) -> None:
        """並び順の出所はregistryだけ。同じ選択なら常に同じdigestになる。"""
        registry_order = list(
            json.loads(REGISTRY.read_text(encoding="utf-8"))["models"]
        )
        temporary, path = self.write_profile(
            {
                "schema_version": 1,
                "name": "reversed",
                "models": list(reversed(registry_order)),
                "default_model": registry_order[0],
            }
        )
        self.addCleanup(temporary.cleanup)
        profile = resolve_profile(REGISTRY, path)
        self.assertEqual(registry_order, list(profile.models))
        self.assertEqual(registry_order, list(profile.registry))

    def test_apply_payload_only_accepts_the_three_editable_fields(self) -> None:
        model = "minimax/minimax-m3"
        accepted = parse_apply_payload(
            json.dumps({"schema_version": 1, "models": [model], "default_model": model})
        )
        resolved = resolve_apply_payload(REGISTRY, accepted, name="keep-me")
        self.assertEqual("keep-me", resolved.name)
        self.assertEqual((model,), resolved.models)
        for rejected in (
            {"schema_version": 1, "models": [model], "default_model": model, "name": "偽名"},
            {"schema_version": 1, "models": [model], "default_model": model, "default_effort": "max"},
            [model],
        ):
            with self.subTest(rejected=rejected), self.assertRaises(ProfileError):
                parse_apply_payload(json.dumps(rejected))
        with self.assertRaises(ProfileError):
            parse_apply_payload("{")

    def test_duplicate_and_unknown_models_fail(self) -> None:
        for models in (
            ["minimax/minimax-m3", "minimax/minimax-m3"],
            ["unverified/model"],
        ):
            temporary, path = self.write_profile(
                {
                    "schema_version": 1,
                    "models": models,
                    "default_model": models[0],
                }
            )
            with self.subTest(models=models), self.assertRaises(ProfileError):
                resolve_profile(REGISTRY, path)
            temporary.cleanup()

    def test_guardrail_model_set_must_match_exactly_and_reject_qwen_aliases(self) -> None:
        key_document = {"data": {"is_management_key": False, "limit": 10}}
        expected = {"minimax/minimax-m3"}
        for ids in (
            ["minimax/minimax-m3", "unverified/model"],
            ["minimax/minimax-m3", "~qwen/qwen3.8-max"],
            [],
        ):
            models_document = {"data": [{"id": model} for model in ids]}
            with self.subTest(ids=ids), mock.patch.object(
                openrouter, "_get_json", side_effect=[key_document, models_document]
            ), self.assertRaises(openrouter.OpenRouterError):
                openrouter.validate_key_and_profile("not-logged", expected)


class AuthenticationTests(unittest.TestCase):
    def test_fresh_setup_validates_once_then_installs_without_permanent_helper(self) -> None:
        from codex_openrouter import install as install_module

        key = "sk-or-v1-" + "z" * 64

        class Store:
            stored = False

            def exists(self) -> bool:
                return self.stored

            def store(self, _value: str) -> None:
                self.stored = True

        store = Store()
        temporary = mock.Mock()
        profile = ResolvedProfile(
            name="one",
            models=("minimax/minimax-m3",),
            default_model="minimax/minimax-m3",
            default_effort=None,
            registry={},
        )
        args = SimpleNamespace(profile=None, auth="paste", workspace="/tmp/workspace")
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(
                 cli.UserPaths,
                 "current",
                 return_value=SimpleNamespace(
                     state_dir=Path(directory) / "state",
                     stock_app=Path("/Applications/ChatGPT.app"),
                 ),
             ), \
             mock.patch.object(cli, "assert_apple_silicon"), \
             mock.patch.object(cli, "detect_stock"), \
             mock.patch.object(cli, "resolved_profile", return_value=(Path("profile.json"), profile)), \
             mock.patch.object(cli, "credential_store", return_value=(store, temporary)), \
             mock.patch.object(cli, "obtain_key", return_value=key), \
             mock.patch.object(cli, "validate_key_and_profile", return_value={"limit": 10}) as validate, \
             mock.patch.object(cli, "root", return_value=ROOT), \
             mock.patch.object(install_module, "_install_unlocked", return_value=0) as install:
            self.assertEqual(0, cli.setup_command(args))
        validate.assert_called_once_with(key, {"minimax/minimax-m3"})
        self.assertTrue(store.stored)
        temporary.cleanup.assert_called_once()
        self.assertFalse(install.call_args.kwargs["network_check"])

    def test_hidden_paste_accepts_key_without_echo(self) -> None:
        key = "sk-or-v1-" + "a" * 64
        with mock.patch("getpass.getpass", return_value=key) as prompt:
            self.assertEqual(key, auth.prompt_for_key())
        prompt.assert_called_once()

    def test_oauth_uses_real_loopback_callback_and_s256_exchange(self) -> None:
        key = "sk-or-v1-" + "b" * 64
        opened: dict[str, str] = {}

        def fake_open(command: list[str], **_kwargs: object) -> mock.Mock:
            url = command[-1]
            opened["url"] = url
            return mock.Mock(returncode=0)

        class Response:
            def __enter__(self) -> "Response":
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def read(self) -> bytes:
                return json.dumps({"key": key}).encode()

        captured: dict = {}

        class LoopbackServer:
            server_port = 43123

            def __init__(self, address: tuple[str, int], handler: object):
                self.address = address
                self.handler = handler
                self.timeout = 0

            def handle_request(self) -> None:
                server_side, client_side = socket.socketpair()
                try:
                    client_side.sendall(
                        b"GET /callback?code=test-code HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n"
                    )
                    self.handler(server_side, ("127.0.0.1", 43123), self)
                    client_side.recv(4096)
                finally:
                    server_side.close()
                    client_side.close()

            def server_close(self) -> None:
                return None

        def fake_exchange(request: object, timeout: int) -> Response:
            self.assertEqual(30, timeout)
            captured.update(json.loads(request.data))
            return Response()

        with mock.patch("subprocess.run", side_effect=fake_open), mock.patch(
            "urllib.request.urlopen", side_effect=fake_exchange
        ):
            self.assertEqual(
                key,
                auth.oauth_key(timeout_seconds=2, _server_factory=LoopbackServer),
            )

        query = urllib.parse.parse_qs(urllib.parse.urlparse(opened["url"]).query)
        self.assertEqual(["S256"], query["code_challenge_method"])
        self.assertTrue(query["callback_url"][0].startswith("http://127.0.0.1:"))
        self.assertEqual("test-code", captured["code"])
        self.assertEqual("S256", captured["code_challenge_method"])
        self.assertNotEqual(captured["code_verifier"], query["code_challenge"][0])

    def test_credential_store_never_passes_secret_as_argument(self) -> None:
        key = "sk-or-v1-" + "c" * 64
        helper = Path("/tmp/fake-helper")
        with mock.patch("subprocess.run", return_value=mock.Mock(returncode=0)) as run_mock:
            auth.CredentialStore(helper).store(key)
        arguments = run_mock.call_args.args[0]
        self.assertEqual([str(helper), "store"], arguments)
        self.assertNotIn(key, arguments)
        self.assertEqual(key, run_mock.call_args.kwargs["input"])

    def test_auth_rotate_validates_then_replaces_keychain_item(self) -> None:
        key = "sk-or-v1-" + "d" * 64

        class Store:
            stored = ""

            def exists(self) -> bool:
                return True

            def store(self, value: str) -> None:
                self.stored = value

        store = Store()
        profile = ResolvedProfile(
            name="test",
            models=("minimax/minimax-m3",),
            default_model="minimax/minimax-m3",
            default_effort=None,
            registry={},
        )
        args = SimpleNamespace(auth_action="rotate", method="paste")
        with mock.patch.object(
            cli.UserPaths, "current", return_value=SimpleNamespace()
        ), mock.patch.object(cli, "credential_store", return_value=(store, None)), mock.patch.object(
            cli, "resolved_profile", return_value=(Path("profile.json"), profile)
        ), mock.patch.object(cli, "obtain_key", return_value=key), mock.patch.object(
            cli, "validate_key_and_profile", return_value={"limit": 10}
        ) as validate:
            self.assertEqual(0, cli.auth_command(args))
        validate.assert_called_once_with(key, {"minimax/minimax-m3"})
        self.assertEqual(key, store.stored)


if __name__ == "__main__":
    unittest.main()
