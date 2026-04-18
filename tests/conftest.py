"""Shared pytest fixtures."""
from __future__ import annotations

from collections import deque
from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    return FIXTURES_DIR


@pytest.fixture
def empty_history() -> deque:
    return deque()


def _history_from_list(ticks: list[tuple[float, float]]) -> deque:
    """Build a (ts, price) deque matching the shape used by run_sniper.py."""
    d: deque = deque()
    for ts, p in ticks:
        d.append((float(ts), float(p)))
    return d


@pytest.fixture
def history_from_list():
    return _history_from_list
