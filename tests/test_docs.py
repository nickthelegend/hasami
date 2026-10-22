"""The documented install works on a fresh clone: pytest lives in the [dev] extra, so every install line that
leads to `python -m pytest` installs '.[dev]', and scripts/setup.sh does the whole setup in one command."""

import os
import re
import subprocess
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_pytest_is_in_the_dev_extra_only():
    meta = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert not any(d.startswith("pytest") for d in meta["dependencies"])
    assert any(d.startswith("pytest") for d in meta["optional-dependencies"]["dev"])


def test_every_documented_install_line_installs_the_dev_extra():
    """README 'Run it', TESTING 'Command' and the landing's quickstart (the dashboard's offline card runs setup.sh)."""
    files = ["README.md", "TESTING.md", "packages/landing/index.html", "packages/dashboard/src/App.tsx"]
    lines = []
    for f in files:
        for n, line in enumerate((ROOT / f).read_text().splitlines(), 1):
            if re.search(r"pip install (--python \S+ )?-e ", line):
                lines.append((f, n, line))
    assert len(lines) >= 3, lines
    bad = [(f, n, line.strip()) for f, n, line in lines if "-e '.[dev]'" not in line and "-e &#39;.[dev]&#39;" not in line]
    assert not bad, f"install lines without '.[dev]' (pytest would be missing on a fresh clone): {bad}"


def test_setup_script_is_one_command_and_installs_the_dev_extra():
    script = ROOT / "scripts" / "setup.sh"
    assert script.is_file() and os.access(script, os.X_OK)
    text = script.read_text()
    assert "-e '.[dev]'" in text and "pnpm build" in text and "sam2.1-hiera-tiny" in text
    out = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    helped = subprocess.run([str(script), "--help"], capture_output=True, text=True, timeout=30)
    assert helped.returncode == 0 and "--no-weights" in helped.stdout
    assert "scripts/setup.sh" in (ROOT / "README.md").read_text()
    assert "scripts/setup.sh" in (ROOT / "TESTING.md").read_text()


def test_ci_runs_pytest_and_the_dashboard():
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    assert "-e '.[dev]'" in ci and "python -m pytest" in ci and "pnpm build" in ci
    for f in re.findall(r"tests/test_\w+\.py", ci):
        assert (ROOT / f).is_file(), f
