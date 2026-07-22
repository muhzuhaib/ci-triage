from __future__ import annotations

import os

import pytest
from sqlalchemy import delete

from ci_triage.budget import Ledger
from ci_triage.schema import create_all, idempotency_keys, reservations, run_budgets
from ci_triage.schema import create_engine_for

#: Point this at a Postgres instance to run the whole suite against it.
#: CI does exactly that, because the concurrency guarantees are backend
#: behaviour and SQLite alone would not prove them -- it serialises writers,
#: which can hide a race that Postgres would expose.
TEST_DATABASE_URL = os.environ.get("CI_TRIAGE_TEST_DATABASE_URL")


@pytest.fixture()
def engine(tmp_path):
    """A database engine, SQLite by default and Postgres when configured.

    The SQLite database is file-backed rather than ``:memory:`` on purpose: an
    in-memory SQLite database is per-connection, so a pooled engine would hand
    each thread its own empty database and the concurrency tests would pass
    without proving anything.
    """
    if TEST_DATABASE_URL:
        eng = create_engine_for(TEST_DATABASE_URL)
        create_all(eng)
        # Shared database, so clear it rather than recreating it per test.
        with eng.begin() as conn:
            for table in (idempotency_keys, reservations, run_budgets):
                conn.execute(delete(table))
    else:
        eng = create_engine_for(f"sqlite:///{tmp_path / 'test.db'}")
        create_all(eng)

    yield eng
    eng.dispose()


@pytest.fixture()
def ledger(engine):
    return Ledger(engine)
