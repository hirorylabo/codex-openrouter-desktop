# codex-openrouter-desktop

> [!WARNING]
> **This is an unofficial, experimental workaround. It is not endorsed by or affiliated with OpenAI or OpenRouter.** The first release supports Apple Silicon macOS only. A ChatGPT.app update may stop it from working, and the software is provided without warranty. OpenAI, ChatGPT, Codex, OpenRouter, and model names are trademarks of their respective owners.

[![CI](https://github.com/hirorylabo/codex-openrouter-desktop/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/hirorylabo/codex-openrouter-desktop/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](./LICENSE)
[![Latest release](https://img.shields.io/github/v/release/hirorylabo/codex-openrouter-desktop?include_prereleases&sort=semver)](https://github.com/hirorylabo/codex-openrouter-desktop/releases)

This CLI leaves the official signed `/Applications/ChatGPT.app` untouched. A marker block in `~/.codex/config.toml` and a local guard add verified OpenRouter models to the stock model picker only while the dedicated launcher is running. It creates no clone and patches no ASAR. The project does not distribute ChatGPT.app, ASAR files, API keys, cookies, history, userData, or logs.

`Codex OpenRouter.app` is a small management launcher. Opening it shows the current model count, default model, and workspace; ChatGPT starts only when you press **OpenRouterで起動**.

[日本語](./README.md)

## Scope

- Apple Silicon macOS only; no Windows, Linux, Intel Mac, or Homebrew support.
- `v0.2.0` is a prerelease. ASAR/catalog handling is not pinned to a build. The tool wire is intentionally limited to the live-canary-verified **latest and previous builds**; an unknown build blocks OpenRouter launch only and leaves the stock app usable.
- While an OpenRouter model is selected, stock background threads bound to the same provider are rejected by the allowlist guard. Those background features are unavailable for that period.
- Switching across the native/OpenRouter provider boundary inside an existing thread fails; start a new thread instead.
- OpenRouter usage charges are the user's responsibility. Network doctor and candidate canaries may incur a small charge.

## Verified download

The project deliberately does not use `curl | bash`.

```bash
mkdir codex-openrouter-download && cd codex-openrouter-download
gh release download v0.2.0 --repo hirorylabo/codex-openrouter-desktop \
  --pattern 'codex-openrouter-desktop-v0.2.0.tar.gz' \
  --pattern 'codex-openrouter-desktop-v0.2.0.spdx.json' \
  --pattern 'SHA256SUMS'
gh attestation verify codex-openrouter-desktop-v0.2.0.tar.gz \
  --repo hirorylabo/codex-openrouter-desktop
shasum -a 256 -c SHA256SUMS
tar -xzf codex-openrouter-desktop-v0.2.0.tar.gz
cd codex-openrouter-desktop-v0.2.0
```

## OpenRouter preparation

Install the official signed ChatGPT.app, Xcode Command Line Tools, and Python 3.11+. (The Node.js dependency was removed in v0.2.0.) In OpenRouter, disable prompt training, public free endpoints, and the 1% data discount; enable Non-frontier ZDR; and **set a spend limit on the key**.

A Guardrail is optional as of v0.2.0. Earlier versions required an exact model allowlist and validated that the key's effective model set matched the profile *exactly*, which meant editing the Guardrail every time you added a model. Validation now only checks that the selected models are callable with the key, so the picker still never offers a model that cannot be called, and retired or renamed models are still detected. What is given up is the cap on which models a leaked key could bill; the spend limit takes over that role.

The default profile contains only `deepseek/deepseek-v4-flash-0731`, with `high` reasoning effort. Add other OpenRouter models from the settings screen.

## Setup

```bash
./codex-openrouter check
./codex-openrouter setup --workspace "$HOME/Documents"
# Existing key, hidden paste:
./codex-openrouter setup --auth paste --workspace "$HOME/Documents"
```

OAuth uses PKCE S256 with a temporary random-port `127.0.0.1` callback. The API key is stored only in macOS Keychain under service `io.github.hirorylabo.codex-openrouter-desktop`. Only the local guard retrieves it. Codex authenticates to the running guard with a separate launch-scoped token, so the OpenRouter key never crosses loopback. Command-line key arguments, `.env`, shell profiles, configuration, and logs are not supported.

When Finder's desktop stacks are enabled, the launcher appears inside the Applications stack. Turn off **Finder > View > Use Stacks** to keep it directly visible. Setup and upgrade regenerate its project icon, signature, and default workspace.

### Management launcher and model settings

The launcher is a regular macOS app with a Dock icon and an application menu, but it never becomes a resident daemon: it quits when its window closes or the OpenRouter session ends. Dropping a folder on it only changes the workspace shown in the panel; nothing starts until you press the primary button.

The settings screen opens from the panel, the application menu, or `⌘,`. It lists the models OpenRouter actually serves and lets you pick one default among the selected ones. There is no entry point for arbitrary slugs. Models without tool support remain discoverable and are hidden by default rather than silently discarded. The `Codex tool / provider` column shows the measured provider; its tooltip includes the verification time and attempt number.

The column reports `verified` (structured and freeform canaries passed through the Tool Bridge), `partial` (structured passed, freeform failed), `declared` (OpenRouter advertises `tools`, not measured), `unknown`, or `unsupported`. These statuses cover direct structured/freeform tools only; they do not cover browser, search, or Node REPL. The **tool非対応も表示 (N)** control reveals unsupported rows; selected rows always remain visible.

Usage is **token volume, not connection count** — that is all OpenRouter publishes, and only for the top 50 models per day. Anything outside that shows `—` rather than zero.

- At least one model is required. Removing the current default disables saving until a new default is chosen explicitly.
- **OpenRouter Guardrailを開く** opens the Guardrail settings page (optional).
- **検証して保存** runs low-token structured/freeform canaries using the same bridge wire as runtime for newly selected, unmeasured models after a possible-charge confirmation. Authentication errors, 429, 5xx, and transport failures are not classified as unsupported and change neither the tool cache nor the profile.
- `partial` and `unsupported` models remain selectable only after an exact-model warning that direct tools such as `exec` and `apply_patch` may fail.
- A successful save reports that the change takes effect on the next OpenRouter launch, where the new default model is applied exactly once.
- Editing is disabled while OpenRouter mode is running.
- The screen never reads or displays the API key. Validation happens in the Python CLI, which reads the Keychain directly.

### Switching from the stock app

The stock mode and OpenRouter mode cannot run concurrently because they use the same application, userData, and `~/.codex/config.toml`. Pressing **OpenRouterで起動** while the stock app is running asks whether to quit it normally and restart the same signed app in OpenRouter mode. Merely opening the launcher panel changes nothing.

- Cancel brings the existing stock app forward without changing configuration or starting the guard.
- Switching requests normal termination only; the launcher never force-kills an unresponsive app.
- `codex-openrouter launch` has no confirmation UI, so close the stock app before using the CLI.
- Setup, upgrade, rollback, migration, and launch operations are serialized per user; a competing operation stops before changing shared state.

## Commands

```text
codex-openrouter check
codex-openrouter setup [--workspace PATH] [--profile default|FILE] [--auth oauth|paste]
codex-openrouter launch [PATH]
codex-openrouter doctor [--network] [--runtime] [--secret-scan]
codex-openrouter migrate
codex-openrouter profile show --json
codex-openrouter profile apply --stdin-json
codex-openrouter models list --json [--refresh]
codex-openrouter models verify-tools --stdin-json
codex-openrouter guard-log [--clear]
codex-openrouter upgrade [--profile default|FILE] [--if-needed]
codex-openrouter rollback
codex-openrouter auth login|rotate|logout
```

Custom profiles may only select models present in the active registry — the bundled [`models/registry.json`](./models/registry.json), or the installed registry that grows as you add models from the live catalog. Before any application write, the CLI requires every selected model to be callable with the API key. The normalized installed profile drives the picker, guard, watcher, and doctor, and its model order always comes from the registry. A normal or automatic upgrade preserves it; an explicit `upgrade --profile ...` and the settings screen replace it. A changed profile applies its default model once on the next dedicated launch.

`profile show --json`, `models list --json`, `models verify-tools --stdin-json`, and `profile apply --stdin-json` are the launcher's update path; the Swift side duplicates no Keychain or profile logic. Tool results are cached atomically for 24 hours by model ID, ChatGPT build, and tool-contract version. `profile apply` also accepts optional exact IDs in `tool_risk_acknowledged`; this is an additive schema-v1 field.

Every OpenRouter catalog entry explicitly uses `tool_mode: "direct"`, `node_repl_disabled: true`, `supports_search_tool: false`, and no experimental supported tools. Following [OpenAI's model guidance](https://developers.openai.com/api/docs/guides/latest-model), it therefore does not inherit GPT-5.6 Code Mode or hosted search. Native catalog entries remain unchanged.

### Minimal Tool Bridge

Protocol handling is isolated in [`src/codex_openrouter/toolbridge.py`](./src/codex_openrouter/toolbridge.py). Ordinary functions pass through. Namespace children and custom tools become request-unique strict functions; `apply_patch` uses a required `patch` string and other custom tools use `input`. The SSE path restores the original namespace/custom events while preserving call, item, and output indexes. Unknown tools, malformed JSON, missing lifecycle events, and truncated streams fail closed rather than being guessed or healed.

Tool-bearing requests do not set a price sort, so OpenRouter's default [Auto Exacto](https://openrouter.ai/docs/guides/routing/auto-exacto) remains active. They also send `X-OpenRouter-Metadata: enabled`. Only provider, attempt, candidate count, and status are retained; the metadata object, pipeline, prompts, and tool arguments are stripped. Missing metadata on cache hits or early errors never means “unsupported.”

`codex-relay` is not a runtime dependency. [`UPSTREAMS.md`](./UPSTREAMS.md) pins the reviewed commits/files and the accepted/rejected behavior. Weekly CI reports upstream drift and asks for fixture regeneration; it never merges upstream changes.

ZDR enforcement is per model. Models with a live ZDR endpoint still get `provider.zdr` forced; models without one do not, because forcing it there guarantees failure. Adding a non-ZDR model requires an explicit confirmation, is shown permanently in the launcher panel, and is reported by `doctor --network` on every run.

After downloading a newer release or updating a source checkout, close ChatGPT.app and run the repository's `./codex-openrouter upgrade`. Runtime files, the launcher, manifest, installed profile, and supervisor state are staged and promoted transactionally. Verification failure restores every promoted target. `codex-openrouter rollback` restores the previous promoted set.

When upgrading from the former multi-provider development build, the migration preserves added OpenRouter models while atomically removing retired-provider registry entries and managed provider configuration. It neither reads nor deletes the retired provider's Keychain item.

When the launcher is inactive, the persistent provider definition points to non-connecting port `0` and its authentication command always fails. During a launch, the guard binds an ephemeral loopback port; normal cleanup restores the inactive stub before stopping the guard. After a forced kill or power loss, the next dedicated launch performs the same self-heal before starting ChatGPT.app.

## Development

```bash
PYTHONPATH=src python3 scripts/run_unit_tests.py
PYTHONPATH=src python3 -m compileall -q src portable scripts
PYTHONPATH=src python3 scripts/macos_synthetic_e2e.py
uvx ruff@0.16.3 check .
xcrun swiftc portable/launcher/app/*.swift -o /tmp/CodexOpenRouterLauncher
xcrun swiftc -parse-as-library \
  portable/launcher/app/ProfileBridge.swift \
  portable/tests/DecoderCompatTests.swift -o /tmp/decoder-compat && /tmp/decoder-compat
python3 scripts/secret_scan.py --tree .
python3 scripts/check_upstreams.py --validate-only
python3 scripts/build_release.py "v$(cat VERSION)" --dist /tmp/codex-openrouter-dist
```

`compileall` checks syntax; `ruff` is the gate that catches undefined names such as `F821`. The unit-test runner rejects every non-loopback socket destination so a missing network stub fails closed. Update `tests/fixtures/launcher-*.json` together with the decoder harness whenever CLI JSON fields change.

For manual checks with the real ChatGPT.app, run the isolated-home E2E first. After upgrading the installed runtime, run two launcher cycles; the latter command asks you to quit ChatGPT.app normally during each cycle. When passed an empty workspace, cycle 1 asks you to use the newly opened chat for one `pwd`, one `apply_patch`, and one namespace-child call. A read-only JSONL audit requires the exact cwd, tool, arguments, and output before the harness can continue. Resuming an existing chat preserves its stored cwd, so that mismatch stops the run before cycle 2. Treat `--open-project` as a current-build internal contract rather than the acceptance signal.

```bash
PYTHONPATH=src python3 scripts/macos_live_e2e.py
scripts/macos_installed_e2e.zsh
scripts/macos_installed_e2e.zsh /private/tmp/codex-openrouter-e2e.EMPTY
```

See the Japanese README for the full operational details, [`SECURITY.md`](./SECURITY.md) for vulnerability reporting, and [`THIRD_PARTY_NOTICES.md`](./THIRD_PARTY_NOTICES.md) for third-party licensing.
