"""The heartbeat log (Phase 7 Task 1): ``runs`` rows opened, beaten, recorded into, rolled over
per window, closed (``crashed`` when a worker died without shutting down) and pruned. Real DB,
fixed clocks."""

import re
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from salesfinder.config import AppConfig
from salesfinder.db.engine import make_session_factory
from salesfinder.db.models import Run, UsageEvent
from salesfinder.ops import runs
from salesfinder.ops.runs import CRASHED_LINE, MAX_FAILURES, MAX_LINE_CHARS, PassResult
from salesfinder.ops.version import code_version, fingerprint

T0 = datetime(2026, 11, 14, 14, 0, tzinfo=UTC)
CONFIG = AppConfig()
HOST, PID, VERSION = "office-mac", 4242, "abc123def456"


async def _start(db: AsyncSession, now: datetime = T0) -> Run:
    return await runs.start_run(db, now=now, host=HOST, pid=PID, code_version=VERSION)


async def _get(db: AsyncSession, run_id: uuid.UUID) -> Run:
    db.expire_all()
    return (await db.execute(select(Run).where(Run.id == run_id))).scalar_one()


def _result(
    lines: list[str] | None = None, failures: list[str] | None = None, *, quiet: bool = False
) -> PassResult:
    return PassResult(lines=lines or [], failures=failures or [], quiet=quiet)


async def test_start_run_inserts_an_open_row(db: AsyncSession) -> None:
    run = await _start(db)
    await db.commit()
    row = await _get(db, run.id)
    assert (row.started_at, row.last_beat_at) == (T0, T0)
    assert row.last_pass_at is None and row.finished_at is None and row.end_reason is None
    assert (row.passes, row.failed_steps, row.lines_dropped) == (0, 0, 0)
    assert (row.lines, row.failures, row.quiet) == ([], [], False)
    assert (row.host, row.pid, row.code_version) == (HOST, PID, VERSION)
    assert row.step is None and row.step_started_at is None


async def test_start_run_closes_an_older_open_run_as_crashed(db: AsyncSession) -> None:
    old = await _start(db, T0 - timedelta(hours=1))
    await runs.beat(
        db,
        old.id,
        now=T0 - timedelta(minutes=40),
        step="Discovery",
        step_started_at=T0 - timedelta(minutes=41),
    )
    await db.commit()

    old_id, new_id = old.id, (await _start(db)).id
    await db.commit()

    old_row = await _get(db, old_id)
    assert old_row.end_reason == "crashed"
    assert old_row.finished_at == old_row.last_beat_at == T0 - timedelta(minutes=40)
    assert old_row.lines[-1]["text"] == CRASHED_LINE == "The worker stopped without shutting down."
    assert old_row.step == "Discovery"  # where it was when it stopped
    new_row = await _get(db, new_id)
    assert new_row.finished_at is None and new_id != old_id


async def test_start_run_leaves_finished_runs_alone(db: AsyncSession) -> None:
    old = await _start(db, T0 - timedelta(hours=1))
    await runs.finish_run(db, old.id, now=T0 - timedelta(minutes=30), reason="shutdown")
    await db.commit()
    await _start(db)
    await db.commit()
    row = await _get(db, old.id)
    assert row.end_reason == "shutdown" and row.lines == []


async def test_record_pass_appends_and_counts(db: AsyncSession) -> None:
    run = await _start(db)
    first = T0 + timedelta(seconds=10)
    same = await runs.record_pass(
        db,
        run.id,
        _result(
            ["Sent 1 email(s).", "Profiling step failed: RuntimeError"],
            ["Profiling step failed: RuntimeError"],
        ),
        now=first,
        config=CONFIG,
    )
    second = T0 + timedelta(seconds=20)
    same2 = await runs.record_pass(
        db, run.id, _result(["Queued 2 follow-up(s)."], quiet=True), now=second, config=CONFIG
    )
    await db.commit()
    assert same == same2 == run.id
    row = await _get(db, run.id)
    assert row.passes == 2 and row.failed_steps == 1
    assert row.last_pass_at == row.last_beat_at == second
    assert row.lines == [
        {"at": first.isoformat(), "text": "Sent 1 email(s)."},
        {"at": first.isoformat(), "text": "Profiling step failed: RuntimeError"},
        {"at": second.isoformat(), "text": "Queued 2 follow-up(s)."},
    ]
    assert row.failures == [
        {"at": first.isoformat(), "text": "Profiling step failed: RuntimeError"}
    ]
    assert row.quiet is True
    assert row.finished_at is None


async def test_quiet_sticks_once_set(db: AsyncSession) -> None:
    run = await _start(db)
    await runs.record_pass(
        db, run.id, _result(quiet=True), now=T0 + timedelta(seconds=10), config=CONFIG
    )
    await runs.record_pass(
        db, run.id, _result(quiet=False), now=T0 + timedelta(seconds=20), config=CONFIG
    )
    assert (await _get(db, run.id)).quiet is True


async def test_record_pass_caps_lines(db: AsyncSession) -> None:
    config = AppConfig.model_validate({"ops": {"max_run_lines": 200}})
    run = await _start(db)
    lines = [f"Line {i}." for i in range(250)]
    await runs.record_pass(
        db, run.id, _result(lines), now=T0 + timedelta(seconds=10), config=config
    )
    row = await _get(db, run.id)
    assert len(row.lines) == 200 and row.lines_dropped == 50
    assert row.lines[-1]["text"] == "Line 199."
    # A later pass only counts.
    await runs.record_pass(
        db, run.id, _result(["More."]), now=T0 + timedelta(seconds=20), config=config
    )
    row = await _get(db, run.id)
    assert len(row.lines) == 200 and row.lines_dropped == 51


async def test_record_pass_caps_failures_but_counts_them_all(db: AsyncSession) -> None:
    run = await _start(db)
    fails = [f"Step {i} step failed: RuntimeError" for i in range(MAX_FAILURES + 5)]
    await runs.record_pass(
        db, run.id, _result(fails, fails), now=T0 + timedelta(seconds=10), config=CONFIG
    )
    row = await _get(db, run.id)
    assert MAX_FAILURES == 50
    assert len(row.failures) == 50 and row.failed_steps == 55
    # The newest 50 are kept, also across passes.
    assert row.failures[0]["text"] == "Step 5 step failed: RuntimeError"
    assert row.failures[-1]["text"] == "Step 54 step failed: RuntimeError"
    await runs.record_pass(
        db,
        run.id,
        _result([], ["Last step failed: OSError"]),
        now=T0 + timedelta(seconds=20),
        config=CONFIG,
    )
    row = await _get(db, run.id)
    assert len(row.failures) == 50 and row.failed_steps == 56
    assert row.failures[0]["text"] == "Step 6 step failed: RuntimeError"
    assert row.failures[-1]["text"] == "Last step failed: OSError"


async def test_long_lines_are_cut(db: AsyncSession) -> None:
    run = await _start(db)
    long = "x" * (MAX_LINE_CHARS + 100)
    await runs.record_pass(
        db, run.id, _result([long], [long]), now=T0 + timedelta(seconds=10), config=CONFIG
    )
    row = await _get(db, run.id)
    assert MAX_LINE_CHARS == 500
    assert row.lines[0]["text"] == "x" * 500 and row.failures[0]["text"] == "x" * 500


async def test_rollover_at_exactly_the_window(db: AsyncSession) -> None:
    run = await _start(db)
    await runs.record_pass(
        db, run.id, _result(["a"]), now=T0 + timedelta(minutes=14, seconds=59), config=CONFIG
    )
    at = T0 + timedelta(minutes=15)
    new_id = await runs.record_pass(db, run.id, _result(["b"]), now=at, config=CONFIG)
    await db.commit()
    assert new_id != run.id
    old = await _get(db, run.id)
    assert old.end_reason == "window" and old.finished_at == at
    assert old.passes == 2 and [line["text"] for line in old.lines] == ["a", "b"]
    new = await _get(db, new_id)
    assert new.finished_at is None and new.started_at == new.last_beat_at == at
    # The pass that triggered the rollover counts as the new row's last pass, so a fresh row
    # never reads as "no pass yet".
    assert new.last_pass_at == at
    assert new.passes == 0 and new.lines == []
    assert (new.host, new.pid, new.code_version) == (HOST, PID, VERSION)


async def test_rollover_honours_the_configured_window(db: AsyncSession) -> None:
    config = AppConfig.model_validate({"usage": {"heartbeat_interval_minutes": 5}})
    run = await _start(db)
    new_id = await runs.record_pass(
        db, run.id, _result(), now=T0 + timedelta(minutes=5), config=config
    )
    assert new_id != run.id


async def test_rollover_prunes_old_finished_runs_but_keeps_open_ones(db: AsyncSession) -> None:
    """Only one run is ever open (``uq_runs_one_open``), so the 31-day-old open run is the
    current one: a worker whose Mac slept mid-window for a month. The rollover prunes before
    it closes that run, so the run (and the pass just recorded) survives; the 31-day-old
    finished run goes."""
    long_ago = T0 - timedelta(days=31)
    finished_old = await _start(db, long_ago - timedelta(minutes=30))
    await runs.finish_run(
        db, finished_old.id, now=long_ago - timedelta(minutes=15), reason="window"
    )
    recent_finished = await _start(db, T0 - timedelta(days=29))
    await runs.finish_run(db, recent_finished.id, now=T0 - timedelta(days=29), reason="shutdown")
    current = await _start(db, long_ago)
    await db.commit()
    finished_old_id, recent_id, current_id = finished_old.id, recent_finished.id, current.id

    new_id = await runs.record_pass(db, current_id, _result(["a"]), now=T0, config=CONFIG)
    await db.commit()

    ids = set((await db.execute(select(Run.id))).scalars())
    assert finished_old_id not in ids
    assert {recent_id, current_id, new_id} == ids
    old = await _get(db, current_id)
    assert old.end_reason == "window" and old.passes == 1


async def test_prune_runs_keeps_an_old_open_run(db: AsyncSession) -> None:
    run = await _start(db, T0 - timedelta(days=40))
    run_id = run.id
    assert await runs.prune_runs(db, now=T0, keep_days=30) == 0
    assert (await _get(db, run_id)).finished_at is None


async def test_at_most_one_open_run(db: AsyncSession) -> None:
    await _start(db)
    await db.commit()
    db.add(Run(started_at=T0, last_beat_at=T0, host=HOST, pid=1, code_version=VERSION))
    with pytest.raises(IntegrityError):
        await db.commit()


async def test_start_run_without_crash_close(db: AsyncSession) -> None:
    """For Task 2: a live worker whose own run was closed under it reopens a row without
    crash-closing anything; if another worker's run is open, the unique index refuses it."""
    first = await _start(db)
    await runs.finish_run(db, first.id, now=T0 + timedelta(minutes=1), reason="shutdown")
    reopened = await runs.start_run(
        db,
        now=T0 + timedelta(minutes=2),
        host=HOST,
        pid=PID,
        code_version=VERSION,
        close_crashed=False,
    )
    await db.commit()
    reopened_id = reopened.id
    assert (await _get(db, reopened_id)).finished_at is None
    with pytest.raises(IntegrityError):
        await runs.start_run(
            db,
            now=T0 + timedelta(minutes=3),
            host=HOST,
            pid=PID,
            code_version=VERSION,
            close_crashed=False,
        )
    await db.rollback()
    assert (await _get(db, reopened_id)).finished_at is None  # left alone


async def test_record_pass_reads_the_row_it_locks(engine: AsyncEngine, db: AsyncSession) -> None:
    """Fix I2: a session that already holds the run in its identity map must re-read it under
    FOR UPDATE, or another session's committed pass would be overwritten."""
    factory = make_session_factory(engine)
    async with factory() as a, factory() as b:
        run = await _start(a)
        await a.commit()
        run_id = run.id
        assert run.lines == []  # loaded in A's identity map
        await runs.record_pass(
            b, run_id, _result(["from B"]), now=T0 + timedelta(seconds=10), config=CONFIG
        )
        await b.commit()
        await runs.record_pass(
            a, run_id, _result(["from A"]), now=T0 + timedelta(seconds=20), config=CONFIG
        )
        await a.commit()
    row = await _get(db, run_id)
    assert row.passes == 2
    assert [line["text"] for line in row.lines] == ["from B", "from A"]


async def test_close_crashed_reads_the_row_it_locks(engine: AsyncEngine, db: AsyncSession) -> None:
    factory = make_session_factory(engine)
    async with factory() as a, factory() as b:
        run = await _start(a)
        await a.commit()
        run_id = run.id
        await runs.record_pass(
            b, run_id, _result(["from B"]), now=T0 + timedelta(seconds=10), config=CONFIG
        )
        await b.commit()
        await _start(a, T0 + timedelta(minutes=5))
        await a.commit()
    row = await _get(db, run_id)
    assert row.end_reason == "crashed"
    assert row.finished_at == T0 + timedelta(seconds=10)  # B's beat, not A's stale one
    assert [line["text"] for line in row.lines] == ["from B", CRASHED_LINE]


async def test_prune_runs_counts(db: AsyncSession) -> None:
    for days in (40, 35, 10):
        run = await _start(db, T0 - timedelta(days=days))
        await runs.finish_run(db, run.id, now=T0 - timedelta(days=days), reason="once")
    assert await runs.prune_runs(db, now=T0, keep_days=30) == 2
    assert await runs.prune_runs(db, now=T0, keep_days=30) == 0


async def test_record_pass_on_missing_or_finished_run_raises(db: AsyncSession) -> None:
    with pytest.raises(runs.RunNotOpen):
        await runs.record_pass(db, uuid.uuid4(), _result(), now=T0, config=CONFIG)
    run = await _start(db)
    await runs.finish_run(db, run.id, now=T0, reason="shutdown")
    with pytest.raises(runs.RunNotOpen):
        await runs.record_pass(db, run.id, _result(), now=T0, config=CONFIG)


async def test_beat_updates_an_open_run(db: AsyncSession) -> None:
    run = await _start(db)
    at = T0 + timedelta(seconds=30)
    await runs.beat(db, run.id, now=at, step="Discovery", step_started_at=T0 + timedelta(seconds=5))
    await db.commit()
    row = await _get(db, run.id)
    assert row.last_beat_at == at
    assert (row.step, row.step_started_at) == ("Discovery", T0 + timedelta(seconds=5))
    await runs.beat(db, run.id, now=at + timedelta(seconds=30), step=None, step_started_at=None)
    row = await _get(db, run.id)
    assert row.step is None and row.step_started_at is None


async def test_beat_does_not_touch_a_finished_run(db: AsyncSession) -> None:
    run = await _start(db)
    await runs.finish_run(db, run.id, now=T0 + timedelta(minutes=1), reason="shutdown")
    await runs.beat(db, run.id, now=T0 + timedelta(minutes=5), step="Sending", step_started_at=T0)
    await db.commit()
    row = await _get(db, run.id)
    assert row.last_beat_at == T0 + timedelta(minutes=1)
    assert row.step is None and row.end_reason == "shutdown"


async def test_finish_run_sets_reason_once(db: AsyncSession) -> None:
    run = await _start(db)
    await runs.beat(db, run.id, now=T0 + timedelta(seconds=30), step="Writing", step_started_at=T0)
    await runs.finish_run(db, run.id, now=T0 + timedelta(minutes=2), reason="once")
    await runs.finish_run(db, run.id, now=T0 + timedelta(minutes=9), reason="shutdown")
    row = await _get(db, run.id)
    assert row.end_reason == "once" and row.finished_at == T0 + timedelta(minutes=2)
    assert row.last_beat_at == T0 + timedelta(minutes=2)
    assert row.step is None and row.step_started_at is None


async def test_latest_and_list_runs_newest_first(db: AsyncSession) -> None:
    assert await runs.latest_run(db) is None
    made = []
    for minutes in (0, 15, 30):
        run = await _start(db, T0 + timedelta(minutes=minutes))
        await runs.finish_run(db, run.id, now=T0 + timedelta(minutes=minutes + 15), reason="window")
        made.append(run.id)
    latest = await runs.latest_run(db)
    assert latest is not None and latest.id == made[-1]
    assert [r.id for r in await runs.list_runs(db, limit=2, offset=0)] == [made[2], made[1]]
    assert [r.id for r in await runs.list_runs(db, limit=2, offset=2)] == [made[0]]
    assert await runs.list_runs(db, limit=-3, offset=0) == []
    assert [r.id for r in await runs.list_runs(db, limit=10**9, offset=-4)] == made[::-1]


async def test_recent_failures_order_and_limit(db: AsyncSession) -> None:
    now = T0
    # Too old: its run's last beat is more than 24 h ago.
    old = await _start(db, now - timedelta(hours=26))
    await runs.record_pass(
        db,
        old.id,
        _result(["X"], ["Old step failed: OSError"]),
        now=now - timedelta(hours=25, minutes=50),
        config=CONFIG,
    )
    await runs.finish_run(db, old.id, now=now - timedelta(hours=25, minutes=45), reason="window")
    run = await _start(db, now - timedelta(minutes=14))
    for i in range(7):
        at = now - timedelta(minutes=13 - i)
        await runs.record_pass(
            db, run.id, _result([], [f"Step{i} step failed: RuntimeError"]), now=at, config=CONFIG
        )
    await db.commit()

    got = await runs.recent_failures(db, now=now)
    assert [text for _, text in got] == [
        f"Step{i} step failed: RuntimeError" for i in (6, 5, 4, 3, 2)
    ]
    assert got[0][0] == now - timedelta(minutes=7)
    assert all(at.tzinfo is not None for at, _ in got)
    assert len(await runs.recent_failures(db, now=now, limit=20)) == 7
    assert len(await runs.recent_failures(db, now=now, hours=30, limit=20)) == 8
    assert await runs.recent_failures(db, now=now + timedelta(days=2)) == []
    assert await runs.recent_failures(db, now=now, limit=-1) == []
    assert await runs.recent_failures(db, now=now, hours=-5) == []


async def test_run_usage_counts_only_events_inside_each_window(db: AsyncSession) -> None:
    def run(start: datetime, finish: datetime | None) -> Run:
        return Run(
            started_at=start,
            last_beat_at=finish or start,
            finished_at=finish,
            end_reason="window" if finish else None,
            host=HOST,
            pid=PID,
            code_version=VERSION,
        )

    closed = run(T0, T0 + timedelta(minutes=15))
    open_run = run(T0 + timedelta(minutes=15), None)
    empty = run(T0 - timedelta(hours=5), T0 - timedelta(hours=4))
    db.add_all([closed, open_run, empty])

    def event(at: datetime, cost: str | None) -> UsageEvent:
        return UsageEvent(
            at=at, task="write", ok=True, duration_ms=1, cost_usd=Decimal(cost) if cost else None
        )

    db.add_all(
        [
            event(T0 - timedelta(seconds=1), "9.0000"),  # before closed
            event(T0, "0.0100"),  # closed (start inclusive)
            event(T0 + timedelta(minutes=10), None),  # closed, no cost
            event(T0 + timedelta(minutes=15), "0.0200"),  # finish exclusive -> open_run
            event(T0 + timedelta(minutes=20), "0.0300"),  # open_run
            event(T0 + timedelta(minutes=40), "5.0000"),  # after "now"
        ]
    )
    await db.commit()
    rows = list(await runs.list_runs(db, limit=10, offset=0))
    usage = await runs.run_usage(db, rows, now=T0 + timedelta(minutes=30))
    assert usage[closed.id] == (2, Decimal("0.0100"))
    assert usage[open_run.id] == (2, Decimal("0.0500"))
    assert usage[empty.id] == (0, Decimal(0))
    assert await runs.run_usage(db, [], now=T0) == {}


def test_code_version_is_twelve_hex_and_stable() -> None:
    first = code_version()
    assert re.fullmatch(r"[0-9a-f]{12}", first)
    code_version.cache_clear()
    assert code_version() == first


def test_fingerprint_covers_py_and_md_only(tmp_path: Path) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "a.py").write_text("x = 1\n")
    (tmp_path / "pkg" / "b.md").write_text("# prompt\n")
    base = fingerprint(tmp_path)
    (tmp_path / "pkg" / "c.pyc").write_bytes(b"junk")
    (tmp_path / "notes.txt").write_text("ignored")
    assert fingerprint(tmp_path) == base
    (tmp_path / "pkg" / "b.md").write_text("# prompt changed\n")
    changed = fingerprint(tmp_path)
    assert changed != base
    (tmp_path / "pkg" / "b.md").write_text("# prompt\n")
    (tmp_path / "pkg" / "a.py").rename(tmp_path / "pkg" / "z.py")
    assert fingerprint(tmp_path) != base  # a rename changes it too


def test_fingerprint_survives_an_unreadable_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "a.py").write_text("x = 1\n")
    (tmp_path / "b.md").write_text("# prompt\n")
    base = fingerprint(tmp_path)
    real = Path.read_bytes

    def flaky(self: Path) -> bytes:
        if self.name == "b.md":
            raise PermissionError("denied")
        return real(self)

    monkeypatch.setattr(Path, "read_bytes", flaky)
    unreadable = fingerprint(tmp_path)
    assert re.fullmatch(r"[0-9a-f]{12}", unreadable) and unreadable != base


# --------------------------------------------------------------------------- final fix wave


async def test_prune_keys_on_when_a_run_finished(db: AsyncSession) -> None:
    """Final wave T1: a run started long ago but finished recently (a Mac asleep mid-window)
    is kept; one that finished more than keep_days ago goes."""
    slept = await _start(db, T0 - timedelta(days=40))
    await runs.finish_run(db, slept.id, now=T0 - timedelta(days=1), reason="window")
    old = await _start(db, T0 - timedelta(days=40))
    await runs.finish_run(db, old.id, now=T0 - timedelta(days=31), reason="shutdown")
    await db.commit()
    slept_id, old_id = slept.id, old.id
    assert await runs.prune_runs(db, now=T0, keep_days=30) == 1
    ids = set((await db.execute(select(Run.id))).scalars())
    assert slept_id in ids and old_id not in ids


async def test_a_long_window_survives_the_next_rollover_too(db: AsyncSession) -> None:
    """Final wave T1: the 31-day-old open run closed at one rollover is not pruned at the next
    one (it finished just now)."""
    current = await _start(db, T0 - timedelta(days=31))
    await db.commit()
    current_id = current.id
    second = await runs.record_pass(db, current_id, _result(["a"]), now=T0, config=CONFIG)
    third = await runs.record_pass(
        db, second, _result(["b"]), now=T0 + timedelta(minutes=15), config=CONFIG
    )
    await db.commit()
    ids = set((await db.execute(select(Run.id))).scalars())
    assert {current_id, second, third} == ids


async def test_finish_run_can_add_a_last_line(db: AsyncSession) -> None:
    """Final wave T3: a stop after the lock was lost says so in the row it closes (one UPDATE,
    past the line cap); a finished run keeps its lines."""
    config = AppConfig.model_validate({"ops": {"max_run_lines": 10}})
    run = await _start(db)
    kept = [f"line {i}" for i in range(10)]
    await runs.record_pass(db, run.id, _result([*kept, "dropped"]), now=T0, config=config)
    await runs.finish_run(db, run.id, now=T0 + timedelta(minutes=1), reason="shutdown", line="Bye")
    await runs.finish_run(db, run.id, now=T0 + timedelta(minutes=2), reason="shutdown", line="X")
    await db.commit()
    row = await _get(db, run.id)
    assert [line["text"] for line in row.lines] == [*kept, "Bye"]
    assert row.lines[-1]["at"] == (T0 + timedelta(minutes=1)).isoformat()
    assert row.end_reason == "shutdown"


async def test_rollover_carries_lines_into_the_new_row(db: AsyncSession) -> None:
    """Final review M1: lines that describe the worker itself (``--no-send``) start the new row,
    so its health can be read from the open row at once."""
    run = await _start(db)
    new_id = await runs.record_pass(
        db, run.id, _result(["a"]), now=T0 + timedelta(minutes=15), config=CONFIG, carry=["Z"]
    )
    same = await runs.record_pass(
        db, new_id, _result(["b"]), now=T0 + timedelta(minutes=16), config=CONFIG, carry=["Z"]
    )
    await db.commit()
    assert same == new_id
    assert [line["text"] for line in (await _get(db, run.id)).lines] == ["a"]
    assert [line["text"] for line in (await _get(db, new_id)).lines] == ["Z", "b"]


async def test_failure_count_matches_the_listed_failures(db: AsyncSession) -> None:
    """Final wave T3: a run that started before the cutoff and beat through it counts only its
    failures since the cutoff (the list's rule); a run started since then counts every failure,
    also those past the kept 50."""
    now = T0

    def item(at: datetime, text: str) -> dict[str, str]:
        return {"at": at.isoformat(), "text": text}

    db.add(
        Run(
            started_at=now - timedelta(hours=30),
            last_beat_at=now - timedelta(minutes=59),
            finished_at=now - timedelta(minutes=59),
            end_reason="shutdown",
            host=HOST,
            pid=PID,
            code_version=VERSION,
            # 3 failures before the cutoff (one of them no longer kept), 2 after it.
            failed_steps=6,
            failures=[
                *(item(now - timedelta(hours=26), "Old step failed: OSError") for _ in range(3)),
                *(item(now - timedelta(hours=1), "New step failed: OSError") for _ in range(2)),
            ],
        )
    )
    busy = await _start(db, now - timedelta(minutes=30))
    await runs.record_pass(
        db,
        busy.id,
        _result([], ["Busy step failed: OSError"] * (MAX_FAILURES + 10)),
        now=now - timedelta(minutes=20),
        config=CONFIG,
    )
    await db.commit()
    listed = await runs.recent_failures(db, now=now, limit=500)
    assert len(listed) == 2 + MAX_FAILURES
    assert await runs.count_recent_failures(db, now=now) == 2 + MAX_FAILURES + 10


async def test_latest_run_prefers_the_open_row(db: AsyncSession) -> None:
    """Final wave T3: with the clock stepped back, a finished row can carry a later start than
    the open one; the open row is still the latest."""
    finished = await _start(db, T0 + timedelta(hours=1))
    await runs.finish_run(db, finished.id, now=T0 + timedelta(hours=2), reason="once")
    current = await _start(db, T0)
    await db.commit()
    latest = await runs.latest_run(db)
    assert latest is not None and latest.id == current.id
