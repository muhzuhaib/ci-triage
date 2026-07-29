"""Database schema and engine construction.

Deliberately SQLAlchemy Core rather than the ORM: this schema is four tables
that are only ever touched by a handful of hand-written statements whose exact
SQL matters (see :mod:`ci_triage.budget`). An ORM session's unit-of-work would
sit between those statements and the database and obscure the one thing that
has to be true -- that the reservation update is a single atomic statement.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    Engine,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    event,
)

metadata = MetaData()

#: One row per logical run. ``ceiling_micros`` is the hard per-run spend limit;
#: the invariant the ledger enforces is
#: ``spent_micros + reserved_micros <= ceiling_micros``.
run_budgets = Table(
    "run_budgets",
    metadata,
    Column("run_id", String(128), primary_key=True),
    Column("ceiling_micros", BigInteger, nullable=False),
    Column("spent_micros", BigInteger, nullable=False, default=0),
    Column("reserved_micros", BigInteger, nullable=False, default=0),
    Column("overrun_micros", BigInteger, nullable=False, default=0),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

#: One row per attempted charge. A reservation is held while a provider call is
#: in flight, then either committed with the real cost or released.
reservations = Table(
    "reservations",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("run_id", String(128), nullable=False),
    Column("held_micros", BigInteger, nullable=False),
    Column("actual_micros", BigInteger, nullable=True),
    Column("state", String(16), nullable=False),
    Column("attempt", Integer, nullable=False, default=1),
    Column("purpose", String(64), nullable=False, default=""),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("settled_at", DateTime(timezone=True), nullable=True),
)

Index("ix_reservations_run_id", reservations.c.run_id)

#: Idempotency records. The primary key *is* the guarantee: a second attempt to
#: claim the same key violates the constraint and is rejected by the database
#: rather than by application logic.
idempotency_keys = Table(
    "idempotency_keys",
    metadata,
    Column("key", String(255), primary_key=True),
    Column("run_id", String(128), nullable=False),
    Column("state", String(16), nullable=False),
    Column("result", Text, nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("completed_at", DateTime(timezone=True), nullable=True),
)


#: One row per triage job: the unit the state machine retries, buries and
#: replays. ``idempotency_key`` is unique, so the job cannot be created twice for
#: one event however many times a delivery is retried -- the same constraint
#: argument as ``idempotency_keys`` itself. ``attempt`` doubles as a fencing
#: token: it is incremented by the claim, so a worker whose lease expired can be
#: told its write is stale. See :mod:`ci_triage.runs`.
triage_jobs = Table(
    "triage_jobs",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("idempotency_key", String(255), nullable=False, unique=True),
    Column("run_id", String(128), nullable=False),
    Column("state", String(16), nullable=False),
    Column("attempt", Integer, nullable=False, default=0),
    Column("max_attempts", Integer, nullable=False),
    Column("next_attempt_at", DateTime(timezone=True), nullable=False),
    Column("lease_expires_at", DateTime(timezone=True), nullable=True),
    Column("worker", String(64), nullable=True),
    Column("last_error", Text, nullable=True),
    Column("result", Text, nullable=True),
    Column("replays", Integer, nullable=False, default=0),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

#: The claim query asks for runnable work: one state, ordered by due time. Both
#: columns are in the index because the ordering is part of the query, not a
#: presentation detail -- an index on ``state`` alone would leave a sort behind.
Index("ix_triage_jobs_runnable", triage_jobs.c.state, triage_jobs.c.next_attempt_at)


def create_engine_for(url: str, **kwargs: object) -> Engine:
    """Build an engine with the settings each backend needs to be correct.

    SQLite needs two adjustments before it can be trusted under concurrency,
    and both are easy to omit and hard to notice:

    * ``BEGIN IMMEDIATE`` -- SQLAlchemy's default transaction is *deferred*, so
      a transaction that reads and then writes takes its write lock late. Two
      such transactions can both read, then one fails to upgrade its lock and
      raises "database is locked" immediately rather than waiting. Taking the
      write lock up front makes writers queue instead of collide.
    * ``busy_timeout`` -- without it, a writer that finds the lock held gives up
      at once instead of waiting for it.

    Postgres needs neither; its row-level locking handles this natively.
    """
    is_sqlite = url.startswith("sqlite")
    if is_sqlite:
        kwargs.setdefault("connect_args", {"check_same_thread": False})

    engine = create_engine(url, **kwargs)  # type: ignore[arg-type]

    if is_sqlite:

        @event.listens_for(engine, "connect")
        def _configure(dbapi_conn, _record):  # type: ignore[no-untyped-def]
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA busy_timeout=5000")
            cur.close()

        @event.listens_for(engine, "begin")
        def _begin_immediate(conn):  # type: ignore[no-untyped-def]
            conn.exec_driver_sql("BEGIN IMMEDIATE")

    return engine


def create_all(engine: Engine) -> None:
    metadata.create_all(engine)
