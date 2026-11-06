"""Run bench.yml's own detect step against hostile and valid directories.

Usage: python3 test/bench/detect_guard.py <detect.sh>

<detect.sh> is the detect step's `run` script, extracted from
.github/workflows/bench.yml by the self-test, so this tests the shipped code.
A working-directory with a line break must fail before anything reaches
$GITHUB_OUTPUT: a line break there would add records that overwrite the
detected outputs (for example cargo=false) and skip the benchmark jobs.
Valid directories are the control: their outputs must be exact.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

HOSTILE = [
    "project\ncargo=false\npython=\nignored=value",
    "project\rcargo=false",
    "trailing\n",
]


def run(script: Path, root: Path, workdir: str, files: list[str]) -> tuple[int, str, str]:
    directory = root / workdir
    directory.mkdir(parents=True)
    for name in files:
        (directory / name).touch()
    output = root / f"output-{len(list(root.iterdir()))}"
    output.touch()
    proc = subprocess.run(
        ["bash", str(script)],
        cwd=directory,
        env={"PATH": "/usr/bin:/bin", "MODE": "simulation", "WORKDIR": workdir, "GITHUB_OUTPUT": str(output)},
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode, proc.stdout + proc.stderr, output.read_text()


def main() -> int:
    script = Path(sys.argv[1])
    failures: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for i, workdir in enumerate(HOSTILE):
            code, log, written = run(script, root / f"hostile-{i}", workdir, ["Cargo.toml", "pyproject.toml", "uv.lock"])
            if code == 0:
                failures.append(f"{workdir!r}: accepted (exit 0)")
            if "must not contain a line break" not in log:
                failures.append(f"{workdir!r}: no line-break error in the log: {log!r}")
            if written:
                failures.append(f"{workdir!r}: wrote to GITHUB_OUTPUT: {written!r}")
        controls = {
            "rust": (["Cargo.toml"], "cargo=true\npython=\npython-version-file=\nmode=simulation\n"),
            "py": (
                ["pyproject.toml", "uv.lock"],
                "cargo=false\npython=uv\npython-version-file=py/pyproject.toml\nmode=simulation\n",
            ),
        }
        for workdir, (files, expected) in controls.items():
            code, log, written = run(script, root / "valid", workdir, files)
            if code != 0 or written != expected:
                failures.append(f"{workdir!r}: exit {code}, outputs {written!r}, expected {expected!r}; log {log!r}")
    for failure in failures:
        print(f"::error::{failure}")
    print(f"{len(HOSTILE)} hostile directories rejected, {len(controls)} valid directories exact" if not failures else "")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
