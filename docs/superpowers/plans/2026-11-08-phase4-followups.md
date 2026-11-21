# Phase 4 follow-ups (deferred review items)

From per-task and whole-branch reviews of Phase 4. Earlier lists
(`2026-11-07-phase{1,2,3}-followups.md`) still apply where not done.

## Fix first in Phase 5 (open Important from the final re-review)
- **Dotted-initial names rejected** (`contacts/names.py` ~157): the dot-stripped stop-word lookup added for "Ph.D." also matches short stop words (kk, sa, ag, nv, sr, vp, md…), so "K.K. Sharma", "S.R. Iyer", "V.P. Singh", "M.D. Rao", "K.K. Nair" are no longer people. Apply the dot-stripped lookup only to tokens that aren't initials (or only to qualification forms like phd/msc/mba). Tests for "K.K. Nair / Head of Procurement" card and "S.R. Iyer - Purchase Manager" line.

## Hard requirements for Phases 5–6 (from the final review)
1. **Live exclusion at the gate:** at draft time and again at send time call `exclusion_for(domain=company.domain, email=recipient)` live and check `Company.status`; never trust `blocked_reason`, `sendable_contacts`, or a stored `is_sendable` result alone. `exclusion_for` must fail closed on malformed email (Phase 2 requirement still open).
2. **Fresh MX at the gate:** refuse unless the recipient's domain is `has_mx` with `checked_at` within `mx_cache_days` (refresh via `mx_for`).
3. **Recompute sendability at draft and send** from current rows (cohort counts are a pre-filter only); campaign activation refuses a stale cohort (`cohort_is_stale`).
4. **Recipients only from targets:** only addresses linked to `is_target` people; never unlinked role/free-mail or never-send addresses without an explicit teammate choice; one address per person, picked deterministically: published → verified → pattern_confirmed → overridden guess/risky.
5. **Bounce feedback:** a hard bounce suppresses the address and marks it `invalid`; on a `pattern_confirmed` address also drop the domain's learned pattern and hold its other pattern addresses.
6. **Cross-side dedupe:** the same address can exist on a buyer and a supplier row — enforce §6.4's "already in an active sequence from the other pipeline".
7. **Scraped text is untrusted:** names, titles, snippets go to the writer as delimited data; the writer may use a person's name and title only; run the injection-echo gate check.
8. **One domain form:** `Company.domain` (Unicode), `mx_domain` (IDNA punycode) and `email_validator` forms differ — pick one before sending; decide SMTPUTF8 addresses.
9. **Still open from Phase 3:** greedy US city capture; current-value table for observations before the writer runs.
10. **Record what was in force at send time:** status + override applied when each message was sent.

## Can wait
- Extraction recall: trailing place/region segments ("Branch Manager, Visakhapatnam", "Director, Asia Pacific") and comma "Title, Name" lines now dropped; partners/associates headings skipped; "What people say" not treated as quote heading (`_TEAM_HEADING` has "people"); quote heading without a section skips later team divs; `_Own.owns` prefix match on short labels ("indo", "tata"); own-company segment kept in line titles but stripped in cards; "Mr. Patel (Proprietor)" single token; "Md. Rafiq"; " at <Capitalized>" drops own "Director at Acme"; root `/?page_id=`; owl-carousel teams; detectors still use substring page matching; `contact_form_url` anchors on the first page.
- Contacts service: sweep vs `refresh_blocks` lock order (narrow deadlock, Postgres aborts one); unfiltered `refresh_blocks` after every HubSpot sync locks all companies with addresses; `refresh_blocks` reads ids before locks; misleading "No team…" on partial with failed people pages; allowing a stale address doesn't count toward `sendable_contacts`; person-delete protection lapses after stale-unlinking; verifier "unknown" still uses the cap; cap leaks / paid result discarded / concurrent CLI overshoot (replace with a verifier-calls ledger before enabling MillionVerifier); `email_patterns` shared across sides; IDN join in the contacts claim; verifier may be called during a DNS blip; subdomain addresses use the company domain's MX.
- DNS/verifier: httpx log filter scope (drop vs redact; global); 3× timeout worst case; null MX mixed with real MX; duplicated exception mapping; Resolver construction reads resolv.conf; naive datetimes.
- Breaker: capped flag overcount when excluded meanwhile; stale `profile_holds` on re-queue; outage ≥45 min turns ok companies into no_site for a day.
- Web: audit `old` read before lock; decision validated before 404; weak assertions.
