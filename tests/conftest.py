from __future__ import annotations

import pytest

from ci_triage.budget import Ledger
from ci_triage.schema import create_all, create_engine_for


@pytest.fixture()
def engine(tmp_path):
    """A file-backed SQLite engine.

    Deliberately a file rather than ``:memory:`` -- an in-memory SQLite database
    is per-connection, so a pooled engine would hand each thread its own empty
    database and the concurrency tests would pass without proving anything.
    """
    url = f"sqlite:///{tmp_path / 'test.db'}"
    eng = create_engine_for(url)
    create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def ledger(engine):
    return Ledger(engine)
