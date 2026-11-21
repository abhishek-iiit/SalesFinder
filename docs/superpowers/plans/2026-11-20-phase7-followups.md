# Phase 7 follow-ups (deferred review items)

These items came out of the per-task and whole-branch reviews of Phase 7 (loop/ops: heartbeat and runs, the worker loop, worker health, LAN/HTTPS, the CSV export, the end-to-end run against fakes, launchd plists and docs, and verification).

Phase 7 closed with:
- `make check` green: 2652 tests, ruff, format and mypy strict all clean.
- The polluted-env hermetic run green.
- Migration head at `0016`.

Every roadmap phase is now built. The earlier follow-up lists still apply wherever their items aren't done; their "Can wait" sections are unchanged.

## Before going live (operator, manual)
These are written up in `docs/operations.md`. Nothing automated runs them.
1. **Certificates.** Render and load the launchd agents (`make plists`, `make start`). Set up HTTPS with mkcert, and have teammates install `rootCA.pem`, never the key.
2. **The real dry run (§7).** Run it with sending paused and `salesfinder worker --no-send`. Finish by archiving the dry-run campaign, and check that "Approved — waiting to send" is empty.
3. **The first real mailbox (§6).** It must be on the separate sending domain. The app password goes in `.env` as `SALESFINDER_MAILBOX_PASSWORDS`, set in one place only. Then run Test connection, check DNS, run `pgrep -fl "salesfinder worker"` (exactly one worker, with no `--no-send`), and finally resume sending.

## Can wait
- **Health:**
  - There's no "stuck in <step> for N min" banner for a step that hangs while beats continue. `step_started_at` is already stored.
  - A brand-new `--no-send` row reads "Running" until its first pass is recorded, which takes seconds.
  - A rollover whose COMMIT acknowledgement is lost, followed by adoption, can show `NO_SEND_LINE` twice.
- **Domains:** IDN and punycode forms don't match in `normalize_domain`. A do-not-contact entry `müller.de` doesn't match the company `xn--mller-kva.de`. This predates Phase 7 and affects the gate, the sweep and the export alike, so fix it in `domains.py`.
- **Contacts:** real people surnamed "Doe" are dropped by the placeholder-name filter. That's a Phase 4 trade-off.
- **Export:** the whole CSV is built in memory before it's sent, because the row count is audited first. Stream it if the company table grows past tens of thousands of rows.
- **Discovery:** a job stopped by a worker shutdown ends `failed` rather than being re-queued. The searches already paid for are kept.
- **Makefile:**
  - `make logs` and the crash-log tidy follow only the default `~/Library/Logs/SalesFinder`, so a custom `ops plists --log-dir` must be handled by hand.
  - Paths containing spaces aren't quoted in the crash-log file list.
- **Tests:**
  - A few test modules still read the repo `config/` catalog and facts at import. Those files have no `.local` merge, so it's harmless.
  - `test_detect_supplier.py::test_exports_linear_time` is a wall-clock check. Its bound was widened to 5 s after it flaked under load.

## Rulings made in Phase 7
P7-A to P7-J are recorded in the Phase 7 SDD ledger and summarised in the final hand-off message. The ones that matter for future work:
- **P7-B/C:** never run two workers, and never run unlocked. A worker that sees another worker's open run, or that loses its advisory lock, exits 1 so launchd restarts it.
- **P7-E:** the export drops a person if any of their linked addresses is excluded, suppressed or on do-not-contact.
- **P7-F/I:** logs rotate in process and are mode 600. Third-party HTTP loggers are held at WARNING so scraped URLs and server text never reach a log.
- **P7-G/H:** on SIGTERM, a pass starts nothing new and long steps return early. The worker's ExitTimeOut is 600 s, and `brain.timeout_seconds` must be under 135 to fit within it. `make restart` is stop, then start.
- **P7-J:** a `--no-send` worker shows as "Running with --no-send" and makes `doctor` fail.
