"""Worker health (Phase 7 Task 3): the state the banner, Today, Runs, doctor and
``salesfinder runs`` show, read from the latest ``runs`` row. Real DB, fixed clocks."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from salesfinder.config import AppConfig
from salesfinder.db.models import Run
from salesfinder.ops import health, runs
from salesfinder.ops.health import MISMATCH, NEVER, ago, worker_health
from salesfinder.ops.runs import LOCK_LOST_LINE, NO_SEND_LINE, PassResult

T0 = datetime(2026, 11, 14, 14, 0, tzinfo=UTC)
CONFIG = AppConfig()
STALE = CONFIG.ops.worker_stale_seconds
VERSION = "abc123def456"
WAITING = "Approved email, replies and discovery jobs wait until it's back."


async def _start(db: AsyncSession, now: datetime = T0, version: str = VERSION) -> Run:
    run = await runs.start_run(db, now=now, host="mac", pid=1, code_version=version)
    await db.commit()
    return run


async def _health(db: AsyncSession, now: datetime, version: str = VERSION) -> health.WorkerHealth:
    db.expire_all()
    return await worker_health(db, now=now, config=CONFIG, code_version=version)


async def test_never(db: AsyncSession) -> None:
    h = await _health(db, T0)
    assert h.state == "never" and h.problem == NEVER
    assert h.run_id is None and h.last_pass_at is None and h.last_beat_at is None and h.step is None
    assert NEVER == (
        "The worker hasn't run yet — start it (see Operations). Approved email, replies and "
        "discovery jobs wait until it runs."
    )


async def test_running_with_last_pass(db: AsyncSession) -> None:
    run = await _start(db)
    run_id = run.id
    await runs.record_pass(
        db, run.id, PassResult([], [], False), now=T0 + timedelta(minutes=1), config=CONFIG
    )
    await db.commit()
    h = await _health(db, T0 + timedelta(minutes=3))
    assert h.state == "running" and h.problem is None
    assert h.last_pass_at == T0 + timedelta(minutes=1)
    assert h.last_beat_at == T0 + timedelta(minutes=1)
    assert h.run_id == run_id and h.no_send is False
    assert health.running_line(h, now=T0 + timedelta(minutes=3)) == "Running — last pass 2 min ago"


async def test_running_before_its_first_pass(db: AsyncSession) -> None:
    await _start(db)
    h = await _health(db, T0 + timedelta(seconds=20))
    assert h.state == "running" and h.last_pass_at is None
    assert health.running_line(h, now=T0) == "Running — first pass under way"


async def test_stopped_cleanly(db: AsyncSession) -> None:
    run = await _start(db)
    await runs.finish_run(db, run.id, now=T0 + timedelta(minutes=5), reason="shutdown")
    await db.commit()
    h = await _health(db, T0 + timedelta(hours=3, minutes=5))
    assert h.state == "stopped"
    assert h.problem == f"The worker isn't running (stopped 3 h ago). {WAITING}"


async def test_single_pass_counts_as_stopped(db: AsyncSession) -> None:
    run = await _start(db)
    await runs.finish_run(db, run.id, now=T0, reason="once")
    await db.commit()
    assert (await _health(db, T0 + timedelta(seconds=5))).state == "stopped"


async def test_a_crashed_latest_row_is_stopped_with_its_own_copy(db: AsyncSession) -> None:
    # Only possible by hand (a start closes it and opens a new row in one transaction), but the
    # copy must still say what happened.
    db.add(
        Run(
            started_at=T0,
            last_beat_at=T0 + timedelta(minutes=2),
            finished_at=T0 + timedelta(minutes=2),
            end_reason="crashed",
            host="mac",
            pid=1,
            code_version=VERSION,
        )
    )
    await db.commit()
    h = await _health(db, T0 + timedelta(minutes=12))
    assert h.state == "stopped"
    assert h.problem == (
        f"The worker isn't running (it stopped without shutting down 10 min ago). {WAITING}"
    )


async def test_stale_mentions_the_step(db: AsyncSession) -> None:
    run = await _start(db)
    await runs.beat(db, run.id, now=T0, step="Discovery", step_started_at=T0)
    await db.commit()
    h = await _health(db, T0 + timedelta(seconds=STALE + 1))
    assert h.state == "stale" and h.step == "Discovery"
    assert h.problem == (f"The worker stopped responding 3 min ago (during Discovery). {WAITING}")


async def test_stale_between_steps_has_no_during(db: AsyncSession) -> None:
    await _start(db)
    h = await _health(db, T0 + timedelta(hours=1))
    assert h.state == "stale"
    assert h.problem == f"The worker stopped responding 60 min ago. {WAITING}"


async def test_stale_boundary(db: AsyncSession) -> None:
    await _start(db)
    assert (await _health(db, T0 + timedelta(seconds=STALE))).state == "running"
    assert (await _health(db, T0 + timedelta(seconds=STALE + 1))).state == "stale"


async def test_stale_follows_the_configured_seconds(db: AsyncSession) -> None:
    await _start(db)
    config = AppConfig.model_validate({"ops": {"beat_seconds": 30, "worker_stale_seconds": 600}})
    db.expire_all()
    h = await worker_health(
        db, now=T0 + timedelta(seconds=599), config=config, code_version=VERSION
    )
    assert h.state == "running"


async def test_mismatch_only_while_running(db: AsyncSession) -> None:
    run_id = (await _start(db, version="old000000000")).id
    h = await _health(db, T0 + timedelta(seconds=10))
    assert h.state == "mismatch" and h.problem == MISMATCH
    assert MISMATCH == (
        "The web app and the worker are running different versions — restart both together."
    )
    # Stale beats mismatch: the worker isn't working at all.
    assert (await _health(db, T0 + timedelta(seconds=STALE + 1))).state == "stale"
    # A stopped worker on old code is just stopped.
    await runs.finish_run(db, run_id, now=T0 + timedelta(seconds=20), reason="shutdown")
    await db.commit()
    assert (await _health(db, T0 + timedelta(seconds=30))).state == "stopped"


async def test_the_latest_run_wins(db: AsyncSession) -> None:
    old = await _start(db, T0 - timedelta(hours=1))
    await runs.finish_run(db, old.id, now=T0 - timedelta(minutes=30), reason="shutdown")
    await db.commit()
    await _start(db)
    assert (await _health(db, T0 + timedelta(seconds=5))).state == "running"


@pytest.mark.parametrize(
    ("delta", "text"),
    [
        (timedelta(seconds=-5), "less than a minute ago"),
        (timedelta(0), "less than a minute ago"),
        (timedelta(seconds=59), "less than a minute ago"),
        (timedelta(seconds=60), "1 min ago"),
        (timedelta(minutes=119, seconds=59), "119 min ago"),
        (timedelta(hours=2), "2 h ago"),
        (timedelta(hours=47, minutes=59), "47 h ago"),
        (timedelta(days=2), "2 days ago"),
        (timedelta(days=40), "40 days ago"),
    ],
)
def test_ago(delta: timedelta, text: str) -> None:
    assert ago(delta) == text


async def test_count_recent_failures_sums_failed_steps(db: AsyncSession) -> None:
    # An old run (beat over 24 h ago) doesn't count; failed_steps counts beyond the kept items.
    old = await _start(db, T0 - timedelta(hours=30))
    await runs.record_pass(
        db, old.id, PassResult(["x"], ["x"], False), now=T0 - timedelta(hours=30), config=CONFIG
    )
    await runs.finish_run(db, old.id, now=T0 - timedelta(hours=26), reason="shutdown")
    await db.commit()
    run = await _start(db, T0 - timedelta(minutes=5))
    many = [f"Mail step failed: E{i}" for i in range(60)]
    await runs.record_pass(db, run.id, PassResult(many, many, False), now=T0, config=CONFIG)
    await db.commit()
    assert await runs.count_recent_failures(db, now=T0) == 60
    assert await runs.count_recent_failures(db, now=T0 + timedelta(days=2)) == 0
    assert await runs.count_recent_failures(db, now=T0, hours=48) == 61


# --------------------------------------------------------------------------- final fix wave


async def test_a_no_send_worker_says_so(db: AsyncSession) -> None:
    """Final review M1 / Ruling P7-J: a leftover ``--no-send`` worker reads as running, but
    "Running with --no-send (nothing will be sent)", from its open row's lines."""
    run = await _start(db)
    run_id = run.id
    await runs.record_pass(
        db, run_id, PassResult(["Sent 0", NO_SEND_LINE], [], False), now=T0, config=CONFIG
    )
    await db.commit()
    h = await _health(db, T0 + timedelta(minutes=2))
    assert h.state == "running" and h.problem is None and h.no_send is True
    assert health.running_line(h, now=T0 + timedelta(minutes=2)) == (
        "Running with --no-send (nothing will be sent) — last pass 2 min ago"
    )
    # Its rolled-over row starts with the line (the worker carries it), so it never flickers.
    new_id = await runs.record_pass(
        db,
        run_id,
        PassResult([], [], False),
        now=T0 + timedelta(minutes=15),
        config=CONFIG,
        carry=[NO_SEND_LINE],
    )
    await db.commit()
    h = await _health(db, T0 + timedelta(minutes=15, seconds=5))
    assert h.run_id == new_id and h.no_send is True
    # Stopped: it says nothing about --no-send.
    await runs.finish_run(db, new_id, now=T0 + timedelta(minutes=16), reason="shutdown")
    await db.commit()
    h = await _health(db, T0 + timedelta(minutes=17))
    assert h.state == "stopped" and h.no_send is False


async def test_a_sending_worker_is_not_no_send(db: AsyncSession) -> None:
    run = await _start(db)
    await runs.record_pass(
        db, run.id, PassResult(["Sent 2 email(s)."], [], False), now=T0, config=CONFIG
    )
    await db.commit()
    h = await _health(db, T0 + timedelta(minutes=1))
    assert h.no_send is False
    assert health.running_line(h, now=T0 + timedelta(minutes=1)) == "Running — last pass 1 min ago"


async def test_a_lost_lock_stop_says_so(db: AsyncSession) -> None:
    """Final wave T3: a worker that stopped because it lost its database lock reads so."""
    run = await _start(db)
    await runs.finish_run(
        db, run.id, now=T0 + timedelta(minutes=1), reason="shutdown", line=LOCK_LOST_LINE
    )
    await db.commit()
    h = await _health(db, T0 + timedelta(minutes=3))
    assert h.state == "stopped"
    assert h.problem == (
        "The worker isn't running (it lost its database lock 2 min ago; launchd restarts it). "
        f"{WAITING}"
    )


async def test_health_prefers_the_open_row(db: AsyncSession) -> None:
    """Final wave T3: a finished row with a later start (the clock stepped back) never hides
    the open one."""
    later = await _start(db, T0 + timedelta(hours=1))
    await runs.finish_run(db, later.id, now=T0 + timedelta(hours=1), reason="once")
    await db.commit()
    current_id = (await _start(db, T0)).id
    h = await _health(db, T0 + timedelta(seconds=10))
    assert h.state == "running" and h.run_id == current_id


async def test_health_leaves_no_half_loaded_run_in_the_session(db: AsyncSession) -> None:
    """Final wave T3: the health read loads no ``Run`` into the session, so a later read of the
    same row (the Runs page) gets its lines and failures."""
    run = await _start(db)
    await runs.record_pass(db, run.id, PassResult(["a"], ["F"], False), now=T0, config=CONFIG)
    await db.commit()
    db.expunge_all()
    await worker_health(db, now=T0, config=CONFIG, code_version=VERSION)
    assert not any(isinstance(obj, Run) for obj in db.identity_map.values())
    [row] = await runs.list_runs(db, limit=5, offset=0)
    assert [line["text"] for line in row.lines] == ["a"]
    assert [item["text"] for item in row.failures] == ["F"]


async def test_run_label_marks_a_stale_open_row(db: AsyncSession) -> None:
    """Final wave T3: an open row with no beat is never shown as a green "Running"."""
    run = await _start(db)
    await db.commit()
    db.expire_all()
    [row] = await runs.list_runs(db, limit=5, offset=0)
    assert health.run_label(row, now=T0 + timedelta(seconds=10), config=CONFIG) == (
        "Running",
        "badge-ok",
    )
    assert health.run_label(row, now=T0 + timedelta(seconds=STALE + 1), config=CONFIG) == (
        "No beat",
        "badge-off",
    )
    await runs.record_pass(db, run.id, PassResult([NO_SEND_LINE], [], False), now=T0,
                           config=CONFIG)  # fmt: skip
    await db.commit()
    db.expire_all()
    [row] = await runs.list_runs(db, limit=5, offset=0)
    assert health.run_label(row, now=T0, config=CONFIG) == ("Running with --no-send", "badge-warn")
    await runs.finish_run(db, run.id, now=T0, reason="shutdown")
    await db.commit()
    db.expire_all()
    [row] = await runs.list_runs(db, limit=5, offset=0)
    assert health.run_label(row, now=T0, config=CONFIG) == ("Shut down", None)


async def test_the_health_query_reads_through_the_indexes(engine: AsyncEngine) -> None:
    """Final wave tail m1: on every full page and /healthz the health query picks its row by
    index (the open-row index, then the start-time index, then the primary key) and never
    scans or sorts the whole table. 3000 rows, analysed, all rolled back."""
    from sqlalchemy import event, text

    from salesfinder.ops.health import _LATEST

    def explain(*args: object) -> tuple[str, object]:
        statement, parameters = args[2], args[3]
        return f"EXPLAIN {statement}", parameters

    async with engine.connect() as conn:
        await conn.execute(
            text(
                "INSERT INTO runs (id, started_at, last_beat_at, finished_at, end_reason, host,"
                " pid, code_version) SELECT gen_random_uuid(), now() - g * interval '15 min',"
                " now(), now() - g * interval '15 min' + interval '15 min', 'window', 'mac', 1,"
                " 'v' FROM generate_series(1, 3000) AS g"
            )
        )
        await conn.execute(text("ANALYZE runs"))
        event.listen(engine.sync_engine, "before_cursor_execute", explain, retval=True)
        try:
            plan = "\n".join(row[0] for row in await conn.execute(_LATEST))
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", explain)
        await conn.rollback()
    assert "Seq Scan" not in plan, plan
    for index in ("pk_runs", "ix_runs_open", "ix_runs_started_at"):
        assert index in plan, plan
