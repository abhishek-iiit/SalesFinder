# Phase 2 follow-ups (deferred review items)

Collected from per-task and whole-branch reviews of Phase 2. Phase 1's list
(`2026-11-07-phase1-followups.md`) still applies where not done.

## Hard requirements for later phases
- **Phase 3:** new company statuses must stay compatible with the exclusion sweep (`exclusions/sweep.py` acts on every `status <> 'excluded'`). Never write a status that hides a company from the sweep.
- **Phase 3:** settle whether `business.site` and similar free-host suffixes (not in the bundled PSL) should be junk-filtered before more data is keyed on `companies.domain`.
- **Phase 5/6 send gate:** always re-run `exclusion_for(domain=company.domain, email=recipient)` at send time — never trust `Company.status` alone — and refuse cold sends unless `sync_health(...).state == "fresh"`.
- **Phase 5/6:** `exclusion_for` must fail closed on malformed email input (today an email-only call with garbage returns `None`).
- **Phase 5/6:** IDN vs punycode normalize differently — normalize to one form before sending is possible.
- **Before onboarding teammates:** invite revocation, `SALESFINDER_PUBLIC_URL`, LAN allowed-hosts warning (from Phase 1 list).

## Discovery / budget (can wait)
- Show dropped URLs and reasons on the job page (today only a count); the `news` prefix rule drops names like `newstarchem.com`.
- A running job can't be stopped; Ctrl-C finishes the current job. Add a "Stop" that sets the status.
- Store `price_per_1000_usd` on the job at estimate time (web and worker could disagree).
- No sign when the worker isn't running (approved jobs wait forever, holding budget) — add a worker heartbeat (Phase 7 `runs`).
- Approval during a run counts the running job at spend-so-far; count it at max(actual, estimate).
- Limit message says "searches" but counts queries; approve-message rounding mismatch; job crossing a UTC month counts to its start month (document).
- `CancelledError` branch in `run_job` untested.

## HubSpot / exclusions (can wait)
- Empty-result guard only blocks a total wipe; consider a ratio guard with a documented override.
- `hubspot sync` CLI check-then-request isn't atomic; worker lock is lost silently if its connection drops.
- Health message shows interrupted/cancelled syncs as "latest attempt failed".
- Worker reaper and sweep share one transaction — split so one can't roll back the other.
- `exclusion_for` runs up to 6 sequential queries per miss; batch.
- DNC on a free-mail domain (e.g. `gmail.com`) blocks every address — warn/refuse in `add_entry`; DNC add race → IntegrityError 500.
- Worker step-failure lines include exception text (make type-only like jobs.py); `_signed_in` swallows exceptions on error pages without logging.

## Code quality / tests (can wait)
- Catalog ConfigError dumps whole input on model-level errors; private `_read_yaml` import; duplicated `Side` literal; avoidable `type: ignore`.
- Whitespace in a URL path → `no_domain`; `extra_domains` set rebuilt per call.
- Test gaps: health/recent-syncs escaping on admin page; budget audit old/new values; email-DNC leaves statuses unchanged; signal wiring in `run_forever`; worker broken-config CLI test; weak `"7" in body` assertion; round-trip test engine disposal.
- Unused `status_labels` in `discover.html`; Cancel form inside the polling element.
