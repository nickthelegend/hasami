"""Shared fixtures. Everything is real: MuJoCo physics, SAM 2.1 weights, FastAPI, an MCP subprocess.

SAM 2.1 loads once per test session (the `session` fixture keeps one Session for every test that
needs perception). Set HF_HOME to where the weights are cached before running (first use downloads them).
"""

from __future__ import annotations

import os

import pytest

# SAM 2.1's weights (~150 MB) come from the Hugging Face cache: HF_HOME if set, else the author's external
# cache when it exists (so a run there does not download a second copy), else the default ~/.cache.
_LOCAL_CACHE = "/Volumes/Extreme SSD/.cache/huggingface"
if "HF_HOME" not in os.environ and os.path.isdir(_LOCAL_CACHE):
    os.environ["HF_HOME"] = _LOCAL_CACHE


@pytest.fixture(scope="session")
def session():
    from hasami_core import Session

    s = Session(seed=1)
    _ = s.sam  # load SAM 2.1 once for the whole test run
    return s


@pytest.fixture()
def no_model_key(monkeypatch):
    monkeypatch.delenv("MODEL_API_KEY", raising=False)


@pytest.fixture()
def runs_tmp(tmp_path, monkeypatch):
    d = tmp_path / "runs"
    monkeypatch.setenv("HASAMI_RUNS_DIR", str(d))
    return d
