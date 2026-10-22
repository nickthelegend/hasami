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


def _pins() -> dict[str, str]:
    path = ROOT / "constraints.txt"
    assert path.is_file(), "constraints.txt (the tested versions) is missing"
    pins = {}
    for line in path.read_text().splitlines():
        line = line.split("#")[0].strip()
        if line:
            name, _, ver = line.partition("==")
            assert ver, f"not an exact pin: {line!r}"
            pins[name.strip().lower().replace("_", "-")] = ver.strip()
    return pins


def test_every_dependency_is_pinned_to_the_tested_version():
    """pyproject says `>=`; the versions the suite passed on are pinned in constraints.txt. The code relies on
    version-specific APIs (mcp.server.mcpserver.MCPServer, transformers Sam2Model, Starlette's httpx2 client)."""
    pins = _pins()
    meta = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    direct = meta["dependencies"] + meta["optional-dependencies"]["dev"]
    for dep in direct:
        name = re.split(r"[<>=!~\[ ]", dep, maxsplit=1)[0].lower().replace("_", "-")
        assert name in pins, f"{name} is not pinned in constraints.txt"
    for name in ("starlette", "huggingface-hub", "tokenizers", "safetensors", "httpx2"):
        assert name in pins, f"{name} (an API the code or tests rely on) is not pinned"


def test_the_installed_versions_are_the_pinned_ones():
    """The environment this suite runs in was installed from constraints.txt (setup.sh and CI do so)."""
    from importlib.metadata import PackageNotFoundError, version

    pins = _pins()
    for name in ("mcp", "transformers", "torch", "mujoco", "fastapi", "starlette", "openai", "pydantic", "numpy"):
        try:
            got = version(name)
        except PackageNotFoundError:
            continue
        assert got.split("+")[0] == pins[name], f"{name} {got} installed, constraints.txt pins {pins[name]}"


def test_setup_ci_and_docs_install_with_the_constraints():
    for f in ("scripts/setup.sh", ".github/workflows/ci.yml"):
        text = (ROOT / f).read_text()
        assert "-c constraints.txt" in text, f"{f} does not install with -c constraints.txt"
    for f in ("README.md", "TESTING.md", "packages/landing/index.html"):
        fenced = False
        for line in (ROOT / f).read_text().splitlines():
            fenced ^= line.strip().startswith("```")
            command = fenced or "<pre>" in line  # a command block, not prose about an earlier install
            if command and re.search(r"pip install (--python \S+ )?-e ('|&#39;)\.\[dev\]", line):
                assert "-c constraints.txt" in line, (f, line.strip())


def test_sam_iou_claims_are_the_recorded_demo_runs_numbers():
    """The README, form and landing quote SAM 2.1's IoU on run 20261021T053112874Z. They used to say 'IoU 0.71-0.92
    per cube; mean 0.86' while the record's red cube is 0.9531 (the landing shows 0.953) and its mean of 0.86 also
    averages the three place masks. Every quoted range is now computed here from the record."""
    import json

    rec = json.loads((ROOT / "deployments/runs/20261021T053112874Z.json").read_text())
    pick = [s["pick"]["iou_vs_truth"] for s in rec["steps"]]
    place = [s["place"]["iou_vs_truth"] for s in rec["steps"]]
    assert sorted(pick + place) == sorted(rec["iou"]["values"])
    claim = (f"pick masks {min(pick):.2f}–{max(pick):.2f} (cubes), place masks {min(place):.2f}–{max(place):.2f}; "
             f"mean of all six {rec['iou']['mean']:.2f}")
    assert claim == "pick masks 0.71–0.95 (cubes), place masks 0.85–1.00; mean of all six 0.86"
    for f in ("README.md", "HACKATHON_SUBMISSION_FORM.md"):
        text = " ".join((ROOT / f).read_text().split())  # prose may wrap
        assert claim in text and "20261021T053112874Z" in text, f
        assert "0.71–0.92" not in text, f
    landing = (ROOT / "packages/landing/index.html").read_text()
    assert f"IoU {rec['steps'][0]['pick']['iou_vs_truth']:.3f}" in landing  # the red cube, step 1


def test_cited_contract_documents_exist_in_this_repo():
    """The server, the dashboard client and the server tests cited 'specs/hasami.md (HTTP contract)', a file that
    is not in this repo; a reader following it found nothing. Every docs path a source file cites must exist here."""
    cited = []
    for folder in ("packages", "tests", "scripts", "e2e/tests"):
        for p in (ROOT / folder).rglob("*"):
            if (p.suffix not in (".py", ".ts", ".tsx", ".mjs", ".sh") or "node_modules" in p.parts or "dist" in p.parts
                    or p == Path(__file__).resolve()):  # this test quotes the old reference
                continue
            for m in re.finditer(r"\b((?:specs|docs|packages/docs)/[\w./-]+\.md)\b", p.read_text(errors="ignore")):
                cited.append((p.relative_to(ROOT).as_posix(), m.group(1)))
    missing = [(f, ref) for f, ref in cited if not (ROOT / ref).is_file()]
    assert not missing, missing
    contract = [ROOT / f for f in ("packages/server/hasami_server/app.py", "packages/dashboard/src/api.ts",
                                   "tests/test_server.py")]
    readme = (ROOT / "README.md").read_text()
    assert "## HTTP API (port 8420)" in readme
    for f in contract:
        assert 'README.md "HTTP API' in f.read_text()[:400], f
