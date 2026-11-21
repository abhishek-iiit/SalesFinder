# Phase 3 follow-ups (deferred review items)

From per-task and whole-branch reviews of Phase 3. Earlier lists
(`2026-11-07-phase1-followups.md`, `2026-11-07-phase2-followups.md`) still apply.

## Fix first in Phase 4 (open Important from the final re-review)
- **Network-outage breaker can hold dead sites forever** (`profiling/service.py` ~586-596). If ≥3 never-read (pending) or stale-ok companies whose domains are genuinely dead make up a whole batch, the breaker fires every 15 min indefinitely: they never get recorded as `no_site`, never back off, and the worker prints "Network looks down — will retry." forever. Fix: cap consecutive holds per company (record the failure after 2–3 holds; track a hold counter), or require independent evidence of an outage (a canary request, or a successful read in the last N passes). Test the dead-sites case.

## Hard requirements for later phases
- **Phase 4:** reuse `SafeFetcher` (one per batch — robots cache lives on the instance); add team/leadership/management page groups carefully (current `pick_pages` keywords are loose: "range" in "orange", query-string links collapse). Consider storing fetched page text, or folding people-finding into the same crawl, so sites aren't read twice. `PageText.links` keeps `mailto:` hrefs (useful for `published` addresses).
- **Phase 4:** MX lookups need a bounded async resolver with timeouts — not `getaddrinfo` in the default executor.
- **Phase 4/5:** observations grow quickly with contact data; before Phase 5 add a "current value" table or `is_latest` flag instead of DISTINCT ON over the whole table; on `/cohorts` load latest once per side per request.
- **Phase 5:** campaign activation must refuse a stale cohort (`cohorts.evaluate.cohort_is_stale`).
- **Before Phase 5 writing:** greedy US city capture ("Main St Houston") must be fixed — the writer may quote the city.
- **Phase 6 export:** filter excluded companies (they still carry stored scores).

## Can wait
- Detectors: "Dealer locator" counts as trader; careers detection loose (/careers-advice, roles from nav text); capacity ranges/marketing tonnage; "99 MTPA" read as 99 million t/yr (cut-off 100 may be better at ~10); generic catalog terms ("silica gel"); blog-post location accepted; fullwidth dashes; industry regexes not cached; `detect_us_location` runs twice; ~1.5 s per 2 MB detector cost (capped at 200k chars).
- Profiling: partial run can leave NEAR_WAREHOUSE out of step with a new US_LOCATION; per-company deadline on a slow-but-up site counts as "unreachable" for the breaker; unreachable failures written at batch end (crash window); `mark_side_due` before the exclusive scoring lock; rescore vs sweep possible deadlock (Postgres aborts one); threads not cancellable; reprofile during an in-flight profile can double-crawl; supplier end-to-end test via real detectors.
- Fetcher: resolver executor exhaustion with many slow lookups; raw deflate advertised but unsupported; unparseable URL reported as "scheme"; overall-timeout result URL keeps userinfo.
- Cohorts: name uniqueness case-insensitive in the form but case-sensitive in the DB (add a `lower(name)` unique index); error shown twice on parse failure; crafted conditions on unconfirmed signals accepted (needed for carry-through); C1/bidi characters in names; unreadable stored definition shows an everyone-count; unused csrf meta tag; silent 403 on live count after session rotation; full re-evaluation per count.
- Pages: company detail shows an excluded company's stored score; "Newer/Older" pager labels under sort=score; synchronous rescore in the request; `format_value` overflow on absurd ints; route-to-route import of `PROFILE_LABELS`.
- Tests: brittle `PROFILE_STATUSES` parsing; hardcoded signal-state drift list; brittle sort assertion; no explicit `matches()` hiring-False test.
- Config: Warehouse lat/lon unbounded; `confirm_free` N upserts bypass the identity map.
