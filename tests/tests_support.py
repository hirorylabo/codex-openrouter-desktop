"""テスト用の共通ヘルパー。"""

from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from codex_openrouter.app import UserPaths  # noqa: E402


def make_paths(root: Path) -> UserPaths:
    """実HOMEに触れない UserPaths を組む。"""
    return UserPaths(
        home=root,
        stock_app=root / "ChatGPT.app",
        openrouter_app=root / "legacy-clone.app",
        codex_home=root / ".codex-openrouter",
        bin_dir=root / "bin",
        support_root=root / "support",
        credential_helper=root / "bin/codex-openrouter-credential",
        desktop_launcher=root / "Desktop/Codex OpenRouter.app",
        shared_home=root / ".codex",
        state_dir=root / "state",
    )
