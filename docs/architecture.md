# Architecture

How SalesFinder fits together: its processes, how data moves through them, the worker's pass, the code's modules, the safety rails, the lock order and where state lives. For running it, see [operations.md](operations.md). For using it, see the [README](../README.md). The design spec is [docs/superpowers/specs/2026-11-07-elchemy-salesfinder-design.md](superpowers/specs/2026-11-07-elchemy-salesfinder-design.md).

## Components

```
 teammates' browsers ──HTTPS (LAN, mkcert)──▶ salesfinder web ──┐
                                                                 ├──▶ Postgres 17 (all state)
 launchd ──keeps running──▶ salesfinder worker ──────────────────┘
                                 │
                                 ├─ claude -p (isolated; the operator's subscription login)
                                 ├─ Serper (search), company websites (profiling), DNS (MX, SPF/DKIM/DMARC)
                                 ├─ HubSpot (read-only GETs), MillionVerifier (optional)
                                 └─ Gmail SMTP (send) and IMAP (read replies, Sent folder)
```

- **Two processes, one database.** `salesfinder web` (FastAPI, Jinja, uvicorn) serves the dashboard and records teammates' decisions. `salesfinder worker` does all the background work. Neither talks to the other directly: they share only Postgres. Both read `.env` and `config/*.yaml` only at start. They must run the same code, so they're always restarted together. Each stores a code-version fingerprint (a hash of the package's `*.py` and `*.md` files): the worker writes it on its `runs` row, and the web app shows a banner when it differs from its own.
- **One worker at a time.** The worker takes a session-level Postgres advisory lock (`pg_try_advisory_lock`) on a dedicated connection for its whole life. A second worker, `--once` included, prints "Another SalesFinder worker is already running." and exits 1. A unique partial index allows only one open `runs` row. The worker checks the lock with every beat. If Postgres restarted and the lock went with the connection, the worker finishes its pass and exits 1, and launchd restarts it under a fresh lock: it never runs unlocked.
- **The CLI** (`salesfinder …`) runs one-off versions of the same services against the same database: admin users, doctor, a single worker pass, HubSpot sync, profiling, contacts, drafting, sending pause/resume/status, the runs log and `ops plists`.
- **Claude** is the Claude Code CLI run as `claude -p` with the prompt on stdin, in a fresh empty folder, with no tools, settings, hooks, plugins, MCP servers or CLAUDE.md (`brain/`). It uses the operator's own login, never an API key. Every call counts against `usage.max_daily_claude_calls`.
- **launchd** keeps both processes running as LaunchAgents of the operator's user (`ops/launchd.py`, `deploy/launchd/`). Each writes its own log file (`SALESFINDER_LOG_FILE`, through a `WatchedFileHandler`, so newsyslog can rotate it), and launchd's output files catch only crash output.

## Data flow

1. **Discovery.** A teammate creates a search on Discover, sees how many searches it takes and their cost, and approves it within the monthly budget. The worker runs the approved job against Serper, filters junk and stores **companies**. Each company is checked against the do-not-contact list and the HubSpot mirror (`known_relationships`), so existing customers and suppliers are excluded at once.
2. **Profiling.** The worker reads each company's own website (robots.txt, page and size limits, no private addresses), records **observations** for the confirmed signals only, and scores the company.
3. **Contacts.** People with job titles and published addresses come from the same pages. The worker learns the domain's address pattern, looks up MX records (never SMTP), picks up to two **targets** and marks each address sendable or not.
4. **Cohorts and campaigns.** A cohort is a saved filter over the latest signals. A campaign (side, cohort, playbook, daily cap) is activated by a teammate.
5. **Writing.** For active campaigns the worker picks companies with a sendable target and asks Claude for a first email and one follow-up. Claude sees confirmed facts and a marked data block only. Code adds the signature and the compliance footer. The **gate** checks the draft: it waits in the **Outbox** as *ready*, or as *held by the gate* with reasons.
6. **Approval.** A teammate approves (the gate runs again on live data), edits (re-gated, never auto-approved) or rejects with a reason. The reason is fed back to Claude.
7. **Sending.** The worker sends approved email from the campaign's mailbox once sending is resumed. It sends at most one email per mailbox per pass and respects warm-up, daily caps, office hours, the 3–9 minute spacing, HubSpot freshness and DNS readiness. The gate runs once more on the exact stored email just before it goes. A send whose outcome is unknown is settled from the Sent folder, never retried blindly.
8. **Replies.** The worker reads INBOX and Sent over IMAP:
   - bounces and unsubscribes become **suppressions** and stop sequences;
   - a teammate's own reply from Gmail takes the conversation over;
   - other replies cancel the follow-up, and Claude classifies them and drafts an answer into the Outbox for approval;
   - conversations that need a person are flagged on Today and Conversations.
9. **Follow-ups.** When a follow-up falls due it is gated and queued for review like a first email.
10. **Hand-off.** Admins can export a companies CSV from Settings. The export drops excluded, disqualified, suppressed and do-not-contact companies and people, and guards cells against formula injection.

## The worker's pass

The worker runs a pass every `worker.poll_seconds` (10 s). That tick is fast because sending sends one email per mailbox per pass. Each step is isolated: a failure is logged as "<Step> step failed: <Type>", and the next step still runs. In order:

| # | Step | Does | Own cadence |
| --- | --- | --- | --- |
| 1 | Reaper | Fails discovery jobs and HubSpot syncs left running by a crashed worker | every pass |
| 2 | Sweep | Re-excludes stored companies that the do-not-contact list or HubSpot now covers, and stops their sequences | every pass |
| 3 | HubSpot | Read-only sync when due | `hubspot.sync_interval_hours` (24 h), backing off from 5 min after a failure |
| 4 | Mail | Reads each tested mailbox's INBOX and Sent: bounces, unsubscribes, takeovers, replies | `mail.poll_interval_minutes` (5 min) |
| 5 | Replies | Claude classifies new replies and drafts answers into the Outbox | every pass, **waits in quiet hours** |
| 6 | Sending | Settles unclear sends, sends approved email (one per mailbox), kill switch | every pass; skipped entirely with `--no-send` |
| 7 | Follow-ups | Gates due follow-ups into To review | every pass |
| 8 | Discovery | Runs one teammate-approved job | every pass |
| 9 | Profiling | Profiles up to `profiling.batch_size` companies | every pass |
| 10 | Contacts | Checks up to `contacts.batch_size` companies' contacts | every pass |
| 11 | Writing | Claude drafts for active campaigns, up to `writing.batch_size` companies | `writing.min_interval_minutes` (5 min), **waits in quiet hours** |

The spec's loop "wake → quiet hours? → budget left? → decide → act → log → sleep" maps onto this: quiet hours gate the Claude steps, the daily Claude budget gates Replies and Writing, each step decides and acts, the pass is recorded, and the worker sleeps until the next tick.

**Quiet hours** (`usage.quiet_hours`, default 22:00–07:00 America/New_York; `end` exclusive; `start == end` means none) stop only Replies and Writing. Everything else must keep running: opt-outs are honoured at once, and suppliers' office hours (IST) fall inside the default window.

**The heartbeat log.** One `runs` row covers `usage.heartbeat_interval_minutes` (15 min) of passes. It holds the pass count, failed steps, log lines (our own log-safe text only, at most `ops.max_run_lines`), failures, whether it was quiet, Claude calls and cost, and the code version. A background **beat** refreshes `last_beat_at` and the current step every `ops.beat_seconds` (30 s), so a long step never looks dead. After `ops.worker_stale_seconds` (180 s) without a beat, the pages say the worker stopped responding. On SIGTERM (`make stop`) the worker finishes its pass and closes the row as "shutdown". From the signal on, Replies, Writing and Discovery start no new item (a Claude call already running finishes, and a running discovery job stops before its next search), and HubSpot, Profiling and Contacts don't start (they are due-based and run after the next start). This keeps a planned stop well inside launchd's 600-second `ExitTimeOut` (`brain.timeout_seconds` is held under 135 s so that four Claude calls and a margin fit), so the worker is never killed mid-pass and never leaves a `claude -p` child behind. A cancelled Claude call kills its process group in any case. A worker that lost its database lock closes its row with that line, and the pages say so. A `--no-send` worker's rows say so too (each new row starts with that line), and the pages read "Running with --no-send (nothing will be sent)". A worker that died leaves the row open, and the next start closes it as "crashed". Rows that finished more than `ops.runs_keep_days` (30) ago are pruned. A failed `runs` write never stops the worker.

## Module map

```
src/salesfinder/
  cli.py, worker.py        the CLI and the worker loop (run_pass, run_forever, beats, the lock)
  settings.py, config.py   .env settings (secrets, LAN), YAML config; catalog.py: catalog/facts
  logging.py               structlog setup: stderr, or a log file reopened after rotation
  domains.py               domain normalization (offline public-suffix list)
  industries.py            industry keys and pipeline sides
  hosts.py                 host-name matching shared by the web app's Origin check and LAN checks
  web/                     FastAPI app, routes, templates, security (sessions, CSRF, hosts)
  auth/                    passwords, sessions, invites, users, rate limiting
  db/                      engine, models, Alembic migrations
  discovery/               queries, Serper client, junk filter, budget, jobs
  exclusions/              do-not-contact, exclusion checks and sweep, HubSpot client and sync
  profiling/, signals/     safe fetcher, page picking, detectors, scoring; signal catalog
  contacts/                names, titles, address patterns, MX checks, verifier
  cohorts/, campaigns/     cohort language and evaluation; campaign lifecycle and drafting
  brain/, writer/          the isolated Claude runtime; what Claude sees, prompts, assembly
  gate/                    the pre-send gate
  outbox/                  approve, re-check, edit, reject, withdraw, resend
  outreach/                the one lock order; stopping sequences
  mail/, sending/          MIME, SMTP, IMAP, DNS readiness; sending state, policy, mailboxes, sender
  replies/, followups/     mailbox polling, ingest, classification, conversations; follow-up queue
  suppressions/            bounced, unsubscribed and manual suppressions
  ops/                     runs (heartbeat log), health, quiet hours, code version, LAN checks, launchd
  export.py                the companies CSV
  audit.py, doctor.py      audit log; environment self-checks
```

## Safety rails

- **Sending is paused by default.** A new install says "Sending hasn't been turned on yet." Only an admin resumes it, and a resume is refused if the state changed after the page was read. Any teammate can pause.
- **Kill switch.** More than 5% bounces in the last 50 sends (counted from the latest resume) pauses sending automatically.
- **Nothing unapproved is sent.** Every first email, follow-up and reply answer waits in the Outbox for a teammate's approval. The worker never starts discovery on its own either: only approved jobs run.
- **The gate, twice more.** Every draft is gated when written. Approval re-runs the gate on live data, and the sender runs it again on the exact stored email just before it goes. The gate checks the content (length, banned phrases, prices, unconfirmed claims, links, placeholders, signature and footer exactly as code wrote them) and the recipient (not excluded, disqualified, suppressed or on do-not-contact, still sendable, MX fresh, not in another campaign).
- **Suppressions and exclusions are live.** Bounces and unsubscribes suppress an address at once, and an unsubscribe can never be removed. Do-not-contact and HubSpot exclusions are re-checked live before every send and by the Sweep each pass.
- **Mailbox guards.** Never elchemy.com. A new or changed mailbox is held until **Test connection** passes, and nothing logs in with its password before then. DNS (SPF, DKIM, DMARC, MX) must be fresh. Warm-up, daily caps, office hours and spacing apply.
- **No SMTP probing**, ever: addresses are judged from websites, patterns and MX records only.
- **Untrusted text stays data.** Scraped values and reply text reach Claude only inside a marked data block it is told never to follow. They never go into logs or `runs` lines: third-party loggers that would log URLs or server text (httpx, httpcore and similar) are held at WARNING, and tracebacks never show local variables. Pages render with Jinja autoescape, and the CSV export guards formula-leading cells.
- **Secrets** (secret key, mailbox app passwords, HubSpot, Serper and verifier keys) live only in `.env`. They never appear in the database, logs, pages, audit rows, CSV exports or the launchd plists. The database stores a mailbox's password *name* only.
- **Web.** HTTPS is required off loopback. Host checks (`SALESFINDER_ALLOWED_HOSTS`) guard against DNS rebinding. Every POST is CSRF-checked, and every state change is audited.

## Lock order

Everything that changes a drafted sequence takes locks in one order, so no two paths can deadlock:

1. the sequence's messages (`FOR UPDATE`, by id);
2. the sequence;
3. the per-recipient advisory lock (`pg_advisory_xact_lock(RECIPIENT_LOCK_NS, hashtext(address))`);
4. the mailbox row;
5. then the conversation.

Campaign archive locks the campaign first, then messages, then sequences, which agrees with this order. `app_settings` is only taken after these or alone. `runs` rows are written in their own short transactions, never while an outreach lock is held. No transaction is held open across network, subprocess, DNS, SMTP or IMAP I/O. Services flush, and their callers commit.

## Where state lives

All state is in Postgres (`salesfinder`):

- **`app_settings`**: small key/value settings changed at runtime:
  - `sending`: paused or on, the reason, when and by whom it changed, and the bounce window's start;
  - `discovery.monthly_budget_usd`: the admin's budget override.
- **`runs`**: the worker's heartbeat log, one row per window, and the source of the banner, the Today card, `/runs`, `salesfinder runs`, doctor's `worker` check and `/healthz`.
- **`audit_log`**: who did what and when, for every state change by a person or the system: approvals, edits, rejects, pauses, resumes, invites, exports, suppressions and so on. It holds ids, addresses and counts only, never email text or secrets. Worker housekeeping and `runs` writes aren't audited: `runs` is itself the operational log.
- **`usage_events`**: every Claude call, for the daily budget and the cost shown on Runs.
- The pipeline's tables: `companies`, `discovery_jobs`, `discovery_drops`, `known_relationships`, `hubspot_syncs`, `do_not_contact`, `observations`, `signal_settings`, `people`, `email_addresses`, `email_patterns`, `domain_mx`, `cohorts`, `campaigns`, `sequences`, `messages`, `outbox_feedback`, `draft_failures`, `mailboxes`, `mailbox_folders`, `mailbox_messages`, `conversations`, `suppressions`, `users`, `user_sessions`, `invites`.

Outside the database: `.env` (secrets and LAN settings), `config/` (YAML: app config, catalog, the facts Claude may use), the logs in `~/Library/Logs/SalesFinder/`, and the mkcert CA and certificates on the Mac.
