# SalesFinder Phase 3: Profiling and Signals Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every discovered company is read from its own website (free, no model calls) for the buying signals the team has confirmed, each finding is stored as an observation with evidence, companies get a deterministic 0–100 fit score, and teammates build saved cohorts (signal queries with live counts) that later campaigns target.

**Architecture:** A safe fetcher (SSRF-guarded, robots-aware, size/time-limited, same-site redirects) collects up to a handful of pages per company. Pure detector functions turn page text into signal values with evidence. A profiling service writes observations and a score; the worker profiles a small batch each pass. Signal catalog lives in code; which signals are confirmed lives in the DB. Cohorts are a small JSON condition language evaluated in Python over each company's latest observations.

**Tech Stack:** Phase 1–2 stack, plus `selectolax` (HTML parsing).

**Spec:** `docs/superpowers/specs/2026-11-07-elchemy-salesfinder-design.md` — §6.2 Profile + Score stages, §6.3 signals and cohorts, §6.7 Signals & cohorts screen, §4 anti-goals (silent failure, unexplainable lists). Follow-ups folded in are marked *(follow-up)*: `docs/superpowers/plans/2026-11-07-phase2-followups.md`.

## Global Constraints

- **Never run `git commit`, `git push`, `git add`, or create branches.** The user commits.
- All Phase 1–2 constraints hold (uv/Python 3.13, UUID PKs, UTC timestamptz, ruff 100, mypy strict, services flush/callers commit, worker-job functions own their transactions, no network in tests, tests never touch the dev DB, hermetic config/env in tests).
- **No model calls in this phase.** Everything is deterministic code.
- **Signals are collected only when confirmed.** A detector whose signal is not `confirmed` does not run and writes nothing.
- **Fetching is polite and safe:** only `http`/`https`; never connect to private, loopback, link-local, multicast, reserved, or unspecified addresses (check every resolved IP, on every redirect hop); obey `robots.txt` for the configured user agent; ≤ `max_pages_per_company` pages; ≤ `max_page_bytes` per response (stop reading beyond it); `page_timeout_seconds` per request; redirects only within the same registrable domain (≤ 3 hops); only `text/html` / `application/xhtml+xml` bodies are parsed.
- Page text is untrusted: stored evidence snippets are plain text ≤ 300 chars, always autoescaped when shown.
- Company `status` stays exactly `new`/`excluded` — profiling state lives in new columns, so the exclusion sweep (`status <> 'excluded'`) keeps working *(follow-up)*. Excluded companies are never profiled.
- Every failure is a finding: a company that can't be profiled gets `profile_status` + a human-readable `profile_error`; a signal with no evidence is stored as an explicit negative observation (e.g. `{"found": false}`), never silently skipped.

## Review Focus

1. **A search result that points at an internal address** (`http://127.0.0.1`, `http://10.0.0.5`, a public hostname that resolves to `192.168.x.x`, or a redirect to one) must never be fetched; the company ends `profile_status="blocked"` with the reason → tests in Task 2.
2. **Hostile or broken sites** — huge pages, endless redirects, non-HTML downloads, slow responses, `robots.txt` disallowing everything, 403/500 — end with a clear `profile_status`/`profile_error` and never hang the worker → tests in Tasks 2 and 5.
3. **Chemical and certification mentions in odd forms** — `Sodium Laureth Sulphate`, `SLES-70`, `CAS No. 68585-34-2`, `ISO 9001:2015`, `cGMP`, `Halal-certified` — are detected; look-alikes (`ISO 9001` inside a phone number, `SLES` inside `SLESSOR`) are not → tests in Tasks 3 and 4.
4. **Changing which signals are confirmed** — confirming a new one later, rejecting one that already has observations — re-profiles or rescoring behaves predictably: scores only count confirmed signals, and old observations of a rejected signal stop affecting scores and cohorts → tests in Tasks 5 and 6.
5. **Cohort conditions that reference a signal a company has never been observed for** match "not present" (not an error), and excluded companies never appear in a cohort → tests in Task 6.

---

## File Structure

```
config/salesfinder.yaml           # + profiling, scoring sections
config/facts.yaml                 # + lat/lon on warehouses
src/salesfinder/
  domains.py                      # + EXTRA_PRIVATE_SUFFIXES (follow-up)
  config.py                       # + ProfilingConfig, ScoringConfig
  catalog.py                      # Warehouse gains lat/lon
  signals/
    catalog.py                    # SignalDef, SIGNALS (code catalog)
    settings.py                   # get/set signal states, confirmed_codes
    geo.py                        # STATE_CENTROIDS, nearest_warehouse
    text.py                       # PageText, extract(html) -> PageText, snippet()
    detect_common.py              # chemicals, industries, certifications, location, region
    detect_buyer.py               # company type, hiring
    detect_supplier.py            # manufacturer/trader, exports, capacity
    run.py                        # run_detectors(pages, company, catalog, config, codes) -> list[Finding]
  profiling/
    fetch.py                      # SafeFetcher, FetchResult, FetchBlocked
    pages.py                      # pick_pages(home_html, base_url) -> list[url]
    service.py                    # profile_company, profile_batch, rescore
    scoring.py                    # score_company(findings, side, config) -> ScoreResult
  cohorts/
    model.py                      # Condition, CohortDefinition (pydantic)
    evaluate.py                   # matches(), count_matches(), list_matches()
  db/models/signals.py            # SignalSetting, Observation, Cohort
  db/migrations/versions/0004_profiling_signals.py
  worker.py                       # + profiling step; sweep split from reaper (follow-up)
  cli.py                          # + signals, profile commands
  web/routes/signals.py, web/routes/cohorts.py, web/routes/companies.py (+score, signals)
  web/templates/signals.html, cohorts.html, cohort_form.html, cohort.html, _cohort_count.html, company.html, companies.html
tests/
  fakes/web.py                    # FakeWeb: MockTransport serving canned sites + fake resolver
  fixtures/sites/*.html           # small hand-written pages
  test_fetch.py, test_pages.py, test_text.py, test_detect_common.py, test_detect_buyer.py,
  test_detect_supplier.py, test_geo.py, test_signal_settings.py, test_profiling.py,
  test_scoring.py, test_cohorts.py, test_web_signals.py, test_web_cohorts.py, test_migrations.py
```

---

### Task 1: Data model, signal catalog, signal settings, config

**Files:**
- Create: `src/salesfinder/signals/__init__.py`, `signals/catalog.py`, `signals/settings.py`, `src/salesfinder/db/models/signals.py`, `db/migrations/versions/0004_profiling_signals.py`
- Modify: `db/models/companies.py`, `db/models/__init__.py`, `config.py`, `config/salesfinder.yaml`, `catalog.py`, `config/facts.yaml`, `audit.py`, `domains.py`
- Test: `tests/test_signal_settings.py`, `tests/test_migrations.py`, `tests/test_config.py`, `tests/test_catalog.py`, `tests/test_domains.py`

**Interfaces:**
- Produces:
  - `signals.catalog.SignalDef` (frozen dataclass): `code: str`, `side: Literal["buyer","supplier"]`, `label: str`, `description: str`, `value_kind: Literal["list","enum","bool","object","text"]`, `cost: str = "free"`.
  - `signals.catalog.SIGNALS: tuple[SignalDef, ...]` — exactly these codes (buyer): `INDUSTRY_MATCH` (list), `USES_CATALOG_CHEMICAL` (list), `COMPANY_TYPE` (enum), `US_LOCATION` (object), `NEAR_WAREHOUSE` (object), `HIRING_PROCUREMENT` (object), `QUALITY_CERTS` (list); (supplier): `MAKES_CATALOG_CHEMICAL` (list), `MANUFACTURER_NOT_TRADER` (enum), `EXPORTS` (bool), `EXPORTS_TO_US` (bool), `SUPPLIER_CERTS` (list), `STATED_CAPACITY` (object), `REGION` (text). Labels/descriptions in plain language (one sentence each, written for a sales teammate). `by_code(code) -> SignalDef` (KeyError), `for_side(side)`.
  - Models (migration `0004`):
    - `SignalSetting` → `signal_settings`: `code: str(64)` PK, `state: str(16)` (check `IN ('proposed','confirmed','skipped','rejected')`), `updated_by_id` FK users nullable, `updated_at` (server now, onupdate).
    - `Observation` → `observations` (UUIDPk): `company_id` FK companies `ON DELETE CASCADE`, `code: str(64)`, `value: JSONB`, `evidence_url: str(2048) | None`, `evidence_snippet: str(300) | None`, `observed_at: timestamptz` (server now), `source: str(16)` = `profiler`. Index `ix_observations_company_id_code_observed_at` on `(company_id, code, observed_at)`.
    - `Cohort` → `cohorts` (UUIDPk + Timestamps): `name: str(120)` unique, `side` (check buyer/supplier), `definition: JSONB`, `created_by_id` FK users.
    - `Company` gains: `profile_status: str(16)` default/server default `pending` (check `IN ('pending','ok','partial','failed','blocked','no_site')`), `profile_error: str(300) | None`, `profiled_at: timestamptz | None`, `pages_fetched: int` default 0, `score: int | None`, `disqualified: bool` default false, `scored_at: timestamptz | None`. Index on `(side, score)`.
  - `signals.settings`: `async def states(db) -> dict[str, str]` (every code in `SIGNALS`; missing rows → `"proposed"`); `async def set_state(db, code: str, state: str, *, actor: User | None) -> None` (validates code/state; upsert; flush); `async def confirmed_codes(db, side: str | None = None) -> frozenset[str]`; `async def confirm_free(db, *, actor) -> int` (confirms every `cost == "free"` signal; returns how many changed).
  - Audit constants: `SIGNAL_STATE_CHANGED = "signal.state_changed"`, `COHORT_CREATED = "cohort.created"`, `COHORT_UPDATED = "cohort.updated"`, `COHORT_DELETED = "cohort.deleted"`, `COMPANY_REPROFILE_REQUESTED = "company.reprofile_requested"`.
  - `config.ProfilingConfig`: `batch_size: int = 10` (≥1), `concurrency: int = 4` (1–16), `max_pages_per_company: int = 6` (1–20), `page_timeout_seconds: float = 10` (>0), `max_page_bytes: int = 2_000_000` (≥10_000), `refresh_after_days: int = 30` (≥1), `user_agent: str = "SalesFinderBot/0.1 (+https://elchemy.com)"`.
  - `config.ScoringConfig`: `near_warehouse_miles: int = 500`, `buyer: dict[str, int]`, `supplier: dict[str, int]` with defaults below (keys must be signal codes of that side or the documented pseudo-keys; values 0–100); `AppConfig` gains `profiling`, `scoring`.
    - buyer: `USES_CATALOG_CHEMICAL: 30`, `INDUSTRY_MATCH: 15`, `COMPANY_TYPE_PRODUCER: 20` (manufacturer / contract_manufacturer / private_label), `COMPANY_TYPE_DISTRIBUTOR: 10`, `US_LOCATION: 5`, `NEAR_WAREHOUSE: 10`, `HIRING_PROCUREMENT: 10`, `QUALITY_CERTS: 10`.
    - supplier: `MAKES_CATALOG_CHEMICAL: 35`, `MANUFACTURER: 20`, `EXPORTS: 10`, `EXPORTS_TO_US: 10`, `SUPPLIER_CERTS: 15`, `STATED_CAPACITY: 10`.
  - `catalog.Warehouse` gains `label: str`, `lat: float`, `lon: float`; `facts.yaml` values: Bridgewater NJ (label "Bridgewater, NJ", 40.59, -74.62), Houston TX ("Houston, TX", 29.76, -95.37), Illinois ("Illinois", 41.88, -87.63), Santa Fe Springs CA ("Santa Fe Springs, CA", 33.94, -118.08).
  - *(follow-up)* `domains.EXTRA_PRIVATE_SUFFIXES = ("business.site", "godaddysites.com", "square.site", "jimdosite.com", "weebly.com", "webflow.io")` — `normalize_domain` treats a host ending in one of these (and not equal to it) as having that suffix, so `acme.business.site` is its own registrable domain. Skip any already handled by the bundled PSL (verify; keep the tuple for the rest).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_signal_settings.py
async def test_defaults_proposed(db): ...       # states(db) has all 14 codes, all "proposed"; confirmed_codes(db) == frozenset()
async def test_set_and_filter_by_side(db, admin): ...
    # set_state("SUPPLIER_CERTS","confirmed"), set_state("QUALITY_CERTS","confirmed")
    # confirmed_codes(db, "buyer") == {"QUALITY_CERTS"}
async def test_invalid_code_or_state(db, admin): ...  # ValueError for "NOPE" and state "maybe"
async def test_confirm_free(db, admin): ...      # returns 14; second call returns 0
```

Extend `tests/test_migrations.py` (round trip now ends at `"0004"`; observation cascade on company delete; `profile_status` check rejects `"bogus"`; cohort name unique). Extend `tests/test_config.py` (shipped scoring defaults load; unknown scoring key → `ConfigError` naming it; `concurrency: 0` → `ConfigError`). Extend `tests/test_catalog.py` (warehouses have the coordinates above). Extend `tests/test_domains.py` (`https://acme.business.site/x` → `acme.business.site`; `https://beta.business.site` ≠ `acme.business.site`).

- [ ] **Step 2: Run to verify they fail** — `uv run pytest tests/test_signal_settings.py tests/test_migrations.py tests/test_config.py tests/test_catalog.py tests/test_domains.py -q` → FAIL.
- [ ] **Step 3: Implement** catalog, settings service, models, config, facts coordinates, suffixes; autogenerate `0004` against the dev DB (throwaway `SALESFINDER_SECRET_KEY` in the command env, no `.env`), hand-review, leave dev DB at head.
- [ ] **Step 4: Run to verify they pass** — same command → PASS; full suite green.

---

### Task 2: Safe fetcher and page picking

**Files:**
- Create: `src/salesfinder/profiling/__init__.py`, `profiling/fetch.py`, `profiling/pages.py`, `tests/fakes/web.py`
- Modify: `pyproject.toml` (+ `selectolax>=0.3.21`), `uv.lock`
- Test: `tests/test_fetch.py`, `tests/test_pages.py`

**Interfaces:**
- Consumes: `normalize_domain`, `ProfilingConfig`.
- Produces:
  - `fetch.FetchBlocked(Exception)` with `.reason: Literal["private_address","robots","scheme","off_site_redirect","too_many_redirects"]` and `.message: str`.
  - `fetch.FetchResult` (frozen): `url: str` (final URL), `status: int`, `content_type: str`, `html: str | None` (decoded text when HTML, else None), `truncated: bool`, `error: str | None`.
  - `fetch.Resolver = Callable[[str], Awaitable[list[str]]]` (host → IP strings); default uses `asyncio.get_running_loop().getaddrinfo`.
  - `fetch.SafeFetcher(config: ProfilingConfig, *, transport: httpx.AsyncBaseTransport | None = None, resolver: Resolver | None = None)`; async context manager; `async def get(url: str, *, site_domain: str) -> FetchResult` — validates scheme; resolves the host and raises `FetchBlocked("private_address", ...)` if ANY resolved IP is not globally routable (`ipaddress.ip_address(x).is_global` false), or if the host is an IP literal that isn't global; follows redirects manually (≤3) re-validating each hop, raising `off_site_redirect` when the target's registrable domain ≠ `site_domain`, `too_many_redirects` beyond 3; streams the body and stops at `max_page_bytes` (`truncated=True`); non-HTML content types → `html=None`; timeouts / connection errors / 4xx / 5xx return a `FetchResult` with `error` set (`"Timed out after 10s"`, `"Could not connect"`, `"HTTP 403"`) rather than raising; sends `User-Agent: <config.user_agent>`; decodes with the declared charset else UTF-8 with replacement.
  - `async def allowed_by_robots(url: str) -> bool` on `SafeFetcher` — fetches `/robots.txt` once per site (cached on the instance), parses with `urllib.robotparser`; missing/4xx robots → allowed; 5xx/timeout robots → allowed (log it); `get()` raises `FetchBlocked("robots", ...)` when disallowed.
  - `pages.pick_pages(home_html: str, base_url: str, *, limit: int) -> list[str]` — absolute same-registrable-domain http(s) links from the homepage whose path or anchor text matches, in this priority order, at most one URL per group: about (`about`, `company`, `who-we-are`, `profile`), products (`product`, `catalog`, `catalogue`, `portfolio`, `chemicals`, `range`), contact (`contact`, `reach-us`, `locations`), careers (`career`, `jobs`, `join-us`, `vacanc`, `opportunit`), quality (`quality`, `certif`, `compliance`, `sustainab`), export (`export`, `global`, `international`, `markets`). Strips fragments, drops query strings, dedupes, excludes the homepage itself and files (`.pdf`, `.jpg`, `.png`, `.zip`, `.doc*`, `.xls*`). Returns `limit - 1` URLs max (homepage counts toward the per-company limit).
  - `tests/fakes/web.py`: `FakeWeb(sites: dict[str, str | tuple[int, dict[str,str], bytes]], dns: dict[str, list[str]])` with `.transport`, `.resolver`, `.calls`; unknown host → connection error; supports `robots.txt`, redirects (3xx with `location`), slow responses (an `asyncio.sleep` marker), and big bodies.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_fetch.py  (FakeWeb; no network)
async def test_fetches_html_with_user_agent(): ...        # 200 text/html -> html set, UA header == config.user_agent
async def test_blocks_private_ip_literal(): ...           # http://127.0.0.1/ -> FetchBlocked reason "private_address"
async def test_blocks_hostname_resolving_private(): ...   # acme.com -> ["192.168.1.5"] -> private_address
async def test_blocks_mixed_resolution(): ...             # acme.com -> ["93.184.216.34","10.0.0.1"] -> private_address
async def test_redirect_offsite_and_private(): ...        # acme.com 301 -> evil.com -> off_site_redirect; 301 -> http://10.0.0.1 -> private_address
async def test_redirect_limit(): ...                      # 4 hops -> too_many_redirects; 3 hops -> ok, url == final
async def test_scheme(): ...                              # ftp://acme.com -> reason "scheme"
async def test_robots_disallow_and_missing(): ...         # robots "Disallow: /" -> robots; 404 robots -> allowed
async def test_truncates_large_body(): ...                # 3 MB body with max 2 MB -> truncated True, len(html) <= 2_000_000
async def test_non_html_not_parsed(): ...                 # application/pdf -> html None, error None
async def test_errors_are_results(): ...                  # 500 -> error "HTTP 500"; timeout -> error startswith "Timed out"; unknown host -> "Could not connect"
```

```python
# tests/test_pages.py
def test_picks_one_per_group_in_priority_order(): ...
    # home with links About Us, /products, /contact-us, /careers, /quality-policy, /export, /blog, https://other.com/about
    # limit 6 -> [about, products, contact, careers, quality] (5 = limit-1); other.com and /blog excluded
def test_skips_files_fragments_queries_and_home(): ...
def test_relative_and_subdomain_links(): ...   # "www.acme.com/about" from acme.com counts as same site; "shop.other.com" not
```

- [ ] **Step 2: Run to verify they fail** — `uv run pytest tests/test_fetch.py tests/test_pages.py -q` → FAIL.
- [ ] **Step 3: Implement** fetcher, page picker, fake (selectolax for link extraction).
- [ ] **Step 4: Run to verify they pass** — same → PASS.

---

### Task 3: Text extraction and common detectors

**Files:**
- Create: `src/salesfinder/signals/text.py`, `signals/geo.py`, `signals/detect_common.py`, `tests/fixtures/sites/` (small HTML files as needed)
- Test: `tests/test_text.py`, `tests/test_geo.py`, `tests/test_detect_common.py`

**Interfaces:**
- Consumes: `Catalog`, `Facts`, `INDUSTRIES`, `DiscoveryConfig.industry_keywords`, `DiscoveryConfig.supplier_regions`.
- Produces:
  - `text.PageText` (frozen): `url: str`, `title: str`, `text: str` (visible text: scripts/styles/noscript/svg/template removed, whitespace collapsed), `links: tuple[tuple[str, str], ...]` (href, anchor text). `extract(html: str, url: str) -> PageText`.
  - `text.snippet(text: str, start: int, end: int, *, width: int = 300) -> str` — window around the match, word-trimmed, ≤ `width` chars, with `…` on cut sides.
  - `text.Evidence` (frozen): `url: str`, `snippet: str`.
  - `detect_common.Finding` (frozen dataclass): `code: str`, `value: Any` (JSON-serializable), `evidence: Evidence | None`. (All detectors return `Finding`; a "not found" finding has evidence `None`.)
  - `detect_chemicals(pages, catalog, *, code: str) -> Finding` — matches each product's `name`, `search_name`, `synonyms`, `inci` (case-insensitive, word-boundary, hyphen/space-insensitive: `SLES-70` matches `SLES`, `Sodium Laureth Sulphate` matches `sulfate`/`sulphate` interchangeably) and each `cas` (exact, allowing `CAS No.`/`CAS#` prefixes). Value: sorted list of product ids (`[]` when none). Evidence: first match. Short synonyms (≤4 chars, e.g. `SLES`, `CAPB`, `KOH`, `HCl`) must match as whole tokens in uppercase-insensitive form but NOT inside longer words (`SLESSOR`).
  - `detect_industries(pages, keywords: dict[str, list[str]]) -> Finding` (code `INDUSTRY_MATCH`) — industry keys whose label words or any keyword appear (whole-phrase, case-insensitive); value sorted list.
  - `detect_certs(pages, *, code: str) -> Finding` — value sorted list from this fixed vocabulary (canonical name ← patterns): `ISO 9001` (`ISO\s?9001(:\d{4})?`), `ISO 14001`, `ISO 45001`, `ISO 22000`, `FSSC 22000`, `GMP` (`\b(c?GMP)\b`, `good manufacturing practice`), `NSF`, `REACH` (`\bREACH\b` near `regist` or `compliant`/`certified`, or `REACH registered`), `Kosher`, `Halal`, `RSPO`, `ECOCERT`, `COSMOS`, `USDA Organic`, `FDA registered` (`FDA[- ]registered`), `BRC`. A pattern inside a longer digit run (e.g. a phone number containing `9001`) must not match ISO.
  - `detect_us_location(pages) -> Finding` (code `US_LOCATION`) — finds US addresses `City, ST 12345` or `City, <State name> 12345`; value `{"state": "TX", "city": "Houston"}` or `{"found": false}`; prefers contact/about pages over others.
  - `geo.STATE_CENTROIDS: dict[str, tuple[float, float]]` — approximate geographic centroids (2 decimals) for the 50 states + DC; `geo.haversine_miles(a, b) -> float`; `geo.nearest_warehouse(state: str, warehouses: Sequence[Warehouse]) -> tuple[Warehouse, int]` (rounded miles).
  - `detect_near_warehouse(location: Finding, facts: Facts) -> Finding` (code `NEAR_WAREHOUSE`) — from `US_LOCATION`'s state: `{"warehouse": label, "miles": n}`; `{"found": false}` when no state.
  - `detect_region(pages, regions: Sequence[Region], fallback: str | None) -> Finding` (code `REGION`) — first supplier region name found in an address-like context on contact/about pages, else `fallback` (the company's discovery region) with evidence `None`; value string or `None`.

- [ ] **Step 1: Write the failing tests** (catalog/facts/config from the shipped files via the hermetic copy)

```python
# tests/test_text.py
def test_extract_strips_scripts_and_collapses(): ...  # "<script>x</script><p>Hello   <b>world</b></p>" -> text "Hello world"
def test_snippet_window_and_ellipsis(): ...           # long text -> len <= 300, starts/ends with "…" when cut

# tests/test_detect_common.py
@pytest.mark.parametrize("phrase,expected", [
    ("We buy Sodium Laureth Sulphate in bulk", ["sles"]),
    ("SLES-70 and CAPB for shampoos", ["capb", "sles"]),
    ("CAS No. 68585-34-2", ["sles"]),
    ("caustic soda flakes and soda ash", ["caustic_soda", "soda_ash"]),
    ("SLESSOR Industries", []),
    ("glycerol USP", ["glycerine"]),
])
def test_chemicals(phrase, expected): ...
@pytest.mark.parametrize("phrase,expected", [
    ("ISO 9001:2015 certified", ["ISO 9001"]),
    ("cGMP facility, Halal-certified", ["GMP", "Halal"]),
    ("Call +1 800 900 1234", []),
    ("REACH registered substances", ["REACH"]),
    ("We reach customers worldwide", []),
])
def test_certs(phrase, expected): ...
def test_industries(): ...      # "shampoo and skin care manufacturer" -> ["personal_care"]
def test_us_location(): ...     # "1200 Main St, Houston, TX 77002" -> {"state":"TX","city":"Houston"}; "Houston, Texas 77002" -> TX
def test_near_warehouse(): ...  # NJ -> Bridgewater, miles < 60; FL -> Houston, TX; CA -> Santa Fe Springs, CA; no state -> {"found": False}
def test_region(): ...          # "Plot 12, GIDC Vapi, Gujarat 396195" -> "Gujarat"; none -> fallback with evidence None
def test_not_found_has_no_evidence(): ...
```

- [ ] **Step 2: Run to verify they fail** — `uv run pytest tests/test_text.py tests/test_geo.py tests/test_detect_common.py -q` → FAIL.
- [ ] **Step 3: Implement** per the Interfaces block (compile patterns once per catalog; pure functions).
- [ ] **Step 4: Run to verify they pass** — same → PASS.

---

### Task 4: Buyer- and supplier-specific detectors

**Files:**
- Create: `src/salesfinder/signals/detect_buyer.py`, `signals/detect_supplier.py`, `signals/run.py`
- Test: `tests/test_detect_buyer.py`, `tests/test_detect_supplier.py`, `tests/test_detect_run.py`

**Interfaces:**
- Consumes: Task 3 (`PageText`, `Finding`, `Evidence`, common detectors).
- Produces:
  - `detect_company_type(pages) -> Finding` (code `COMPANY_TYPE`) — value one of `manufacturer`, `contract_manufacturer`, `private_label`, `distributor`, `retailer`, `service`, `unknown`. Phrase scores (count occurrences across pages): manufacturer (`we manufacture`, `manufacturer of`, `manufacturing facility`, `our plant`, `production facility`, `formulate and manufacture`), contract_manufacturer (`contract manufactur`, `toll manufactur`, `co-manufactur`, `CMO`), private_label (`private label`, `white label`), distributor (`distributor of`, `we distribute`, `distribution partner`, `authorized distributor`, `wholesale`), retailer (`add to cart`, `shop now`, `checkout`, `free shipping`), service (`consulting services`, `testing services`, `laboratory services`). Highest score wins if ≥ 2; ties broken in the order listed; else `unknown`. Evidence: first hit of the winner.
  - `detect_hiring(pages) -> Finding` (code `HIRING_PROCUREMENT`) — only on pages whose URL/title matches the careers group from Task 2; roles matched (case-insensitive): `purchasing`, `procurement`, `buyer`, `supply chain`, `sourcing`, `formulation chemist`, `R&D chemist`, `formulator`; value `{"hiring": bool, "roles": sorted list}`; `{"hiring": false, "roles": [], "careers_page": false}` when no careers page was fetched.
  - `detect_manufacturer_or_trader(pages) -> Finding` (code `MANUFACTURER_NOT_TRADER`) — value `manufacturer` / `trader` / `unclear`. Manufacturer phrases: `manufacturing facility`, `manufacturing plant`, `our factory`, `production capacity`, `installed capacity`, `reactor`, `we manufacture`, `manufacturer and exporter`, `manufacturer & exporter`; trader phrases: `trader`, `trading company`, `traders`, `stockist`, `dealer`, `importer and distributor`, `indenting agent`. Manufacturer if manufacturer hits ≥ 2 and > trader hits; trader if trader hits ≥ 2 and > manufacturer hits; else `unclear`.
  - `detect_exports(pages) -> tuple[Finding, Finding]` (codes `EXPORTS`, `EXPORTS_TO_US`) — exports true on `export`, `exporter`, `we export to`, `international markets`, `overseas customers`; to-US true when `USA`, `United States`, `U.S.`, `North America`, or `US market` appears within 200 characters of an export phrase. Values bool.
  - `detect_capacity(pages) -> Finding` (code `STATED_CAPACITY`) — patterns: number (with `,` thousands and decimals) followed by `MT`/`metric tons`/`tons`/`tonnes`/`TPM`/`TPA`/`MTPA`/`KL` and a period (`per month`, `/month`, `monthly`, `per annum`, `per year`, `/year`, `annually`, `TPM`=month, `TPA`/`MTPA`=year); value `{"text": matched text, "mt_per_year": int}` (month × 12; `KL` treated as tonnes; `MTPA` = million tonnes per annum × 1,000,000); largest value wins; `{"found": false}` otherwise.
  - `run.run_detectors(pages: Sequence[PageText], *, side: str, codes: frozenset[str], catalog, facts, config: AppConfig, region_fallback: str | None) -> list[Finding]` — runs only detectors for confirmed `codes` of that side (NEAR_WAREHOUSE needs US_LOCATION computed even if US_LOCATION isn't confirmed — compute internally, emit only confirmed codes). Returns findings in `SIGNALS` order.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_detect_buyer.py
def test_company_type_manufacturer(): ...   # "We manufacture shampoos at our plant in Ohio. Our manufacturing facility..." -> manufacturer
def test_company_type_retailer(): ...       # "Add to cart ... Free shipping ... Checkout" -> retailer
def test_company_type_unknown_single_hit(): ...  # one phrase only -> unknown
def test_company_type_tie_order(): ...      # 2 manufacturer + 2 distributor hits -> manufacturer
def test_hiring_roles_only_on_careers_page(): ...  # "Procurement Manager" on /careers -> roles ["procurement"]; same text on /about -> hiring False
def test_no_careers_page(): ...             # -> {"hiring": False, "roles": [], "careers_page": False}

# tests/test_detect_supplier.py
def test_manufacturer(): ...   # "manufacturer and exporter ... installed capacity ... our factory" -> manufacturer
def test_trader(): ...         # "leading traders and stockist ... trading company" -> trader
def test_unclear(): ...
def test_exports_to_us(): ...  # "We export to USA, UAE and Europe" -> (True, True); "We export to Europe" -> (True, False)
@pytest.mark.parametrize("phrase,mt", [
    ("capacity of 5,000 MT per month", 60000), ("1200 TPA", 1200), ("0.5 MTPA", 500000),
    ("installed capacity 300 tonnes/month", 3600),
])
def test_capacity(phrase, mt): ...

# tests/test_detect_run.py
def test_only_confirmed_codes_and_order(): ...  # codes {"NEAR_WAREHOUSE","QUALITY_CERTS"} buyer -> exactly those two findings, NEAR_WAREHOUSE computed from address
def test_supplier_side_ignores_buyer_codes(): ...
```

- [ ] **Step 2: Run to verify they fail** — `uv run pytest tests/test_detect_buyer.py tests/test_detect_supplier.py tests/test_detect_run.py -q` → FAIL.
- [ ] **Step 3: Implement** per the Interfaces block.
- [ ] **Step 4: Run to verify they pass** — same → PASS.

---

### Task 5: Profiling service, scoring, worker step, CLI

**Files:**
- Create: `src/salesfinder/profiling/service.py`, `profiling/scoring.py`
- Modify: `src/salesfinder/worker.py`, `src/salesfinder/cli.py`, `README.md`
- Test: `tests/test_scoring.py`, `tests/test_profiling.py`, `tests/test_worker.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: Tasks 1–4; `Company`, `Observation`; `confirmed_codes`; `SafeFetcher`; `pick_pages`; `extract`; `run_detectors`.
- Produces:
  - `scoring.ScoreResult` (frozen): `score: int` (0–100), `disqualified: bool`, `reasons: list[str]` (e.g. `"Uses catalog chemicals: sles, capb (+30)"`).
  - `scoring.score_company(latest: Mapping[str, Any], *, side: str, confirmed: frozenset[str], config: ScoringConfig) -> ScoreResult` — `latest` maps code → value (latest observation). Buyer: `USES_CATALOG_CHEMICAL` non-empty → weight; `INDUSTRY_MATCH` non-empty → weight; `COMPANY_TYPE` in producer set → `COMPANY_TYPE_PRODUCER`, `distributor` → `COMPANY_TYPE_DISTRIBUTOR`, `retailer`/`service` → **disqualified** (score 0); `US_LOCATION` found → weight; `NEAR_WAREHOUSE` miles ≤ `near_warehouse_miles` → weight; `HIRING_PROCUREMENT.hiring` → weight; `QUALITY_CERTS` non-empty → weight. Supplier: `MAKES_CATALOG_CHEMICAL` non-empty; `MANUFACTURER_NOT_TRADER == "manufacturer"` → `MANUFACTURER` (trader → 0, not disqualified); `EXPORTS`; `EXPORTS_TO_US`; `SUPPLIER_CERTS` non-empty; `STATED_CAPACITY` found. Only codes in `confirmed` count. Clamp to 0–100.
  - `service.ProfileOutcome` (frozen): `company_id`, `profile_status`, `pages_fetched`, `findings: int`, `score: int | None`, `error: str | None`.
  - `service.profile_company(db_factory, company_id: uuid.UUID, *, fetcher: SafeFetcher, catalog, facts, config: AppConfig, now: Callable[[], datetime]) -> ProfileOutcome` — skips (returns without fetching) if the company is `excluded`. Fetch homepage (`website_url`); `FetchBlocked` → `profile_status="blocked"`, `profile_error` = its message; homepage error → `failed` with the error (`no_site` when DNS fails on the homepage — "Could not connect"); otherwise fetch `pick_pages(...)` (each page error is tolerated → `partial` if any page failed, else `ok`); extract → `run_detectors` with the confirmed codes → insert one `Observation` per finding (value, evidence url/snippet) → compute `latest` (this run's findings merged over earlier latest observations for still-confirmed codes) → `score_company` → update company (`profile_status`, `profile_error`, `profiled_at=now`, `pages_fetched`, `score`, `disqualified`, `scored_at`). One transaction for observations + company update, committed at the end; fetching happens outside any transaction.
  - `service.profile_batch(db_factory, *, fetcher, catalog, facts, config, now, limit: int) -> list[ProfileOutcome]` — picks up to `limit` companies with `status <> 'excluded'` and (`profile_status = 'pending'` or `profiled_at < now - refresh_after_days`), ordered pending first then oldest `profiled_at`, claimed with `FOR UPDATE SKIP LOCKED` by setting `profiled_at=now` in a short transaction (so a crash doesn't re-pick instantly); runs them with `asyncio.Semaphore(config.profiling.concurrency)`; one company's exception becomes a `failed` outcome with `profile_error="Profiling error (<ExcType>)."` and never stops the batch. Does nothing (returns `[]`) when no signal is confirmed.
  - `service.rescore_all(db, *, config, now) -> int` — recompute `score/disqualified/scored_at` for every non-excluded company from latest observations of currently confirmed codes (used after signal state changes); flush.
  - `service.request_reprofile(db, company_id, *, actor) -> None` — sets `profile_status='pending'`; flush.
  - Worker: new step `profiling` after discovery: `profile_batch(limit=config.profiling.batch_size)`; lines like `"Profiled 7 companies (6 ok, 1 failed)."` *(follow-up)* the exclusion sweep moves out of `_reap` into its own isolated step `sweep` (so a sweep failure can't roll back reaping and vice versa). `WorkerDeps` gains `facts: Facts` and `web_transport` / `web_resolver` (test injection).
  - CLI: `salesfinder signals` (table: code, side, state, label, companies observed), `salesfinder signals --confirm free` / `--confirm CODE` / `--reject CODE` / `--skip CODE` (audit `signal.state_changed`, then `rescore_all`, commit); `salesfinder profile --limit N` (runs one `profile_batch` now and prints outcomes).
  - README: Signals section (what each signal means, how to confirm, that profiling reads companies' own websites politely and obeys robots.txt), and that the worker profiles `batch_size` companies per pass.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_scoring.py
def test_buyer_full_score(): ...  # all buyer signals positive + producer, confirmed all -> 100 with 8 reasons
def test_buyer_disqualified_retailer(): ...  # COMPANY_TYPE "retailer" -> score 0, disqualified True
def test_unconfirmed_signals_ignored(): ...  # confirmed only {"QUALITY_CERTS"} -> score 10
def test_near_warehouse_threshold(): ...     # miles 499 -> +10; 501 -> +0
def test_supplier_trader_not_disqualified(): ...  # trader -> 0 for that part, disqualified False

# tests/test_profiling.py  (FakeWeb sites + fake resolver; engine/db fixtures)
async def test_profile_buyer_site_end_to_end(engine, db): ...
    # confirm all buyer signals; company acme.com (new); FakeWeb: home links /about /products /contact /careers;
    # pages mention SLES, ISO 9001, "Houston, TX 77002", "Procurement Manager" on careers
    # -> profile_status "ok", pages_fetched 5, observations for 7 codes with evidence urls, score > 0
async def test_excluded_company_never_fetched(engine, db): ...   # FakeWeb.calls == []
async def test_private_address_blocked(engine, db): ...          # resolver -> 10.0.0.1 -> "blocked", no observations
async def test_homepage_errors(engine, db): ...                  # unknown host -> "no_site"; HTTP 500 -> "failed" with "HTTP 500"
async def test_partial_when_subpage_fails(engine, db): ...       # /about 500 -> "partial"
async def test_negative_findings_recorded(engine, db): ...       # site with nothing -> observations with {"found": False}/[] values, evidence None
async def test_no_confirmed_signals_does_nothing(engine, db): ... # profile_batch -> []
async def test_batch_isolates_failures_and_claims(engine, db): ... # 3 companies, one raises inside detectors (monkeypatch) -> 2 ok + 1 failed "Profiling error (RuntimeError)."
async def test_rescore_after_reject(engine, db, admin): ...
    # profiled with QUALITY_CERTS confirmed (score 10); reject it + rescore_all -> score 0
async def test_refresh_after_days(engine, db): ...   # profiled 31 days ago -> picked again; 5 days ago -> not
```

Extend `tests/test_worker.py` (run_once with a confirmed signal and a pending company → line "Profiled 1 company (1 ok, 0 failed)."; sweep step failing doesn't prevent reaping). Extend `tests/test_cli.py` (`signals --confirm free` → 14 confirmed + audit rows; `signals --reject NOPE` → exit 1 with "Unknown signal NOPE").

- [ ] **Step 2: Run to verify they fail** — `uv run pytest tests/test_scoring.py tests/test_profiling.py tests/test_worker.py tests/test_cli.py -q` → FAIL.
- [ ] **Step 3: Implement** scoring, service, worker step + sweep split, CLI, README.
- [ ] **Step 4: Run to verify they pass** — same → PASS.

---

### Task 6: Cohorts — condition language and evaluation

**Files:**
- Create: `src/salesfinder/cohorts/__init__.py`, `cohorts/model.py`, `cohorts/evaluate.py`
- Test: `tests/test_cohorts.py`

**Interfaces:**
- Consumes: `SIGNALS`, `Observation`, `Company`, `confirmed_codes`.
- Produces:
  - `model.Condition` (pydantic, `extra="forbid"`): `code: str` (a signal code), `op: Literal["present","absent","contains_any","contains_all","in","is_true","is_false","lte","gte"]`, `values: list[str] = []`, `field: str | None = None`, `number: float | None = None`. Validation: op must suit the signal's `value_kind` (list → contains_any/contains_all/present/absent; enum/text → in/present/absent; bool → is_true/is_false/present/absent; object → present/absent/lte/gte with `field` ∈ {`miles` for NEAR_WAREHOUSE, `mt_per_year` for STATED_CAPACITY, `hiring` via is_true/is_false for HIRING_PROCUREMENT}); `values` required and non-empty for contains_*/in; `number` required for lte/gte.
  - `model.CohortDefinition`: `side: Literal["buyer","supplier"]`, `must: list[Condition] = []`, `must_not: list[Condition] = []`, `min_score: int | None = None` (0–100), `include_disqualified: bool = False`. Every condition's code must belong to `side`.
  - `evaluate.condition_holds(cond: Condition, value: Any | None) -> bool` — `value is None` (never observed, or the signal isn't confirmed) means "absent": `absent` → True, everything else → False. "Present" means observed with a truthy finding (non-empty list, non-`unknown`/`unclear` enum, `True`, object without `"found": False`).
  - `evaluate.matches(defn, *, latest: Mapping[str, Any], score: int | None, disqualified: bool) -> bool` — all `must` hold, no `must_not` holds, score ≥ `min_score` when set (unscored → False), and not disqualified unless `include_disqualified`.
  - `async def evaluate.load_latest(db, *, side: str, confirmed: frozenset[str]) -> dict[uuid.UUID, dict[str, Any]]` — one query: latest observation per (company, code) for non-excluded companies of that side, only confirmed codes (`DISTINCT ON (company_id, code) ... ORDER BY company_id, code, observed_at DESC`).
  - `async def evaluate.count_matches(db, defn) -> int` and `async def evaluate.list_matches(db, defn, *, limit: int = 50, offset: int = 0) -> tuple[list[Company], int]` (ordered by score desc nulls last, then name). Excluded companies never match.
  - `async def create_cohort(db, *, name: str, definition: CohortDefinition, actor: User) -> Cohort`, `update_cohort(db, cohort_id, *, name, definition, actor)`, `delete_cohort(db, cohort_id)` — name stripped, 1–120 chars, unique (`ValueError("A cohort with that name already exists.")`); flush.

- [ ] **Step 1: Write the failing tests** — `tests/test_cohorts.py`:

```python
def test_condition_validation(): ...
    # Condition(code="USES_CATALOG_CHEMICAL", op="lte", number=1) -> ValidationError
    # Condition(code="NEAR_WAREHOUSE", op="lte", field="miles", number=500) ok; field "km" -> ValidationError
    # CohortDefinition(side="buyer", must=[Condition(code="EXPORTS", op="is_true")]) -> ValidationError (supplier code)
def test_holds_semantics(): ...
    # contains_any(["sles","capb"]) vs ["capb"] -> True; vs [] -> False; vs None -> False; absent vs None -> True
    # NEAR_WAREHOUSE lte 500 vs {"warehouse":"Houston, TX","miles":320} -> True; vs {"found": False} -> False
    # COMPANY_TYPE present vs "unknown" -> False
def test_matches_score_and_disqualified(): ...
async def test_count_and_list_live(db): ...
    # 4 buyer companies with observations + 1 excluded matching one + 1 supplier
    # defn must USES_CATALOG_CHEMICAL contains_any [sles] AND must_not COMPANY_TYPE in [distributor] -> count 2;
    # list ordered by score desc; excluded never included
async def test_unconfirmed_code_counts_as_absent(db): ...
async def test_latest_observation_wins(db): ...  # older obs ["sles"], newer [] -> no longer matches
async def test_cohort_crud_unique_name(db, admin): ...
```

- [ ] **Step 2: Run to verify they fail** — `uv run pytest tests/test_cohorts.py -q` → FAIL.
- [ ] **Step 3: Implement** per the Interfaces block.
- [ ] **Step 4: Run to verify they pass** — same → PASS.

---

### Task 7: Dashboard — Signals page, company signals and score

**Files:**
- Create: `src/salesfinder/web/routes/signals.py`, templates `signals.html`
- Modify: `web/routes/companies.py`, `web/app.py`, templates `companies.html`, `company.html`, `base.html`, `static/app.css`
- Test: `tests/test_web_signals.py`, `tests/test_web_companies.py`

**Interfaces:**
- Consumes: `signals.settings`, `SIGNALS`, `rescore_all`, `request_reprofile`, `Observation`.
- Produces:
  - Nav: Today, Discover, Companies, Signals, Cohorts, Catalog (+ admin links).
  - `GET /signals` — two tables (Buyer signals, Supplier signals): label, description, cost ("Free"), state, companies observed (count of distinct companies with a latest observation that is "present"), and per-row state buttons (`POST /signals/{code}/state` with `state` ∈ confirmed/skipped/rejected/proposed; CSRF; audit `signal.state_changed` with old/new; then `rescore_all`; commit; PRG with fixed flash "Saved. Scores updated."). A "Confirm all free signals" button (`POST /signals/confirm-free`). Members and admins may change states (spec §6.7). Page note: "Profiling only reads a company's website for confirmed signals."
  - Companies list: adds a Score column (blank when unscored; "✗" badge when disqualified) and a Profile column (status); sort param `sort=score|newest` (default newest; invalid → 400).
  - Company detail: profile status/error/time and pages fetched; score with reasons (recomputed via `score_company` from latest observations for display); a Signals table of latest observations for confirmed codes (label, value rendered readably — lists comma-joined with product names from the catalog, objects as "Houston, TX", "320 miles from Houston, TX", booleans Yes/No — evidence snippet and evidence link with `rel="noopener noreferrer"`); "Profile again" button (`POST /companies/{id}/reprofile`, CSRF, audit `company.reprofile_requested`, flash "Queued — the worker will profile it shortly."), hidden for excluded companies.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_web_signals.py
async def test_signals_page_lists_both_sides(client, member): ...       # contains "Uses catalog chemicals" label and "Manufacturer or trader" style labels
async def test_change_state_audits_and_rescores(client, member, db): ...
    # company with QUALITY_CERTS obs ["ISO 9001"], score None -> confirm QUALITY_CERTS -> company.score == 10; audit old/new
async def test_confirm_free(client, member, db): ...                     # all 14 confirmed; flash
async def test_bad_state_400_and_csrf_403(client, member): ...

# tests/test_web_companies.py (extend)
async def test_company_detail_shows_signals_evidence_escaped(client, member, db): ...
    # observation snippet "<img src=x onerror=alert(1)>" rendered escaped; product ids shown as names
async def test_reprofile_button(client, member, db): ...  # POST -> profile_status "pending", audit; excluded company -> 400
async def test_sort_by_score(client, member, db): ...     # sort=score orders desc, unscored last; sort=bogus -> 400
```

- [ ] **Step 2: Run to verify they fail** — `uv run pytest tests/test_web_signals.py tests/test_web_companies.py -q` → FAIL.
- [ ] **Step 3: Implement** routes, templates, nav, CSS.
- [ ] **Step 4: Run to verify they pass** — same → PASS.

---

### Task 8: Dashboard — Cohorts with live counts; full verification

**Files:**
- Create: `src/salesfinder/web/routes/cohorts.py`, templates `cohorts.html`, `cohort_form.html`, `_cohort_count.html`, `cohort.html`
- Modify: `web/app.py`, `README.md`
- Test: `tests/test_web_cohorts.py`

**Interfaces:**
- Consumes: Task 6 services, `SIGNALS`, `confirmed_codes`, `Catalog` (product choices), `INDUSTRIES`.
- Produces:
  - `GET /cohorts` — list: name, side, live count (`count_matches`), created by, updated.
  - `GET /cohorts/new?side=buyer|supplier` and `GET /cohorts/{id}/edit` — form built from the side's **confirmed** signals: for each signal a row with a mode select (`ignore` / `must` / `must_not`) and the inputs its kind needs (product checkboxes for chemical lists; industry checkboxes for INDUSTRY_MATCH; cert checkboxes from the Task 3 vocabulary; enum checkboxes for COMPANY_TYPE / MANUFACTURER_NOT_TRADER; a number for NEAR_WAREHOUSE miles / STATED_CAPACITY MT per year; "present" for objects and text), plus min score and "include disqualified". Unconfirmed signals are listed greyed out with a link to /signals.
  - `POST /cohorts/count` — HTMX endpoint taking the same form fields, returning `_cohort_count.html` ("23 companies match") with `hx-post`; **(ruling)** this is the first `hx-post`: send the CSRF token via `hx-headers` from a `<meta name="csrf-token">` in `base.html`, and *(follow-up)* `check_csrf` accepts the `X-CSRF-Token` header as an alternative to the form field. The form uses `hx-trigger="change delay:300ms"` on the form element.
  - `POST /cohorts` (create) / `POST /cohorts/{id}` (update) / `POST /cohorts/{id}/delete` — CSRF; audit `cohort.created/updated/deleted` (details: name, side, definition); validation errors re-render the form 400 with messages and selections kept; PRG on success.
  - `GET /cohorts/{id}?page=N` — the cohort's definition in plain English (e.g. "Uses SLES or CAPB · not a distributor · score ≥ 40"), live count, matching companies (50/page, score desc) linking to company pages.
  - Form-to-definition parsing lives in one function `cohort_from_form(form: FormData, side: str) -> CohortDefinition` (unit-tested), used by count/create/update.
  - README: Cohorts section.

- [ ] **Step 1: Write the failing tests** — `tests/test_web_cohorts.py`:

```python
def test_cohort_from_form(): ...   # must USES_CATALOG_CHEMICAL [sles,capb], must_not COMPANY_TYPE [distributor], min_score 40 -> expected definition
async def test_new_form_shows_only_confirmed(client, member, db): ...
async def test_live_count_with_header_csrf(client, member, db): ...
    # POST /cohorts/count with X-CSRF-Token header and HX-Request -> fragment "2 companies match"; without token -> 403
async def test_create_view_edit_delete(client, member, db): ...   # audits all three; plain-English summary shown; list count correct
async def test_duplicate_name_and_invalid(client, member, db): ...  # 400 with "A cohort with that name already exists."; kept selections
async def test_cohort_page_paginates_and_excludes_excluded(client, member, db): ...
```

- [ ] **Step 2: Run to verify they fail** — `uv run pytest tests/test_web_cohorts.py -q` → FAIL.
- [ ] **Step 3: Implement** routes, templates, header CSRF, README.
- [ ] **Step 4: Run to verify they pass** — same → PASS.
- [ ] **Step 5: Full verification** — `make check` (ruff, mypy, all tests, `-W error` clean) and the hermetic polluted-env run. Manual smoke (only if no `.env` exists: temporary `.env`, cleaned up after, no real API keys): `make migrate`; throwaway admin; `salesfinder signals --confirm free`; insert one company row for a local test page? — NO: do not fetch real sites; instead verify `/signals`, `/cohorts/new?side=buyer` render and `POST /cohorts/count` returns a count with the header token; remove the throwaway admin and its rows.
- [ ] **Step 6: Hand back** — report `git status --short`. Do not commit.
