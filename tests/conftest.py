"""Shared fixtures. Everything is real: MuJoCo physics, SAM 2.1 weights, FastAPI, an MCP subprocess.

SAM 2.1 loads once per test session (the `session` fixture keeps one Session for every test that
needs perception). Set HF_HOME to where the weights are cached before running.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("HF_HOME", "/Volumes/Extreme SSD/.cache/huggingface")


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
