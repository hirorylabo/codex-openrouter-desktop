from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class CandidatePatcherTests(unittest.TestCase):
    def test_broken_asar_is_rejected_before_upstream_load(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            candidate_root = Path(temporary)
            app = candidate_root / "Candidate.app"
            asar = app / "Contents/Resources/app.asar"
            asar.parent.mkdir(parents=True)
            asar.write_bytes(b"not-an-asar")
            config = candidate_root / "providers.json"
            config.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "default_provider": "openrouter",
                        "model_providers": {"minimax/minimax-m3": "openrouter"},
                    }
                ),
                encoding="utf-8",
            )
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "portable/patcher/patch_candidate.py"),
                    "--app",
                    str(app),
                    "--candidate-root",
                    str(candidate_root),
                    "--config",
                    str(config),
                    "--backup-dir",
                    str(candidate_root / "backup"),
                    "--upstream",
                    str(candidate_root / "missing-upstream.py"),
                    "--transform",
                    str(candidate_root / "missing-transform.mjs"),
                    "--node",
                    Path(sys.executable).as_posix(),
                    "--stock-hash",
                    "0" * 64,
                    "--version",
                    "test",
                    "--build",
                    "test",
                    "--adapter-output",
                    str(candidate_root / "adapter.json"),
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            self.assertNotEqual(0, result.returncode)
            self.assertIn("not an exact copy", result.stdout)


if __name__ == "__main__":
    unittest.main()
