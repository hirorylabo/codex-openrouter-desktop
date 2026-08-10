from __future__ import annotations

from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tarfile
import time

from .app import UserPaths, detect_stock, load_adapter, sha256
from . import __version__
from .auth import CredentialStore
from .openrouter import validate_key_and_profile
from .processes import process_pids
from .profile import resolve_profile


class CandidateError(RuntimeError):
    pass


def run(
    command: list[str],
    *,
    environment: dict[str, str] | None = None,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        env=environment,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if result.returncode != 0:
        raise CandidateError(
            f"command failed ({result.returncode}): {Path(command[0]).name}\n{result.stdout[-4000:]}"
        )
    return result


def atomic_copy(source: Path, target: Path, mode: int = 0o600) -> None:
    temporary = target.with_name(f".{target.name}.candidate-new")
    if temporary.exists() or temporary.is_symlink():
        raise CandidateError(f"stale temporary file exists: {temporary}")
    shutil.copyfile(source, temporary)
    temporary.chmod(mode)
    os.replace(temporary, target)


def write_json(path: Path, document: dict) -> None:
    temporary = path.with_name(f".{path.name}.candidate-new")
    if temporary.exists() or temporary.is_symlink():
        raise CandidateError(f"stale temporary file exists: {temporary}")
    temporary.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    os.replace(temporary, path)


def command_environment(root: Path) -> dict[str, str]:
    environment = os.environ.copy()
    environment.pop("OPENROUTER_API_KEY", None)
    environment.pop("CODEX_ACCESS_TOKEN", None)
    environment["PYTHONPATH"] = str(root / "src")
    environment["CODEX_OPENROUTER_SUPPORT_ROOT"] = str(root)
    return environment


def stop_exact_processes(executable: Path) -> None:
    pids = process_pids(executable)
    for pid in pids:
        os.kill(pid, signal.SIGTERM)
    deadline = time.time() + 10
    while time.time() < deadline and process_pids(executable):
        time.sleep(0.25)
    remaining = process_pids(executable)
    for pid in remaining:
        os.kill(pid, signal.SIGKILL)
    if remaining:
        time.sleep(0.5)
    if process_pids(executable):
        raise CandidateError(f"rollbackのためappを停止できません: {executable}")


def issue_bundle(candidate_root: Path, key: str) -> Path:
    include = [
        candidate_root / "report.json",
        candidate_root / "adapter.json",
        candidate_root / "doctor-preflight.txt",
        candidate_root / "doctor-runtime.txt",
    ]
    needle = key.encode("utf-8")
    existing = [path for path in include if path.is_file()]
    exposed = [str(path) for path in existing if needle in path.read_bytes()]
    if exposed:
        raise CandidateError(f"diagnostic bundle source contains the API key: {exposed}")
    archive = candidate_root / "github-issue-diagnostics.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        for path in existing:
            bundle.add(path, arcname=path.name, recursive=False)
    archive.chmod(0o600)
    return archive


def render_runtime(
    source_root: Path,
    profile: Path,
    output_home: Path,
    credential: Path,
) -> None:
    run(
        [
            shutil.which("python3") or "/usr/bin/python3",
            str(source_root / "portable/render_runtime.py"),
            "--registry",
            str(source_root / "models/registry.json"),
            "--profile",
            str(profile),
            "--template",
            str(source_root / "portable/templates/config.toml.in"),
            "--output-home",
            str(output_home),
            "--credential-helper",
            str(credential),
        ],
        environment=command_environment(source_root),
    )


def backup_live_state(paths: UserPaths, backup_dir: Path) -> list[str]:
    backup_dir.mkdir(parents=True, mode=0o700)
    names = [
        "adapter.json",
        "config.toml",
        "profile.json",
        "registry.json",
        "desktop-model-providers.json",
        "model-catalogs/openrouter.json",
        "price-refresh-state.json",
        "install-manifest.json",
    ]
    copied: list[str] = []
    for name in names:
        source = paths.codex_home / name
        if source.is_file() and not source.is_symlink():
            target = backup_dir / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            copied.append(name)
    write_json(backup_dir / "inventory.json", {"files": copied})
    return copied


def restore_live_state(paths: UserPaths, backup_dir: Path) -> None:
    inventory = json.loads((backup_dir / "inventory.json").read_text(encoding="utf-8"))
    for name in inventory.get("files", []):
        source = backup_dir / name
        target = paths.codex_home / name
        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_copy(source, target)


def update(source_root: Path, paths: UserPaths, profile_argument: str) -> int:
    if not paths.openrouter_app.is_dir() or not paths.codex_home.is_dir():
        raise CandidateError("既存installationがありません。known buildではsetupを使用してください")
    if not paths.credential_helper.is_file():
        raise CandidateError("Keychain credential helperがありません")
    stock = detect_stock(paths.stock_app)
    known = load_adapter(source_root / "adapters/index.json", stock)
    active_adapter_path = paths.codex_home / "adapter.json"
    active_adapter = json.loads(active_adapter_path.read_text(encoding="utf-8"))
    if known and active_adapter.get("id") == known.get("id"):
        receipt = paths.codex_home / "install-manifest.json"
        if receipt.is_file() and json.loads(receipt.read_text(encoding="utf-8")).get(
            "release_version"
        ) == __version__:
            print(f"UPDATE: v{__version__} / adapter {known['id']} は最新です")
            return 0
    if known:
        from .upgrade import upgrade

        return upgrade(source_root, paths, profile_argument)

    profile_path = (
        paths.codex_home / "profile.json"
        if profile_argument == "default"
        else Path(profile_argument).expanduser().resolve()
    )
    profile = resolve_profile(source_root / "models/registry.json", profile_path)
    key = CredentialStore(paths.credential_helper).get()
    metadata = validate_key_and_profile(key, set(profile.models))
    if metadata.get("limit") is None:
        print("WARNING: API keyのspend limitが未設定です。")

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    safe_build = "".join(char if char.isalnum() or char in ".-_" else "_" for char in stock.build)
    candidate_root = paths.codex_home / "candidates" / f"{timestamp}-build-{safe_build}"
    candidate_root.mkdir(parents=True, mode=0o700)
    candidate_app = candidate_root / "ChatGPT OpenRouter Candidate.app"
    staging_home = candidate_root / "codex-home"
    staging_home.mkdir(mode=0o700)
    user_data = candidate_root / "user-data"
    user_data.mkdir(mode=0o700)
    report: dict = {
        "schema_version": 1,
        "created_at": timestamp,
        "stock": {
            "version": stock.version,
            "build": stock.build,
            "asar_sha256": stock.asar_sha256,
        },
        "profile": profile.as_json(),
        "checks": [],
        "promoted": False,
    }
    write_json(candidate_root / "report.json", report)

    print(f"未知build candidateを作成します（純正appは変更しません）: {candidate_root}")
    run(["/usr/bin/ditto", str(paths.stock_app), str(candidate_app)])
    if sha256(candidate_app / "Contents/Resources/app.asar") != stock.asar_sha256:
        raise CandidateError("candidate cloneのASARがstock hashと一致しません")
    if detect_stock(paths.stock_app).asar_sha256 != stock.asar_sha256:
        raise CandidateError("candidate作成中にstock appが変化しました")
    report["checks"].append("stock-signature-and-hash-unchanged")

    render_runtime(source_root, profile_path, staging_home, paths.credential_helper)
    runtime_bin = candidate_root / "runtime-bin"
    runtime_bin.mkdir(mode=0o700)
    python = shutil.which("python3") or "/usr/bin/python3"
    from .upgrade import render_template

    render_template(
        source_root / "portable/templates/codex-openrouter-refresh.py.in",
        runtime_bin / "codex-openrouter-refresh",
        paths.home,
        python,
    )
    render_template(
        source_root / "portable/templates/codex-openrouter-doctor.py.in",
        runtime_bin / "codex-openrouter-doctor",
        paths.home,
        python,
    )
    js_root = candidate_root / "semantic-patcher"
    run(["/usr/bin/ditto", str(source_root / "portable/patcher-js"), str(js_root)])
    npm = shutil.which("npm")
    node = shutil.which("node")
    if not npm or not node:
        raise CandidateError("unknown-build candidateにはNode.jsとnpmが必要です")
    run([npm, "ci", "--ignore-scripts"], cwd=js_root)

    manifest = json.loads(
        (source_root / "portable/manifest.json").read_text(encoding="utf-8")
    )["upstream_patcher"]
    patch_root = paths.home / ".local/share/codex-openrouter-patcher" / manifest["commit"]
    upstream = patch_root / "patch_chatgpt_providers.py"
    license_path = patch_root / "LICENSE"
    if not upstream.is_file() or sha256(upstream) != manifest["source_sha256"]:
        raise CandidateError("pinned upstream patcherが欠落またはhash不一致です")
    if not license_path.is_file() or sha256(license_path) != manifest["license_sha256"]:
        raise CandidateError("pinned upstream Unlicenseが欠落またはhash不一致です")

    adapter_output = candidate_root / "adapter.json"
    run(
        [
            shutil.which("python3") or "/usr/bin/python3",
            str(source_root / "portable/patcher/patch_candidate.py"),
            "--app",
            str(candidate_app),
            "--candidate-root",
            str(candidate_root),
            "--config",
            str(staging_home / "desktop-model-providers.json"),
            "--backup-dir",
            str(candidate_root / "patch-backup"),
            "--upstream",
            str(upstream),
            "--upstream-sha256",
            manifest["source_sha256"],
            "--transform",
            str(js_root / "semantic_transform.mjs"),
            "--node",
            str(node),
            "--stock-hash",
            stock.asar_sha256,
            "--version",
            stock.version,
            "--build",
            stock.build,
            "--adapter-output",
            str(adapter_output),
        ]
    )
    atomic_copy(adapter_output, staging_home / "adapter.json")
    candidate_adapter = json.loads(adapter_output.read_text(encoding="utf-8"))
    asar_bytes = (candidate_app / "Contents/Resources/app.asar").read_bytes()
    registry = json.loads((source_root / "models/registry.json").read_text(encoding="utf-8"))["models"]
    outside_models = set(registry) - set(profile.models)
    if any(model.encode("utf-8") in asar_bytes for model in outside_models):
        raise CandidateError("candidate ASARにprofile外のregistry model固定文字列があります")
    run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(candidate_app)])
    report["checks"].extend(
        [
            "semantic-routing-transform=1",
            "semantic-visibility-transform=1",
            "semantic-label-transform=1",
            "asar-integrity-and-codesign-valid",
            "no-profile-external-registry-model-literals",
        ]
    )

    environment = command_environment(source_root)
    environment.update(
        {
            "CODEX_OPENROUTER_APP": str(candidate_app),
            "CODEX_OPENROUTER_HOME": str(staging_home),
            "CODEX_OPENROUTER_USER_DATA": str(user_data),
            "CODEX_OPENROUTER_CREDENTIAL": str(paths.credential_helper),
        }
    )
    refresh = runtime_bin / "codex-openrouter-refresh"
    doctor = runtime_bin / "codex-openrouter-doctor"
    run([str(refresh), "--init"], environment=environment)
    print("INFO: candidate network canaryは少量のOpenRouter API利用料が発生する場合があります。")
    doctor_preflight = run(
        [str(doctor), "--network", "--secret-scan"], environment=environment
    )
    (candidate_root / "doctor-preflight.txt").write_text(
        doctor_preflight.stdout, encoding="utf-8"
    )
    (candidate_root / "doctor-preflight.txt").chmod(0o600)
    report["checks"].extend(["app-server-profile-exact", "all-efforts-zdr-provider-canary"])

    log_path = staging_home / "logs/desktop.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_handle = log_path.open("ab")
    launch_environment = environment.copy()
    launch_environment["CODEX_HOME"] = str(staging_home)
    candidate_process = subprocess.Popen(
        [
            str(candidate_app / "Contents/MacOS/ChatGPT"),
            f"--user-data-dir={user_data}",
            str(Path.cwd()),
        ],
        env=launch_environment,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
    )
    try:
        time.sleep(5)
        if candidate_process.poll() is not None:
            raise CandidateError(f"candidate appが起動直後に終了しました: {log_path}")
        doctor_runtime = run(
            [str(doctor), "--runtime", "--secret-scan"], environment=environment
        )
        (candidate_root / "doctor-runtime.txt").write_text(
            doctor_runtime.stdout, encoding="utf-8"
        )
        (candidate_root / "doctor-runtime.txt").chmod(0o600)
        print("candidateでモデルピッカーのprofile一致と、短いタスクを1件開始できることを目視確認してください。")
        approval = input("両方確認でき、昇格する場合だけ PROMOTE と入力: ")
    finally:
        if candidate_process.poll() is None:
            candidate_process.terminate()
            try:
                candidate_process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                candidate_process.kill()
                candidate_process.wait(timeout=5)
        log_handle.close()
    if approval != "PROMOTE":
        report["result"] = "candidate-kept-not-promoted"
        write_json(candidate_root / "report.json", report)
        bundle = issue_bundle(candidate_root, key)
        print(f"昇格せずcandidateを保持しました: {candidate_root}")
        print(f"秘密値除外済み診断bundle: {bundle}")
        return 0
    if candidate_process.poll() is None:
        raise CandidateError("candidate appが終了していないため昇格しません")
    stock_before_promotion = detect_stock(paths.stock_app)
    if stock_before_promotion != stock:
        raise CandidateError("目視確認中にstock appが変化したため昇格しません")

    active_executable = paths.openrouter_app / "Contents/MacOS/ChatGPT"
    if process_pids(active_executable):
        input("現行のCodex OpenRouterを通常終了し、Enterを押してください: ")
    if process_pids(active_executable):
        raise CandidateError("現行の専用appが起動中のため昇格しません")

    backup_root = paths.home / "Applications/ChatGPT OpenRouter Backups"
    backup_root.mkdir(parents=True, exist_ok=True)
    backup_app = backup_root / f"ChatGPT OpenRouter.pre-candidate-{timestamp}.app"
    backup_metadata = backup_root / f"metadata-pre-candidate-{timestamp}"
    backup_live_state(paths, backup_metadata)
    paths.openrouter_app.rename(backup_app)
    try:
        run(["/usr/bin/ditto", str(candidate_app), str(paths.openrouter_app)])
        render_runtime(source_root, profile_path, paths.codex_home, paths.credential_helper)
        atomic_copy(adapter_output, active_adapter_path)
        atomic_copy(
            staging_home / "model-catalogs/openrouter.json",
            paths.codex_home / "model-catalogs/openrouter.json",
        )
        if (staging_home / "price-refresh-state.json").is_file():
            atomic_copy(
                staging_home / "price-refresh-state.json",
                paths.codex_home / "price-refresh-state.json",
            )
        source_commit_result = subprocess.run(
            ["/usr/bin/git", "-C", str(source_root), "rev-parse", "HEAD"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        source_commit = (
            source_commit_result.stdout.strip()
            if source_commit_result.returncode == 0 and source_commit_result.stdout.strip()
            else "release-archive"
        )
        workspace = Path.cwd()
        receipt = paths.codex_home / "install-manifest.json"
        if receipt.is_file():
            saved = json.loads(receipt.read_text(encoding="utf-8")).get("workspace")
            if isinstance(saved, str) and Path(saved).is_dir():
                workspace = Path(saved)
        write_json(
            receipt,
            {
                "schema_version": 2,
                "release_version": __version__,
                "source_commit": source_commit,
                "adapter_id": candidate_adapter["id"],
                "chatgpt_version": stock.version,
                "chatgpt_build": stock.build,
                "stock_asar_sha256": stock.asar_sha256,
                "workspace": str(workspace),
            },
        )
        active_user_data = paths.codex_home / "user-data" / candidate_adapter["id"]
        active_user_data.parent.mkdir(parents=True, exist_ok=True)
        if active_user_data.exists():
            raise CandidateError(f"candidate userData target already exists: {active_user_data}")
        run(["/usr/bin/ditto", str(user_data), str(active_user_data)])

        run(
            [str(doctor), "--network", "--secret-scan"],
            environment=command_environment(source_root),
        )
        run([str(paths.bin_dir / "codex-openrouter-app"), str(workspace)])
        run(
            [str(doctor), "--runtime", "--secret-scan"],
            environment=command_environment(source_root),
        )
        if detect_stock(paths.stock_app) != stock:
            raise CandidateError("昇格後にstock appの署名/version/build/hashが変化しました")
    except Exception as promotion_error:
        stop_exact_processes(active_executable)
        failed = candidate_root / "failed-promoted.app"
        if paths.openrouter_app.exists():
            paths.openrouter_app.rename(failed)
        run(["/usr/bin/ditto", str(backup_app), str(paths.openrouter_app)])
        restore_live_state(paths, backup_metadata)
        rollback_check = subprocess.run(
            [str(doctor)], text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
        )
        report["rollback_doctor_returncode"] = rollback_check.returncode
        report["result"] = "promotion-failed-auto-rolled-back"
        write_json(candidate_root / "report.json", report)
        issue_bundle(candidate_root, key)
        raise CandidateError(
            f"candidate昇格後の検証に失敗し、自動rollbackしました: {promotion_error}"
        ) from promotion_error

    report["promoted"] = True
    report["result"] = "promoted-and-verified"
    report["active_adapter"] = candidate_adapter
    write_json(candidate_root / "report.json", report)
    bundle = issue_bundle(candidate_root, key)
    print(f"UPDATE: candidateを昇格し、doctor/runtimeを検証しました: {candidate_adapter['id']}")
    print(f"rollback app: {backup_app}")
    print(f"秘密値除外済み診断bundle（自動送信なし）: {bundle}")
    return 0
