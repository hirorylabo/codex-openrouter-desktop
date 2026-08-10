from __future__ import annotations

import argparse
from pathlib import Path
import subprocess


class ProcessError(RuntimeError):
    pass


def matching_processes(process_table: str, executable: Path) -> list[tuple[int, str]]:
    prefix = str(executable)
    matches: list[tuple[int, str]] = []
    for line in process_table.splitlines():
        fields = line.strip().split(None, 1)
        if len(fields) != 2 or not fields[0].isdigit():
            continue
        command = fields[1]
        if command == prefix or command.startswith(prefix + " "):
            matches.append((int(fields[0]), command))
    return matches


def process_pids(executable: Path) -> list[int]:
    result = subprocess.run(
        ["/bin/ps", "-axo", "pid=,command="],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        raise ProcessError("process listを取得できません")
    return [pid for pid, _command in matching_processes(result.stdout, executable)]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--executable", type=Path, required=True)
    args = parser.parse_args()
    for pid in process_pids(args.executable):
        print(pid)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
