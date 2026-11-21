# SalesFinder Roadmap (phase plans)

Spec: `docs/superpowers/specs/2026-11-07-elchemy-salesfinder-design.md`

The spec spans several subsystems, so it is built as seven phase plans. Each
phase ends with working, tested software and its own plan file. A phase's plan
is written when the previous phase is done, against the code that exists.

| Phase | Plan file | Delivers | Spec milestones |
|---|---|---|---|
| 1 | `2026-11-07-phase1-foundation.md` | Project tooling, settings + YAML config, Postgres + Alembic, users/sessions/invites/roles, audit log, dashboard shell with login, CLI, `doctor`, CI | 1, 2 (auth part) |
| 2 | phase2-exclusions-discovery | Catalog + facts YAML, HubSpot read-only sync → `known_relationships`, do-not-contact, Serper discovery jobs with estimate/confirm/monthly budget, junk filter, Companies page | 2 (HubSpot), 3 |
| 3 | phase3-profiling-signals | Free HTTP profiler, buyer + supplier signal detectors as observations, signal confirmation, scoring, cohorts with live counts | 4 |
| 4 | phase4-people-email | People finder from own pages, email pattern learning, MX checks, statuses per §6.2.1, optional paid verifier | 5 |
| 5 | phase5-brain-writer-outbox | Isolated Claude CLI brain + budget, campaigns, writer (buyer/supplier playbooks), deterministic gate, outbox approve/edit/reject with feedback | 6, 7 |
| 6 | phase6-sending-replies | Mailboxes, DNS readiness, warm-up ramp, SMTP sending in office hours, follow-ups, bounce kill switch, IMAP replies, Sent-folder takeover, reply classification/drafts, suppressions, Conversations page | 8 |
| 7 | phase7-loop-ops | Heartbeat worker, Today screen data, LAN/HTTPS run mode, launchd plists, docs, end-to-end run against fakes, real dry run with sending paused | 9 |

Commits: the user commits and pushes. Plans contain no commit steps.
