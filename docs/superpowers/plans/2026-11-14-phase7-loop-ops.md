# SalesFinder Phase 7: Loop and Ops Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** SalesFinder runs unattended on the office Mac and the team can see that it does. The worker records a heartbeat log (`runs`) and honours quiet hours. Today and a new Runs page show whether the worker is alive, what it did and what failed, and every page warns when it isn't running. Teammates reach the dashboard over the LAN on HTTPS with correct invite links. Admins can export a clean companies CSV. launchd keeps web and worker running, and the docs cover install, upgrade, the first real mailbox and a real dry run. An end-to-end test drives the whole loop against fakes. Sending still ships paused.

**Architecture:** A new `ops/` package holds the heartbeat (`runs` rows, beats, health), quiet hours, the code-version fingerprint, LAN warnings and launchd plist rendering. `worker.py` gains `run_pass` (one pass returning a `PassResult`), and `run_forever` records each pass into the current `runs` row. A background beat task keeps the liveness timestamp fresh during long steps. One `runs` row covers one heartbeat window (`usage.heartbeat_interval_minutes`, default 15). The web app reads `ops.health.worker_health` for a site-wide banner, the Today card and `/runs`. `export.py` builds the companies CSV.

**Tech Stack:** Phase 1–6 stack. No new runtime dependencies (`plistlib`, `csv`, `hashlib`, `zoneinfo` are stdlib). macOS `launchd` (LaunchAgents) for process supervision; mkcert for LAN HTTPS.

**Spec:** `docs/superpowers/specs/2026-11-07-elchemy-salesfinder-design.md`:
- §9 milestone 9: loop wiring, LAN/HTTPS, launchd example, docs, end-to-end run against fakes, real dry run with sending paused.
- §6.1: the heartbeat, quiet hours, and "every run writes a `runs` row with what it did, counts, cost, and any failures".
- §5: budget and quiet hours.
- §6.7: network rules, and the Today, Runs and Settings (CSV export) screens.
- §8: ops/doctor, deployability (launchd plist), docs, and structured logs to file.
- §3: CSV export as the handoff.

Follow-ups folded in *(follow-up)*:
- `2026-11-14-phase6-followups.md` "For Phase 7" items 1–5.
- `2026-11-07-phase3-followups.md` / `2026-11-13-phase5-followups.md` item 9: filter excluded companies from exports.
- `2026-11-07-phase2-followups.md`:
  - worker heartbeat, "approved jobs wait forever";
  - "Before onboarding teammates".
- `2026-11-07-phase1-followups.md`:
  - "Before inviting the first teammate": revoke invites, `SALESFINDER_PUBLIC_URL`, the LAN allowed-hosts warning;
  - the Docs item (mkcert file mapping, teammates need the mkcert root CA, `make setup` needs `createdb` rights).

## Global Constraints

- **Never run `git commit`, `git push`, `git add`, or create branches.** The user commits. This plan has no commit steps.
- All Phase 1–6 constraints hold:
  - uv/Python 3.13, UUID PKs, UTC timestamptz, ruff 100, mypy strict;
  - **services flush, callers commit** (worker-job functions own their transactions);
  - **no DB transaction held across network, subprocess, DNS, SMTP or IMAP I/O**;
  - **untrusted text never goes in logs**: scraped values, reply bodies and subjects, From names, server text. The `runs.lines`/`runs.failures` columns hold only the worker's existing log-safe lines (counts, our own copy, mailbox addresses, exception type names) and must never gain anything else;
  - Jinja **autoescape**;
  - **CSRF on every POST**;
  - **every state change is audited**.
- **Lock order:** messages → sequence → advisory recipient → mailbox, then conversation. `runs` rows are written only in their own short transactions and never while any outreach lock is held.
- **No SMTP probing**, ever.
- **Passwords never in the DB, logs, pages, audit rows, CSV exports or rendered plists.** Plists carry no `SALESFINDER_*` variables, and `.env` is read from the working directory.
- **Tests are hermetic:**
  - `salesfinder_test` only, never the dev DB `salesfinder`;
  - fakes only: `tests/fakes/fake_claude`, `FakeSender`/`FakeReader`, `FakeResolver`, `FakeWeb`, `FakeSerper`, `FakeHubSpot`;
  - the conftest guards stay on (real claude, network, DNS, mail);
  - no test runs `launchctl`, `mkcert`, `plutil` or the real `claude`.
- **Sending stays paused by default**, and nothing in this phase resumes it. The "real dry run" is a **manual operator step** documented in `docs/operations.md`. No automated step in this plan runs the real `claude`, a real mailbox, Serper, HubSpot or real websites.
- **Spec values (verbatim):**
  - "A worker process … wakes every 15 minutes (configurable): wake → quiet hours? → budget left? → decide → act → log → sleep".
  - "Every run writes a `runs` row with what it did, counts, cost, and any failures."
  - "a heartbeat that sleeps in quiet hours, leaving headroom for the operator's own Claude use".
  - "LAN mode … requires HTTPS … the app refuses LAN mode over plain HTTP. Host/Origin checks against DNS rebinding. Not exposed to the internet in v1."
  - "Today: needs-you items, outbox count, sends today vs cap, replies, last run status and any failures." "Runs: heartbeat log, failures, Claude usage vs budget, discovery spend."
  - "A launchd plist example is included for keeping it running on the Mac."

## Review Focus

1. **The worker dies without shutting down** (SIGKILL, power loss, launchd's ExitTimeOut mid-pass, Postgres restarted under it). The open `runs` row stays unfinished; every page says "stopped responding"; the next start closes the row as `crashed`. A failed `runs` write never stops the worker → tests in Task 2 (crash close, record failure isolated) and Task 3 (stale banner).
2. **Quiet-hours clock edges:**
   - a window crossing midnight;
   - the DST changeover nights;
   - `end` exactly (exclusive);
   - `start == end` (no quiet hours);
   - supplier office hours (IST) that fall entirely inside the default US-Eastern quiet window.

   Sending and mail reading must keep running → tests in Task 1 and Task 2.
3. **Hostile text in the CSV export.** A scraped company name or title such as `=HYPERLINK("http://evil")`, `+1 555`, `@SUM(A1)` or a leading tab, a newline inside a title, or a non-ASCII name. Formula-leading cells get a `'` prefix, newlines become spaces, and the file starts with a UTF-8 BOM → tests in Task 5.
4. **LAN misconfiguration.** Possible mistakes:
   - LAN on with only loopback `allowed_hosts`;
   - `allowed_hosts` containing `*`;
   - an `http://` public URL in LAN mode;
   - a public-URL host missing from `allowed_hosts`;
   - an admin creating an invite while browsing on `localhost`.

   Doctor and Settings name the problem, and the invite link uses the public URL → tests in Task 4.
5. **Version skew after an upgrade.** The web app was restarted and the worker wasn't (or the reverse). The worker's code fingerprint differs from the web's, so a banner says to restart both. This is the Phase 6 deploy note made visible → tests in Task 3.

---

## Decisions (recorded for review)

- **Cadence stays a fast tick.** A pass runs every `worker.poll_seconds` (10 s) as today. Reason: one send per mailbox per pass with 3–9 min spacing needs a fast tick. A 15-minute tick would cap a mailbox at ~32 sends in an 8-hour office day, below the 50/day max, and delay opt-outs.
- **The spec's 15-minute heartbeat is the `runs` window.** One `runs` row covers `usage.heartbeat_interval_minutes` (default 15, now bounded 1–1440) of passes, with counts, lines, failures, Claude calls and cost. Reason: this keeps "every run writes a runs row" at ~96 rows/day instead of 8,640 idle rows. The existing key gets its meaning back.
- **Liveness is a beat, not a pass.** A background task updates `runs.last_beat_at` and the current step every `ops.beat_seconds` (30 s). The worker counts as down after `ops.worker_stale_seconds` (180 s) without a beat. Reason: a pass can legitimately take many minutes (a discovery job, writing up to 300 s, replies up to 300 s).
- **Quiet hours stop only the Claude steps** (Replies classification/drafting and Writing). Mail reading, sending, follow-up queuing, sweep, HubSpot, discovery, profiling and contacts keep running. Reasons:
  - Supplier office hours (09:00–17:00 IST) fall entirely inside the default quiet window (22:00–07:00 America/New_York), so a sleeping loop would never send to suppliers.
  - Opt-outs must be honoured immediately.
  - The spec's stated purpose is Claude headroom.

  `salesfinder draft` (operator-run) ignores quiet hours. A `start == end` window means "no quiet hours". *(follow-up P6 #3)*
- **Command name stays `salesfinder worker`** (the spec says `salesfinder run`). README, tests and plists already use `worker`; an alias would add a second name for one thing. It gains `--once` (one recorded pass, exit 1 if a step failed) and `--no-send` (the Sending step is skipped). The real dry run uses `--no-send` as well as the paused state.
- **Code-version fingerprint:** `ops.version.code_version()` returns the sha256 of the package's `*.py`/`*.md` files, first 12 hex. The worker stores it on its run row, and the web compares it with its own. A mismatch shows "restart both together" *(follow-up P6 #1)*. Reason: this makes the deploy rule visible instead of relying on people reading the README.
- **Held mailboxes:** we never log in to an untested host (P6-V/W), so we can't count unread mail. Any mailbox with a password that is held untested gets:
  - a red alert on Today;
  - a doctor `mailboxes` problem.

  The worker's existing "Not reading …" line still lands in the run log *(follow-up P6 #5)*.
- **Worker banner on every page** for every signed-in user. Its states are never run / stopped / stopped responding / version mismatch, read on full-page GETs like the sending banner. Reason: approved jobs and approved email silently wait without the worker *(follow-up P2)*. `/healthz` also reports `worker`.
- **Doctor `worker` check:** passes with "hasn't run yet" on a fresh install (setup runs doctor before the worker exists). It fails when the worker ran before and is now stopped, stale or on different code. **Doctor still doesn't check the Claude login.** A real call would spend budget; a signed-out CLI already surfaces as "Writing stopped/Replies stopped: …" in the run log and on Today.
- **CSV export:**
  - **Who:** admins only, a POST with CSRF from Settings (spec §6.7 lists it under Settings), audited `companies.exported` with side and row count (never data).
  - **Shape:** one row per company with up to `contacts.max_targets_per_company` contacts.
  - **Companies dropped:**
    - excluded (`status`);
    - live do-not-contact or HubSpot-known domain (re-checked in the query, not trusted from `status` alone);
    - `disqualified`;
    - **any company with a suppressed address**, i.e. any address of the company or any sequence recipient for it is in `suppressions`;
  - **Contacts dropped:** a contact whose address `exclusions_for_emails` flags *(follow-up P6 #2 / P3)*.
  - **Format:** UTF-8 with BOM (Excel), CRLF, formula-injection guard.
- **launchd:** two **LaunchAgents** (`com.elchemy.salesfinder.web`, `…worker`), not LaunchDaemons, because the Claude subscription login lives in the operator's user session.
  - Rendered with `plistlib` by `salesfinder ops plists`; tracked examples live in `deploy/launchd/`.
  - Each program is `<repo>/.venv/bin/salesfinder`, not `uv run`, which may re-sync or reach the network at start.
  - `PATH` includes the Claude CLI's directory, because launchd's default PATH lacks `~/.local/bin` and Homebrew.
  - `KeepAlive`; `ThrottleInterval` 30.
  - `ExitTimeOut`: 30 s for web, 120 s for the worker (to finish a pass; a kill mid-pass is crash-safe by design).
  - Logs go to `~/Library/Logs/SalesFinder/{web,worker}.log`, uncoloured when not a TTY. Rotation is documented (newsyslog), not automated.
  - The Mac needs automatic login and no system sleep (documented).
- **LAN/HTTPS:**
  - keep port 5555 with mkcert certificates, no reverse proxy;
  - add `SALESFINDER_PUBLIC_URL` (https required in LAN mode) for invite links;
  - `ops.network.lan_warnings(settings)` feeds a doctor `network` check and a Network section on Settings *(follow-up P1)*;
  - certificate expiry isn't checked; `cryptography` isn't a dependency, and the docs give the `openssl` one-liner.
- **Invites:** "Revoke" sets `expires_at = now` (no migration), audited `invite.revoked`. Deactivating a user expires the pending invites they created *(follow-up P1)*.
- **Runs page:** any signed-in teammate can read it. It shows:
  - the worker status;
  - Claude calls today against the budget;
  - discovery spend this month against the budget;
  - failures in the last 24 h;
  - the run log (25 per page), each run with passes, failed steps, Claude calls and cost in its window, and its lines.
- **Backups:** documented (`pg_dump` nightly suggestion), not automated.
- **Real dry run:** a manual operator runbook in `docs/operations.md`. Sending stays paused and the worker runs with `--no-send`. Real Claude, Serper, HubSpot and websites are used only by the operator, never by this plan's automated steps.

## File Structure

```
config/salesfinder.yaml               # usage comments, ops section
.env.example                          # + SALESFINDER_PUBLIC_URL
Makefile                              # plists, start, stop, restart, logs, e2e
deploy/launchd/com.elchemy.salesfinder.web.plist, …worker.plist   # tracked examples
src/salesfinder/
  config.py                           # OpsConfig; usage.heartbeat_interval_minutes bounds
  settings.py                         # public_url
  audit.py                            # INVITE_REVOKED, COMPANIES_EXPORTED
  logging.py                          # no colours when stderr isn't a TTY
  db/models/ops.py, db/models/__init__.py, db/migrations/versions/0016_runs.py
  ops/{__init__,runs,health,quiet,version,network,launchd}.py
  export.py
  worker.py, cli.py, doctor.py
  auth/users.py                       # revoke_invite, expire on deactivate
  sending/mailboxes.py                # not_read()
  web/app.py, web/deps.py, web/routes/{runs,home,admin}.py
  web/templates/{runs,today,base,admin_settings,admin_users}.html
docs/operations.md, docs/architecture.md, README.md
tests/test_runs.py, test_quiet.py, test_health.py, test_web_runs.py, test_network.py,
  test_export.py, test_launchd.py, test_e2e.py, test_docs.py
  (+ test_migrations, test_config, test_settings, test_worker, test_cli, test_doctor,
     test_pages, test_web_admin, test_web_admin_settings, test_users, test_logging_isolation)
```

---

### Task 1: `runs` table, ops config, heartbeat service, quiet hours, code version

**Files:**
- Create: `src/salesfinder/db/models/ops.py`, migration `0016_runs.py` (hand-written; **do not autogenerate against the dev DB**), `src/salesfinder/ops/{__init__,runs,quiet,version}.py`
- Modify: `db/models/__init__.py`, `config.py`, `config/salesfinder.yaml` (re-document `usage.heartbeat_interval_minutes` as "minutes of worker passes one `runs` row covers"; drop the "Reserved: not enforced yet" quiet-hours comment; add `ops`)
- Test: `tests/test_runs.py`, `tests/test_quiet.py`, `tests/test_migrations.py`, `tests/test_config.py`

**Interfaces:**
- Produces (table exact, migration `0016`, `down_revision="0015"`): `Run` → `runs` (UUIDPk):
  - `started_at` timestamptz not null; `last_beat_at` timestamptz not null; `last_pass_at` timestamptz null; `finished_at` timestamptz null.
  - `end_reason: str(12) | None`, with check `end_reason IN ('window','shutdown','crashed','once')` and check `(finished_at IS NULL) = (end_reason IS NULL)`.
  - `passes: int` default 0; `failed_steps: int` default 0.
  - `lines: JSONB` default `[]` (items `{"at": iso, "text": str}`); `lines_dropped: int` default 0.
  - `failures: JSONB` default `[]` (same item shape, at most 50).
  - `quiet: bool` default false.
  - `step: str(32) | None`, `step_started_at: timestamptz | None`.
  - `host: str(255)`, `pid: int`, `code_version: str(16)`.
  - Indexes: `ix_runs_started_at (started_at)` and partial `ix_runs_open (started_at) WHERE finished_at IS NULL`.
  - Constant `RUN_END_REASONS`.
- `config.OpsConfig` (`AppConfig.ops`):
  - `beat_seconds: int = 30` (5–300);
  - `worker_stale_seconds: int = 180`, which must be ≥ 2 × `beat_seconds` ("worker_stale_seconds must be at least twice beat_seconds");
  - `runs_keep_days: int = 30` (1–365);
  - `max_run_lines: int = 200` (10–2000).
- `UsageConfig.heartbeat_interval_minutes` becomes `Field(15, ge=1, le=1440)`.
- `ops.runs.PassResult` (frozen): `lines: list[str]`, `failures: list[str]` (the "X step failed: Type" lines), `quiet: bool`.
- `ops.runs` functions (each flushes; callers commit):
  - `start_run(db, *, now, host: str, pid: int, code_version: str) -> Run`: first closes every unfinished run (`finished_at = last_beat_at`, `end_reason="crashed"`, a line "The worker stopped without shutting down." appended), then inserts the new row (`last_beat_at = now`).
  - `record_pass(db, run_id, result: PassResult, *, now, config) -> uuid.UUID`: locks the row `FOR UPDATE`, then:
    - `passes += 1`, `last_pass_at = last_beat_at = now`;
    - appends lines up to `max_run_lines` (the rest count into `lines_dropped`);
    - appends failures (cap 50), `failed_steps += len(result.failures)`, `quiet |= result.quiet`;
    - **rollover** when `now - started_at >= usage.heartbeat_interval_minutes`: finish with `"window"`, `prune_runs`, `start_run` with the same host/pid/version (no crash-close, because this run was just finished). It returns the new id; otherwise the same id.
  - `beat(db, run_id, *, now, step: str | None, step_started_at: datetime | None) -> None`: an UPDATE only while `finished_at IS NULL`.
  - `finish_run(db, run_id, *, now, reason: Literal["window","shutdown","once"]) -> None`.
  - `prune_runs(db, *, now, keep_days) -> int`: deletes finished runs with `started_at < now - keep_days`.
  - `latest_run(db) -> Run | None`.
  - `list_runs(db, *, limit, offset) -> list[Run]` (newest first).
  - `recent_failures(db, *, now, hours: int = 24, limit: int = 5) -> list[tuple[datetime, str]]` (newest first, from runs whose `last_beat_at >= now - hours`).
  - `run_usage(db, runs: Sequence[Run], *, now) -> dict[uuid.UUID, tuple[int, Decimal]]`: Claude calls and summed `cost_usd` of `usage_events` in `[started_at, finished_at or now)`.
- `ops.quiet.in_quiet_hours(now: datetime, quiet: QuietHours) -> bool`:
  - converts `now` to `quiet.timezone`;
  - `start == end` → False;
  - `start < end` → `start <= t < end`;
  - else `t >= start or t < end`.
- `ops.quiet.quiet_line(quiet: QuietHours) -> str`: `"Quiet hours ({start:%H:%M}–{end:%H:%M} {timezone}): Claude steps wait until {end:%H:%M}."`
- `ops.version.code_version() -> str` (`lru_cache`): the sha256 over sorted `(relative path, bytes)` of every `*.py` and `*.md` under the `salesfinder` package, first 12 hex.

- [ ] **Step 1: Write the failing tests:**
  - **Migrations:** head `"0016"` everywhere the suite asserts the head (replace `"0015"`); round trip 0016 → 0015 → head; `end_reason='bogus'` is rejected; `finished_at` set with `end_reason` NULL is rejected.
  - **Config:** defaults; `worker_stale_seconds: 40` with `beat_seconds: 30` → ConfigError naming `ops`; `heartbeat_interval_minutes: 0` is rejected.
  - **`test_quiet.py`**, with the default 22:00–07:00 America/New_York:
    - 2026-11-14 03:30 UTC (22:30 EST) → True;
    - 2026-11-14 12:00 UTC (07:00 EST, end exclusive) → False;
    - 2026-07-01 02:30 UTC (22:30 EDT) → True;
    - 2026-03-08 06:30 UTC (01:30 EST, the night clocks go forward) → True; 2026-03-08 11:30 UTC (07:30 EDT) → False;
    - 2026-11-01 05:30 UTC (01:30 EDT, the night clocks go back) → True;
    - `start == end` → always False;
    - a non-crossing `13:00`–`14:00` → 13:59 True, 14:00 False;
    - `quiet_line` exact text.
  - **`test_runs.py`:**
    - `start_run` closes an older open run as `crashed` with `finished_at == its last_beat_at`;
    - `record_pass` appends and counts; 250 lines with `max_run_lines=200` → 200 kept, `lines_dropped == 50`;
    - rollover at exactly 15 min → the old row is `window` and a new row is returned;
    - rollover prunes a 31-day-old finished run but keeps a 31-day-old *open* one;
    - `beat` doesn't touch a finished run;
    - `recent_failures` order and limit;
    - `run_usage` counts only events inside the window;
    - `code_version()` is 12 hex and stable across two calls.
- [ ] **Step 2: Run** `uv run pytest tests/test_runs.py tests/test_quiet.py tests/test_migrations.py tests/test_config.py -q` → FAIL.
- [ ] **Step 3: Implement** per the interfaces. The migration uses explicit constraint names.
- [ ] **Step 4: Run** the same command → PASS; `make check` green.

---

### Task 2: Worker loop wiring: recorded passes, beats, quiet hours, `--once`, `--no-send` *(follow-ups P2 heartbeat, P6 #3)*

**Files:**
- Modify: `src/salesfinder/worker.py`, `src/salesfinder/cli.py` (`worker` options), `README.md` (the worker paragraph: runs log, quiet hours, `--once`, `--no-send`)
- Test: `tests/test_worker.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: Task 1 `ops.runs`, `ops.quiet`, `ops.version`.
- Produces:
  - `WorkerDeps` gains:
    - `send_enabled: bool = True`;
    - `run_id: uuid.UUID | None = None`;
    - `current_step: str | None = None`, `step_started_at: datetime | None = None`;
    - `quiet_noted: bool = False`, `no_send_noted: bool = False`;
    - `code_version: str = field(default_factory=code_version)`.
  - `worker.run_pass(deps) -> PassResult`: the existing step order. `_step` sets `current_step`/`step_started_at` and collects failure lines into `PassResult.failures` (they stay in `lines` as today). When `in_quiet_hours(deps.now(), config.usage.quiet_hours)`:
    - **Replies and Writing are not called**;
    - `quiet_line(...)` is added once per quiet period (`quiet_noted`, reset when quiet ends);
    - `PassResult.quiet=True`.
  - `run_once(deps) -> list[str]` stays (returns `(await run_pass(deps)).lines`), so existing tests keep working.
  - `_sending` with `send_enabled=False` returns `["Sending skipped: this worker runs with --no-send."]` once per process, then `[]`, and never calls `send_pass`.
  - `worker.beat_once(deps) -> None`: `ops.runs.beat(...)` in its own transaction with `deps.current_step`/`step_started_at`. `_beat_loop(deps, stop)` calls it every `ops.beat_seconds` until `stop` is set.
  - `worker.record(deps, result: PassResult) -> None`: `record_pass` in its own transaction, then updates `deps.run_id`. **Any exception is caught** and logged `run_record_failed` with `error_type` only; the worker keeps running.
  - `run_forever(deps, *, stop)`:
    1. `start_run` (own transaction; host `socket.gethostname()[:255]`, `os.getpid()`, `deps.code_version`);
    2. start `_beat_loop` as a task;
    3. loop: `run_pass` → log lines → `record` → wait `poll_seconds`;
    4. finally: cancel the beat task, then `finish_run(reason="shutdown")` (errors logged type-only).
  - CLI `salesfinder worker [--once] [--no-send]`:
    - `--no-send` sets `send_enabled=False`.
    - `--once` takes the same single-worker lock, then `start_run` → `run_pass` → `record` → `finish_run("once")`. It prints each line and exits 1 if `failures`, else 0.
    - "Another SalesFinder worker is already running." and exit 1 as today.
- [ ] **Step 1: Write the failing tests:**
  - `test_worker.py`:

```python
async def test_run_pass_collects_failures(...)            # one step raises -> failures == ["Profiling step failed: RuntimeError"], next steps still ran
async def test_quiet_hours_skip_claude_steps_only(...)    # config quiet 12:00-14:00 America/New_York, now SEND_NOW (13:00 EST): an approved buyer email is sent (FakeSender.sent == 1), a waiting reply isn't classified and an active campaign isn't drafted (fake_claude calls == []), quiet line once over two passes, result.quiet
async def test_quiet_line_again_after_quiet_ends(...)     # quiet -> not quiet -> quiet: the line appears twice in total
async def test_no_send_never_calls_send_pass(...)         # resumed + approved mail + send_enabled=False -> FakeSender.sent == [], skip line once over two passes; Mail/Follow-ups still ran
async def test_run_forever_records_and_finishes(...)      # stop after the first pass -> one runs row, passes == 1, end_reason == "shutdown", code_version == deps.code_version
async def test_run_forever_closes_crashed_run(...)        # a pre-existing open run -> end_reason "crashed" before the new one opens
async def test_rollover_across_window(...)                # clock advances 16 min between two passes -> two rows, first "window"
async def test_record_failure_does_not_stop_worker(...)   # record_pass raises -> 2 passes still run; caplog has run_record_failed with error_type and no exception text
async def test_beat_once_updates_step(...)                # current_step "Discovery" -> runs.step == "Discovery", last_beat_at == now
```

  - `test_cli.py`, with `salesfinder.worker.run_pass` monkeypatched to a stub and `default_mail` patched to `fake_mail(FakeSender(), None)`:
    - `worker --once` prints the stub's line, exits 0, and leaves a run with `end_reason == "once"`;
    - a stub with a failure exits 1;
    - with the lock held by another connection → "Another SalesFinder worker is already running.", exit 1.
- [ ] **Step 2: Run** `uv run pytest tests/test_worker.py tests/test_cli.py -q` → FAIL.
- [ ] **Step 3: Implement.** Keep `test_run_once_step_order` green: the step names and order are unchanged.
- [ ] **Step 4: Run** → PASS; `make check` green.

---

### Task 3: Worker health: banner, Today card, Runs page, doctor, `salesfinder runs`, held-mailbox alert *(follow-ups P2, P6 #1, P6 #5)*

**Files:**
- Create: `src/salesfinder/ops/health.py`, `web/routes/runs.py`, `web/templates/runs.html`
- Modify:
  - `web/app.py` (router; `app.state.code_version = code_version()`);
  - `web/deps.py` (`AuthContext.worker_problem`);
  - `base.html` (nav "Runs"; the worker banner under the sending banner);
  - `web/routes/home.py` + `today.html` (Worker card, mail-not-read alert, `/healthz` `worker`);
  - `sending/mailboxes.py` (`not_read`);
  - `doctor.py` (`worker` check; `mailboxes` lists untested ones);
  - `cli.py` (`runs` command).
- Test: `tests/test_health.py`, `tests/test_web_runs.py`, `tests/test_pages.py`, `tests/test_doctor.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: Task 1 `ops.runs`, `code_version`; `brain.budget.calls_today`; `discovery.budget.month_spend`/`get_budget`; `sending.mailboxes.credentials`/`untested`.
- Produces:
  - `ops.health.WorkerHealth` (frozen): `state: Literal["never","running","stale","stopped","mismatch"]`, `problem: str | None` (None only for `running`), `last_pass_at`, `last_beat_at`, `step: str | None`, `run: Run | None`.
  - `ops.health.worker_health(db, *, now, config, code_version: str) -> WorkerHealth`, from `latest_run`, in this order:
    1. No row → `never`.
    2. `finished_at` set → `stopped`.
    3. `now - last_beat_at > ops.worker_stale_seconds` → `stale`.
    4. `run.code_version != code_version` → `mismatch`.
    5. Else `running`.
  - Copy (exact):
    - `NEVER = "The worker hasn't run yet — start it (see Operations). Approved email, replies and discovery jobs wait until it runs."`
    - `STOPPED = "The worker isn't running (stopped {ago}). Approved email, replies and discovery jobs wait until it's back."`
    - `STALE = "The worker stopped responding {ago}{during}. Approved email, replies and discovery jobs wait until it's back."` where `during` is `" (during <step>)"` or `""`.
    - `MISMATCH = "The web app and the worker are running different versions — restart both together."`
  - `ops.health.ago(delta: timedelta) -> str`:
    - `< 60 s` → "less than a minute ago";
    - `< 120 min` → "N min ago";
    - `< 48 h` → "N h ago";
    - else "N days ago".
  - `AuthContext.worker_problem: str | None`: set on full-page GETs from `worker_health(..., code_version=request.app.state.code_version)`. `base.html` shows `<p class="alert alert-error worker-banner" role="status">` on every page.
  - `sending.mailboxes.not_read(db, settings, config) -> list[str]`: addresses of mailboxes with credentials that are `untested` (the same predicate `poll_mailboxes` skips).
  - Today:
    - a **Worker** card: the health line ("Running — last pass {ago}" when healthy), "Failures in the last 24 h: N" with up to 5 `recent_failures` (time + text), and a link to `/runs`;
    - an alert per `not_read` address: `"Not reading {address}: its connection hasn't passed a test, so replies and opt-outs there wait unread. An admin should press Test connection on the Mailboxes page."`
  - `GET /healthz` adds `"worker": <state>` (status code unchanged).
  - `GET /runs?page=N` (any signed-in user), 25 per page:
    - a summary: health, Claude calls today / `usage.max_daily_claude_calls`, discovery spend this month / budget (`$x.xx`), failures in the last 24 h;
    - a table: window start → end (or "running"), end reason label (Running / Closed / Shut down / Crashed / Single pass), passes, failed steps, quiet badge, Claude calls and cost (`run_usage`), and `<details>` with the lines (escaped) and "+N more lines" when dropped.
  - Doctor:
    - `run_checks` adds `worker` after `sending`: `never` → ok with detail "hasn't run yet"; `running` → ok "running, last pass <ago>"; else fail with the health problem.
    - `_check_mailboxes` adds `"<address>: not read until Test connection passes — opt-outs may be waiting."` for each `not_read` address (enabled or not).
  - CLI `salesfinder runs [--limit 10]`: the health line (problem or "Worker running, last pass …"), then one line per run: `"<start> → <end|running>  <reason>  passes=<n>  failed=<n>"`.
- [ ] **Step 1: Write the failing tests:**
  - `test_health.py`:
    - each state from seeded rows;
    - `stale` at `worker_stale_seconds + 1` but not at exactly `worker_stale_seconds`;
    - a mismatch shows only while running;
    - `ago` boundaries (59 s, 60 s, 119 min, 2 h, 47 h, 2 days).
  - `test_web_runs.py`:
    - `/runs` lists runs newest first, escapes a line containing `<script>`, shows "+50 more lines", shows the Claude calls of a run's window, pages;
    - the Today Worker card shows a seeded failure line;
    - the banner appears on `/companies` when stale and not when running;
    - the mismatch banner text;
    - the Today not-read alert for a mailbox with a password held `NOT_TESTED`, and no alert without a password;
    - `/healthz` has `worker`.
  - `test_pages.py`: any assertion that a fresh page has no alert is updated for the "hasn't run yet" banner.
  - `test_doctor.py`: `worker` ok on an empty DB; fail when stopped; the untested-mailbox problem text.
  - `test_cli.py`: `runs` on an empty DB prints the `NEVER` copy.
- [ ] **Step 2: Run** `uv run pytest tests/test_health.py tests/test_web_runs.py tests/test_doctor.py tests/test_cli.py -q` → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** → PASS; `make check` green.

---

### Task 4: LAN/HTTPS mode: public URL, LAN warnings, invite revocation *(follow-ups P1/P2 "before inviting the first teammate")*

**Files:**
- Create: `src/salesfinder/ops/network.py`
- Modify:
  - `settings.py` (`public_url`), `.env.example`;
  - `doctor.py` (`network` check);
  - `web/routes/admin.py` (invite link, revoke route, Network section data, deactivate audit details), `admin_users.html` (Revoke button), `admin_settings.html` (Network section);
  - `auth/users.py`, `audit.py`.
- Test: `tests/test_network.py`, `tests/test_settings.py`, `tests/test_doctor.py`, `tests/test_users.py`, `tests/test_web_admin.py`, `tests/test_web_admin_settings.py`

**Interfaces:**
- Produces:
  - `Settings.public_url: str | None = None`. It must be `http`/`https` with a host, with no userinfo, query, fragment or path other than `/`; it is stored without the trailing slash.
    - Otherwise: "SALESFINDER_PUBLIC_URL must look like https://host[:port]".
    - In LAN mode a non-https value is refused: "SALESFINDER_PUBLIC_URL must use https in LAN mode."
  - `ops.network.lan_warnings(settings) -> list[str]`, exact copy:
    - LAN mode and every `allowed_hosts` entry in `LOOPBACK_HOSTS`: `LOOPBACK_ONLY = "LAN mode is on but SALESFINDER_ALLOWED_HOSTS lists only loopback names — teammates' browsers will be refused. Add the names they use, e.g. [\"salesfinder.local\", \"192.168.1.20\"]."`
    - `"*"` in `allowed_hosts`: `WILDCARD = "SALESFINDER_ALLOWED_HOSTS contains \"*\" — any Host header is accepted, so DNS-rebinding protection is off."`
    - LAN mode and no `public_url`: `NO_PUBLIC_URL = "LAN mode is on but SALESFINDER_PUBLIC_URL isn't set — invite links will use whatever address the admin browsed from."`
    - `public_url` host not in `allowed_hosts` (and no `"*"`): `PUBLIC_URL_HOST = "SALESFINDER_PUBLIC_URL's host {host} isn't in SALESFINDER_ALLOWED_HOSTS — invite links would be refused."`
  - `ops.network.mode_line(settings) -> str`: "Loopback only (127.0.0.1)" or "LAN over HTTPS on {bind_host}:{port}".
  - Doctor `network` check (after `https`): ok iff `lan_warnings` is empty; detail `mode_line` or the warnings joined.
  - Admin Settings: a **Network** section with `mode_line`, allowed hosts, public URL (or "not set") and each warning as `alert-warn`.
  - The invite link is `f"{settings.public_url}/invite/{raw}"` when set, else today's `request.base_url` form.
  - `users.revoke_invite(db, invite_id, *, now) -> Invite`:
    - LookupError if missing or already expired;
    - `ValueError(INVITE_USED="That invite was already used.")` if accepted;
    - sets `expires_at = now`.
  - `users.expire_invites_from(db, user_id, *, now) -> int`. `set_active(..., False)` calls it, and the deactivate route's audit details gain `invites_revoked`.
  - Route `POST /admin/invites/{id}/revoke` (admin, CSRF, audit `invite.revoked` with the email and role, PRG to `/admin/users?msg=invite_revoked`). Flash copy: "Invite revoked."
  - `audit.INVITE_REVOKED = "invite.revoked"`.
- [ ] **Step 1: Write the failing tests:**
  - settings: an `http` public URL in LAN mode is refused; a path is refused; a trailing slash is stripped; the secret key is never echoed in the errors.
  - `lan_warnings`, one test per rule plus a clean LAN config → `[]`; `mode_line`.
  - doctor `network` fails with `LOOPBACK_ONLY` for LAN with loopback-only hosts.
  - The invite link uses `https://salesfinder.local:5555/invite/…` when set, even when the request came to `127.0.0.1`.
  - Revoke: a member gets 403; no CSRF → 403; revoked → gone from pending and accepting it fails; revoking an accepted invite → the error flash; audited.
  - Deactivating an admin expires their two pending invites (count in the audit details).
  - Settings page Network section escapes the hosts and shows the warning.
- [ ] **Step 2: Run** `uv run pytest tests/test_network.py tests/test_settings.py tests/test_users.py tests/test_web_admin.py tests/test_web_admin_settings.py tests/test_doctor.py -q` → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** → PASS.

---

### Task 5: Companies CSV export *(follow-ups P6 #2, P3/P5 #9)*

**Files:**
- Create: `src/salesfinder/export.py`
- Modify:
  - `campaigns/service.py` (rename `_preference` → public `address_preference`, callers updated);
  - `web/routes/admin.py` + `admin_settings.html` (Export form: side buyer / supplier / all);
  - `audit.py` (`COMPANIES_EXPORTED = "companies.exported"`).
- Test: `tests/test_export.py`, `tests/test_web_admin_settings.py`

**Interfaces:**
- Consumes: `exclusions.check.exclusions_for_emails`, `signals.settings.confirmed_codes`, `signals.latest.latest_values`, `signals.format.format_value`, `campaigns.service.address_preference`.
- Produces:
  - `export.COLUMNS = ("side","company","domain","website","country","region","score","fit_reasons","profile_status","contact_1_name","contact_1_title","contact_1_email","contact_1_email_status","contact_2_name","contact_2_title","contact_2_email","contact_2_email_status","outreach_status","last_sent_at","reply_intent")`. The contact columns repeat up to `contacts.max_targets_per_company`; with the default 2 this is the tuple shown.
  - `export.csv_cell(value: object) -> str`:
    - `None` → `""`;
    - CR/LF/TAB inside the value → a space;
    - a result starting with `=`, `+`, `-`, `@`, `\t` or `\r` (after the replacement, checked on the original first char) → prefixed with `'`.
  - `export.company_rows(db, *, side: Literal["buyer","supplier","all"], config, catalog) -> list[dict[str, str]]`, ordered by side, then score desc, then domain. Companies dropped when any of these holds:
    - `status == "excluded"`;
    - the domain is on the `do_not_contact` domain list or a `known_relationships` domain (live `NOT EXISTS` subqueries, as in `exclusions/sweep.py`);
    - `disqualified`;
    - **any suppressed address** among the company's `email_addresses` or its sequences' recipients.

    Per company:
    - Contacts: target people in rank order, each with its best linked address by `address_preference`. A contact is dropped when `exclusions_for_emails` flags that address.
    - `fit_reasons`: `"; ".join(f"{label}: {format_value(code, value, catalog)}")` over latest values of confirmed signals.
    - `outreach_status`: the newest sequence's status, else `""`.
    - `last_sent_at`: max `messages.sent_at` (ISO, UTC).
    - `reply_intent`: the newest conversation's intent.
  - `export.to_csv(rows) -> str`: `"﻿"` + `csv.writer(lineterminator="\r\n")`, with the header from `COLUMNS` and every cell through `csv_cell`.
  - `POST /admin/export/companies` (admin, CSRF; form `side`):
    - writes the audit row `companies.exported` `{side, rows}`, commits, then returns `Response(to_csv(rows), media_type="text/csv; charset=utf-8")` with `Content-Disposition: attachment; filename="salesfinder-companies-<side>-<YYYYMMDD>.csv"` and `Cache-Control: no-store`;
    - an unknown side → 400.
- [ ] **Step 1: Write the failing tests** (`test_export.py`, real DB):
  - an excluded company, a DNC-domain company whose `status` is still `new` (sweep not run), a HubSpot-known domain and a disqualified one → all absent;
  - a company with one unsubscribed address → absent; with only a DNC'd *email* → present, with that contact blank;
  - two targets → contact 1/2 in rank order with the preferred address (published over pattern_confirmed);
  - `csv_cell` table: `=HYPERLINK("x")` → `'=HYPERLINK("x")`, `+1 555` → `'+1 555`, `-5` → `'-5`, `@SUM(A1)` → `'@SUM(A1)`, `"\tTab"` → `"' Tab"`, `"a\nb"` → `"a b"`, `"Müller GmbH"` unchanged;
  - `to_csv` starts with the BOM and the header equals `COLUMNS`;
  - a sent message and a conversation fill `outreach_status`/`last_sent_at`/`reply_intent`.
  - web: a member gets 403; no CSRF → 403; an admin gets `text/csv`, the attachment filename and a `companies.exported` audit row with `rows` and no company data; `side=bogus` → 400.
- [ ] **Step 2: Run** `uv run pytest tests/test_export.py tests/test_web_admin_settings.py -q` → FAIL.
- [ ] **Step 3: Implement.** One query for companies plus batched queries for people, addresses, sequences and conversations (no per-company query loop).
- [ ] **Step 4: Run** → PASS; `make check` green.

---

### Task 6: End-to-end run against fakes

**Files:**
- Create: `tests/test_e2e.py`
- Modify: only what the run proves broken, each fix with its own regression test in the owning module's test file (list them in the hand-back).
- Test: `tests/test_e2e.py`

**Interfaces:**
- Consumes everything above plus:
  - `tests/fakes/{hubspot,serper,web,dns,mail}.py`, `fake_claude` via `fake_claude_env`;
  - `sending_helpers.sending_settings/ready_mailbox`, `campaign_helpers.FACTS` (confirmed persona and address), `mail_helpers.reply_to`;
  - the web app built like `confirmed_app` (so approvals go through `POST /outbox/{id}/approve` with CSRF).
- Produces:
  - the test-local helper `Clock` (mutable `now()`);
  - `drive(deps, *, until: Callable[[], Awaitable[bool]], max_passes: int = 10) -> list[PassResult]`, which runs `run_pass` + `record` until `until()` holds (failing after `max_passes`) and asserts no pass had `failures`.
- Setup:
  - clock Tue 2026-11-10 14:00 UTC (09:00 EST);
  - settings with fake HubSpot/Serper keys, `mailbox_passwords={"sales_box": …}`, `claude_bin` = the fake;
  - `FakeHubSpot` knows `known-co.com`;
  - `FakeSerper` returns `acme-soap.com`, `beta-clean.com`, `known-co.com`;
  - `FakeWeb` serves:
    - `acme-soap.com`: a home page naming sodium laureth sulfate and shampoo, an about/team page "Jane Doe | Purchasing Manager" and a published `jane.doe@acme-soap.com`;
    - `beta-clean.com`: a home page with catalog terms and no people;
  - `FakeResolver` gives MX for both domains and SPF/DKIM/DMARC/MX for `send-domain.com`;
  - free signals confirmed; a buyer cohort requiring `USES_CATALOG_CHEMICAL`; a campaign with the ready mailbox, activated; sending resumed;
  - the discovery job estimated and approved by a teammate through the discovery service.
- [ ] **Step 1: Write the tests:**

```python
async def test_buyer_loop_end_to_end(...)
    # drive until a first email is awaiting_approval:
    #   known-co.com stored as excluded and never profiled; acme profiled with USES_CATALOG_CHEMICAL;
    #   Jane is a target with her published address; 1 fake_claude writer call
    # approve via the web app -> drive until FakeSender.sent == 1:
    #   envelope_to jane.doe@acme-soap.com; body == stored body; From is the persona
    # put reply_to(sent, body="Could you send the CoA for SLES?") in INBOX;
    #   advance 6 min -> drive until a reply draft is awaiting_approval:
    #   step 2 cancelled; sequence replied; draft in_reply_to == their Message-ID;
    #   fake_claude calls: 1 classify + 1 draft
    # approve the reply -> drive -> FakeSender.sent == 2 with In-Reply-To set
    # runs: every row has failures == []; some row's lines contain "Sent 1 email(s).";
    #   worker_health(now=clock()) is "running"; GET /runs shows "Sent 1 email(s)."
async def test_unsubscribe_and_export_end_to_end(...)
    # after the first send, INBOX gets reply_to(sent, body="Please remove me from your list")
    # -> suppression 'unsubscribed', sequence stopped, conversation handled, 0 classify calls
    # admin POST /admin/export/companies side=buyer -> beta-clean.com present;
    #   acme-soap.com (suppressed) and known-co.com (excluded) absent
async def test_dry_run_mode_drafts_but_never_sends(...)
    # sending left paused (no resume) and send_enabled=False: drive until a first email is
    # approved -> the Outbox "approved" view shows it held "Sending is paused: …";
    # FakeSender.sent == []; the skip line appears once
```

- [ ] **Step 2: Run** `uv run pytest tests/test_e2e.py -v`. Expected first: PASS if Tasks 1–5 are right. Any failure is a real integration bug: fix it in the owning module with a regression test there, never by loosening the e2e assertions.
- [ ] **Step 3: Run** `uv run pytest tests/test_e2e.py -v` → PASS, then `make check` green.

---

### Task 7: launchd plists, `salesfinder ops plists`, Makefile, log output, docs *(follow-ups P6 #1, #4; P1 Docs)*

**Files:**
- Create:
  - `src/salesfinder/ops/launchd.py`;
  - `deploy/launchd/com.elchemy.salesfinder.web.plist`, `deploy/launchd/com.elchemy.salesfinder.worker.plist` (examples, generated by the render function);
  - `docs/operations.md`, `docs/architecture.md`;
  - `tests/test_launchd.py`, `tests/test_docs.py`.
- Modify:
  - `cli.py` (`ops` group, `plists` command);
  - `logging.py` (`ConsoleRenderer(colors=sys.stderr.isatty())`);
  - `Makefile`;
  - `README.md`:
    - intro: it still says "So far … Phase 1 and 2";
    - the config var list: add `MAILBOX_PASSWORDS`, `PUBLIC_URL`;
    - links to the docs;
    - the mkcert cert/key → `SALESFINDER_TLS_CERTFILE`/`KEYFILE` mapping;
    - project layout: `ops/`, `export.py`, `mail/`, `sending/`, `replies/`, `followups/`, `outreach/`, `suppressions/`;
    - `make setup` needs `createdb` rights.
- Test: `tests/test_launchd.py`, `tests/test_docs.py`, `tests/test_logging_isolation.py`, `tests/test_cli.py`

**Interfaces:**
- Produces:
  - `launchd.LABELS = {"web": "com.elchemy.salesfinder.web", "worker": "com.elchemy.salesfinder.worker"}`.
  - `launchd.render(kind: Literal["web","worker"], *, repo: Path, log_dir: Path, claude_dir: Path | None) -> bytes`, via `plistlib.dumps`. Keys:
    - `Label`;
    - `ProgramArguments=[str(repo/".venv/bin/salesfinder"), kind]`;
    - `WorkingDirectory=str(repo)`;
    - `EnvironmentVariables={"PATH": <claude_dir>:<repo>/.venv/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin (deduped, order kept), "LANG": "en_US.UTF-8"}`;
    - `RunAtLoad=True`, `KeepAlive=True`, `ThrottleInterval=30`, `ProcessType="Standard"`;
    - `ExitTimeOut` 30 (web) / 120 (worker);
    - `StandardOutPath`/`StandardErrorPath` = `log_dir/<kind>.log`.
  - `launchd.claude_dir(claude_bin: str) -> Path | None`: the directory of `shutil.which(claude_bin)` (never runs it).
  - CLI `salesfinder ops plists [--out DIR] [--log-dir DIR] [--force]`:
    - the repo is the current directory, which must contain `pyproject.toml` and `.venv/bin/salesfinder`; otherwise exit 1 "Run this from the SalesFinder folder after `uv sync`.";
    - the Claude directory comes from settings, and if it isn't found: exit 1 "Claude CLI not found: set SALESFINDER_CLAUDE_BIN to its absolute path.";
    - without `--out` it prints both plists;
    - with `--out` it writes `<label>.plist` (refusing to overwrite without `--force`: "<path> exists; re-run with --force."), creates `--log-dir` (default `~/Library/Logs/SalesFinder`), and prints the `launchctl bootstrap gui/<uid> …` lines to run.
  - Makefile targets (added to `.PHONY`; **never run by tests or by the executor**):
    - `plists`: `uv run salesfinder ops plists --out $(HOME)/Library/LaunchAgents`;
    - `start`: bootstrap both;
    - `stop`: bootout worker, then web;
    - `restart`: `launchctl kickstart -k` the worker and the web, always both, per the deploy note;
    - `logs`: `tail -F` both logs;
    - `e2e`: `uv run pytest tests/test_e2e.py -q`.
  - `docs/operations.md` sections:
    1. **Run it on the office Mac:**
       - automatic login and never sleep (`sudo pmset -a sleep 0 disksleep 0`);
       - `brew services start postgresql@17`;
       - `uv sync`, `make migrate`, `make plists`, `make start`;
       - logs, and newsyslog rotation with an `/etc/newsyslog.d/salesfinder.conf` example;
       - allow incoming connections for the venv's Python in the macOS firewall.
    2. **LAN and HTTPS:**
       - mkcert on the Mac (`mkcert -install`; `mkcert salesfinder-mac.local 192.168.1.20` → `SALESFINDER_TLS_CERTFILE=…/salesfinder-mac.local+1.pem`, `KEYFILE=…-key.pem`);
       - **each teammate installs the mkcert root CA** (`mkcert -CAROOT`; copy `rootCA.pem`; trust it in Keychain / the OS store), never `rootCA-key.pem`;
       - `SALESFINDER_BIND_HOST`, `ALLOWED_HOSTS`, `PUBLIC_URL`;
       - the Settings Network section and doctor `network`;
       - checking expiry with `openssl x509 -enddate -noout -in <cert>`;
       - not exposed to the internet.
    3. **Upgrading:** `make stop` → pull → `uv sync` → `uv run salesfinder db upgrade` → `make start`. **"Restart the web app and the worker together"**, never one alone; the version banner says when they differ *(P6 #1)*.
    4. **Is it running?** The worker banner, Today's Worker card, `/runs`, `salesfinder runs`, `salesfinder doctor`; quiet hours and what they pause.
    5. **Backups:** a nightly `pg_dump -Fc salesfinder > ~/Backups/salesfinder-$(date +%F).dump` (via cron or a user LaunchAgent) and how to restore.
    6. **First real mailbox** *(P6 #4)*:
       1. Buy the separate sending domain (never elchemy.com) and set up Google Workspace.
       2. Set SPF/DKIM/DMARC.
       3. Create an app password and put it in `.env` `SALESFINDER_MAILBOX_PASSWORDS` (one place only), then `make restart`.
       4. An admin adds the mailbox, presses **Test connection** (held until it passes), then **Check DNS**.
       5. Set the warm-up start date when the warm-up service starts, then enable.
       6. Wait out the 14 days.
       7. Only after the dry run below passes, an admin resumes sending.
    7. **Real dry run (sending paused): a manual step the operator runs.**
       - Precheck: `salesfinder sending status` must say paused, otherwise `salesfinder sending pause --reason "Dry run"`.
       - `make stop`, then run `uv run salesfinder worker --no-send` in a terminal.
       - Confirm signals; start one small discovery job (≤ 10 searches); create a cohort and a campaign with the planned mailbox (it may stay disabled); activate.
       - Watch `/runs` for failures, Claude calls against the budget and quiet hours; review and approve a few drafts.
       - Confirm they sit in "Approved — waiting to send" held by the pause; `sending status` shows sent today 0; nothing appears in the mailbox's Sent folder.
       - Finish: withdraw the approvals or archive the campaign, then `make start`.
       - A checklist of what "passed" means.
    8. **Troubleshooting:** a table covering:
       - worker banner states;
       - "Invalid host header" (allowed hosts);
       - certificate warnings (root CA not installed);
       - "Writing stopped: Claude is signed out" (`claude` login as the operator);
       - HubSpot stale;
       - "Not reading <mailbox>";
       - "Another SalesFinder worker is already running".
  - `docs/architecture.md`:
    - the two processes and the single-worker lock;
    - the pass step order and cadences (pass, mail poll, writing interval, runs window, beat);
    - the module map;
    - the safety invariants (gate twice, paused by default, no SMTP probing, untrusted text, secrets);
    - the lock order;
    - where state lives (`app_settings` keys, `runs`, `audit_log`).
- [ ] **Step 1: Write the failing tests:**
  - `test_launchd.py`:
    - `plistlib.loads(render(...))` has the keys and values above;
    - the worker `ExitTimeOut == 120`;
    - PATH starts with `claude_dir` and has no duplicates;
    - with a `Settings` holding a secret key and a mailbox password, neither value nor the string `SALESFINDER_` appears in the bytes;
    - each `deploy/launchd/*.plist` equals `render(kind, repo=Path("/Users/YOU/SalesFinder"), log_dir=Path("/Users/YOU/Library/Logs/SalesFinder"), claude_dir=Path("/Users/YOU/.local/bin"))` (drift);
    - the CLI `ops plists --out tmp` writes both files; a second run without `--force` exits 1 with the exact message; a missing `.venv/bin/salesfinder` exits 1. The test's repo is a `tmp_path` with a stub `pyproject.toml` and an executable stub file, and `claude_bin` is an absolute stub under `tmp_path`.
  - `test_logging_isolation.py`: `configure_logging` with a non-TTY stderr → a logged line has no `\x1b[`.
  - `test_docs.py`:
    - every `make <target>` in `README.md`/`docs/*.md` (excluding `docs/superpowers/`) is a Makefile target;
    - every `salesfinder <cmd> [<sub>]` in fenced shell blocks resolves to a registered Typer command;
    - `docs/operations.md` contains "Restart the web app and the worker together" and "manual step the operator runs";
    - README links `docs/operations.md` and `docs/architecture.md`.
- [ ] **Step 2: Run** `uv run pytest tests/test_launchd.py tests/test_docs.py tests/test_logging_isolation.py -q` → FAIL.
- [ ] **Step 3: Implement.** Write the example plists with the render function (a one-off `uv run python -c …` writing into `deploy/launchd/`), then write the docs.
- [ ] **Step 4: Run** → PASS; `make check` green.

---

### Task 8: Full verification and smoke

**Files:** none new (fix anything found in its owning module with a test).

- [ ] **Step 1: `make check`** → ruff, format, mypy strict and all tests pass. Report the test count against Phase 6's 2225.
- [ ] **Step 2: Hermetic polluted-env run:**

```sh
SALESFINDER_BIND_HOST=0.0.0.0 SALESFINDER_ALLOWED_HOSTS='["*"]' \
SALESFINDER_PUBLIC_URL=http://evil.example SALESFINDER_HUBSPOT_TOKEN=x \
SALESFINDER_SERPER_API_KEY=y SALESFINDER_EMAIL_VERIFIER_KEY=z \
SALESFINDER_CLAUDE_BIN=/nonexistent SALESFINDER_MAILBOX_PASSWORDS='{"sales_box":"junk"}' \
uv run pytest -q
```

  Expected: all pass (the suite never reads the shell's settings).
- [ ] **Step 3: End-to-end against fakes:** `make e2e` → 3 passed. Then run these guard greps:
  - `grep -rn "IMAP4_SSL(\|aiosmtplib.SMTP(" tests/` shows only `test_imap.py`/`test_smtp.py`;
  - `grep -rn "launchctl\|mkcert\|plutil" src tests` shows no executed calls (docs/Makefile only).
- [ ] **Step 4: Smoke on the dev DB.** Never the real Claude, a real mailbox, Serper, HubSpot or real websites:
  1. Create a temporary `.env` **only if none exists**, with a throwaway `SALESFINDER_SECRET_KEY`, `SALESFINDER_DATABASE_URL=postgresql+asyncpg://localhost/salesfinder` and `SALESFINDER_CLAUDE_BIN=<repo>/tests/fakes/fake_claude`.
  2. `uv run salesfinder db upgrade` → head `0016`.
  3. `uv run salesfinder runs` → the `NEVER` line, *or* the existing health line if the dev DB already has runs.
  4. `uv run salesfinder doctor`: `network` ✓ "Loopback only", `worker` present, `claude_cli` ✓ (the fake). A ✗ on `mailboxes`/`hubspot`/`serper` is expected and fine.
  5. Plists: `uv run salesfinder ops plists --out <scratchpad>/plists --log-dir <scratchpad>/logs`, then `plutil -lint <scratchpad>/plists/*.plist` → OK. This writes **not** into `~/Library/LaunchAgents`, and **no** `launchctl`.
  6. Create a throwaway admin.
  7. Run a scratchpad script (`python -I`, outside the repo). It builds `WorkerDeps` with `fake_mail(FakeSender(), FakeReader({}))`, `FakeResolver()` and `send_enabled=False`, and monkeypatches `_reap`, `_sweep`, `_hubspot`, `_replies`, `_discovery`, `_profiling`, `_contacts` and `_writing` to no-ops (they would touch real services or dev data). Then it runs `start_run` → `run_pass` → `record` → `finish_run("once")`. Expected:
     - no exception;
     - `FakeSender.sent == []`;
     - the lines include the `--no-send` skip line.
  8. Through the app with the throwaway admin:
     - GET `/`, `/runs`, `/admin/settings` → 200; the run appears on `/runs`; Settings shows the Network section and the Export form.
     - POST `/admin/export/companies` `side=all` with CSRF → 200 `text/csv` starting with the BOM and the header. The CSV is not saved to disk.
  9. Cleanup:
     - delete the smoke's `runs` rows (by id), the throwaway admin, its sessions and its audit rows;
     - delete the temporary `.env` if this step created it, and the scratchpad plists/logs;
     - leave the dev DB at head `0016`.
- [ ] **Step 5: Hand back:**
  - `git status --short` (do not commit);
  - the e2e fixes made in Task 6, if any;
  - a reminder that the **real dry run** (`docs/operations.md` §7) and the **first real mailbox** (§6) are manual operator steps not run by this plan.

---

## Self-review notes

- **Spec coverage:**

  | Spec item | Task |
  |---|---|
  | §6.1 heartbeat cadence, quiet hours, "budget left?" (already enforced inside the Claude steps) | T1/T2 |
  | §6.1 `runs` row with what it did, counts, cost and failures | T1/T2/T3 |
  | §5 budget headroom via quiet hours | T2 |
  | §6.7 LAN/HTTPS, Host checks (existing) + warnings, public URL | T4 |
  | §6.7 Today "last run status and any failures" | T3 |
  | §6.7 Runs screen (heartbeat log, failures, Claude usage vs budget, discovery spend) | T3 |
  | §6.7 Settings CSV export | T5 |
  | §3 CSV handoff | T5 |
  | §8 doctor (worker, network, mailboxes) | T3/T4 |
  | §8 launchd example, logs to file | T7 |
  | §8 docs (configuration, sending/deliverability, troubleshooting, architecture) | T7 (operations, architecture; README keeps setup + config) |
  | §9 end-to-end run against fakes | T6/T8 |
  | §9 real dry run with sending paused | T7 runbook (manual) + `--no-send` (T2) + T6 dry-run test |

- **Follow-ups:**

  | Item | Task |
  |---|---|
  | P6 #1 deploy note | T3 (mismatch banner), T7 (docs, `make restart`) |
  | P6 #2 CSV export, excluded + suppressed filtered | T5 |
  | P6 #3 quiet hours vs cadence | Decisions, T2 |
  | P6 #4 first real mailbox | T7 runbook |
  | P6 #5 held mailboxes | T3 |
  | P3 / P5 #9 export filter | T5 |
  | P2 worker heartbeat | T1–T3 |
  | P2 "before onboarding teammates" | T4 |
  | P1 LAN allowed-hosts warning, public URL, invite revoke | T4 |
  | P1 Docs (mkcert mapping, root CA, `createdb`) | T7 |

- **Not taken (still "can wait"):** the Phase 6 can-wait list; Phase 1 CI hardening; the Phase 2 job "Stop" button. None targets Phase 7.
- **Type consistency:**
  - `PassResult` (T1) is used by `run_pass`/`record` (T2), `drive` (T6) and the smoke (T8).
  - `worker_health(db, *, now, config, code_version)` (T3) is used by deps, Today, `/runs`, doctor, the CLI and T6.
  - `record_pass` returns the run id, and `worker.record` stores it in `deps.run_id`.
  - `not_read` (T3) shares its predicate with `poll_mailboxes`.
  - `address_preference` (T5) is the renamed `_preference`.
- **Proportion:** 8 tasks; code appears only as test-name lists and two exact command lines. All copy the tests assert is given verbatim.
