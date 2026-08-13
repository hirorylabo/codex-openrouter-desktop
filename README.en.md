# codex-openrouter-desktop

> [!WARNING]
> **This is an unofficial, experimental workaround. It is not endorsed by or affiliated with OpenAI or OpenRouter.** The first release supports Apple Silicon macOS only. A ChatGPT.app update may stop it from working, and the software is provided without warranty. OpenAI, ChatGPT, Codex, OpenRouter, and model names are trademarks of their respective owners.

This CLI leaves the official signed `/Applications/ChatGPT.app` untouched. A marker block in `~/.codex/config.toml` and a local guard add verified OpenRouter models to the stock model picker only while the dedicated launcher is running. It creates no clone and patches no ASAR. The project does not distribute ChatGPT.app, ASAR files, API keys, cookies, history, userData, or logs.

`Codex OpenRouter.app` is a small management launcher. Opening it shows the current model count, default model, and workspace; ChatGPT starts only when you press **OpenRouterで起動**.

[日本語](./README.md)

## Scope

- Apple Silicon macOS only; no Windows, Linux, Intel Mac, or Homebrew support.
- `v0.2.0` is a prerelease. It no longer pins a specific ChatGPT build because ASAR patching was removed.
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

Install the official signed ChatGPT.app, Xcode Command Line Tools, and Python 3.11+. (The Node.js dependency was removed in v0.2.0.) In OpenRouter, disable prompt training, public free endpoints, and the 1% data discount; enable Non-frontier ZDR; create a Guardrail whose exact model allowlist matches the selected profile; assign it to the OAuth-created key; and set a spend limit.

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

The settings screen opens from the panel, the application menu, or `⌘,`. It lists the verified models from the bundled registry as checkboxes and lets you pick one default among the selected ones. There is no entry point for arbitrary slugs.

- At least one model is required. Removing the current default disables saving until a new default is chosen explicitly.
- **OpenRouter Guardrailを開く** opens the Guardrail settings page.
- **検証して保存** requires the API key's effective concrete model set to match the selection exactly. A mismatch, a network failure, or a Keychain failure changes nothing on disk.
- A successful save reports that the change takes effect on the next OpenRouter launch, where the new default model is applied exactly once.
- Editing is disabled while OpenRouter mode is running.
- The screen never reads or displays the API key. Validation happens in the Python CLI, which reads the Keychain directly.

### Switching from the stock app

The stock mode and OpenRouter mode cannot run concurrently because they use the same application, userData, and `~/.codex/config.toml`. Clicking `Codex OpenRouter.app` while the stock app is running asks whether to quit it normally and restart the same signed app in OpenRouter mode.

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
codex-openrouter guard-log [--clear]
codex-openrouter upgrade [--profile default|FILE] [--if-needed]
codex-openrouter rollback
codex-openrouter auth login|rotate|logout
```

Custom profiles may only select models already present in [`models/registry.json`](./models/registry.json). Before any application write, the CLI requires the API key's effective concrete model set to exactly match the profile. The normalized installed profile drives the picker, guard, watcher, and doctor, and its model order always comes from the registry. A normal or automatic upgrade preserves it; an explicit `upgrade --profile ...` and the settings screen replace it. A changed profile applies its default model once on the next dedicated launch.

`profile show --json` and `profile apply --stdin-json` are the only update path the launcher uses; the Swift side duplicates no profile, Keychain, or Guardrail logic. `apply` accepts only `schema_version`, `models`, and `default_model`, and replaces the profile, supervisor state, install manifest, and stale catalogs in a single transaction under the lifecycle lock. Re-saving an identical selection is a no-op that does not re-arm the default model.

After downloading a newer release or updating a source checkout, close ChatGPT.app and run the repository's `./codex-openrouter upgrade`. Runtime files, the launcher, manifest, installed profile, and supervisor state are staged and promoted transactionally. Verification failure restores every promoted target. `codex-openrouter rollback` restores the previous promoted set.

When the launcher is inactive, the persistent provider definition points to non-connecting port `0` and its authentication command always fails. During a launch, the guard binds an ephemeral loopback port; normal cleanup restores the inactive stub before stopping the guard. After a forced kill or power loss, the next dedicated launch performs the same self-heal before starting ChatGPT.app.

## Development

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
PYTHONPATH=src python3 -m compileall -q src portable scripts
PYTHONPATH=src python3 scripts/macos_synthetic_e2e.py
python3 scripts/secret_scan.py --tree .
python3 scripts/build_release.py "v$(cat VERSION)" --dist /tmp/codex-openrouter-dist
```

For manual checks with the real ChatGPT.app, run the isolated-home E2E first. After upgrading the installed runtime, run two launcher cycles; the latter command asks you to quit ChatGPT.app normally during each cycle.

```bash
PYTHONPATH=src python3 scripts/macos_live_e2e.py
scripts/macos_installed_e2e.zsh
```

See the Japanese README for the full operational details, [`SECURITY.md`](./SECURITY.md) for vulnerability reporting, and [`THIRD_PARTY_NOTICES.md`](./THIRD_PARTY_NOTICES.md) for third-party licensing.
