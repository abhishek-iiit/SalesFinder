# SalesFinder Phase 4: People and Email Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** For every profiled company, find up to two decision makers named on the company's own pages, collect the email addresses it publishes, learn its email pattern, check the domain can receive mail (MX), produce candidate addresses with an honest status, optionally confirm them with a paid verifier, and show teammates who to write to and whether each address is sendable.

**Architecture:** People/email extraction is pure code run on the pages the profiler already fetched (no second crawl): `profile_company` gains extra page groups (team/leadership) and stores people, published addresses and the contact-form URL in the same write. A separate worker step ("contacts") does the network part per company — bounded async MX lookups (dnspython) with a cache table, pattern learning, candidate generation, optional verifier calls under a daily cap — and computes sendability. Status rules follow spec §6.2.1 exactly.

**Tech Stack:** Phase 1–3 stack, plus `dnspython` (async resolver).

**Spec:** `docs/superpowers/specs/2026-11-07-elchemy-salesfinder-design.md` — §6.2 People + Verify email rows, §6.2.1 statuses and sendability, §4 anti-goals, §6.7 company page. Follow-ups folded in are marked *(follow-up)*: `docs/superpowers/plans/2026-11-07-phase3-followups.md`.

## Global Constraints

- **Never run `git commit`, `git push`, `git add`, or create branches.** The user commits.
- All Phase 1–3 constraints hold (uv/Python 3.13, UUID PKs, UTC timestamptz, ruff 100, mypy strict, services flush / callers commit, worker-job functions own their transactions, no transaction during network I/O, no network in tests, tests never touch the dev DB, hermetic config/env, untrusted page text never in logs/errors, autoescape everywhere).
- **No SMTP probing, ever** (spec §6.2.1). No connection to port 25 or any mail server. DNS MX lookups only.
- Email statuses are exactly: `published`, `pattern_confirmed`, `pattern_guess`, `verified`, `risky`, `invalid`. Sendable by default: `published`, `pattern_confirmed`, `verified` — and only when the domain has MX and `exclusion_for(email=…)` is `None`. `pattern_guess` and `risky` are held unless a teammate overrides that address (audited). `invalid` is never sendable, override or not.
- At most `contacts.max_targets_per_company` (default **2**) people per company are marked as targets.
- Names, titles, and addresses come only from the company's own pages (no LinkedIn, no third-party directories).
- The paid verifier is optional (`SALESFINDER_EMAIL_VERIFIER_KEY`), never called for `published` addresses, capped per UTC day (`contacts.verifier_max_per_day`, default 100), and its key never appears in logs/errors/DB.
- DNS lookups: async, per-lookup timeout `contacts.dns_timeout_seconds` (default 5), at most `contacts.dns_concurrency` (default 8) at once, results cached per domain for `contacts.mx_cache_days` (default 7).

## Review Focus

1. **Name and title layouts seen on real Indian and US SME sites** — "Mr. Rajesh K. Sharma – Managing Director", "Priya Nair | Head of Procurement", a card with name and title on separate lines, "Contact: Purchase Dept." (no person) — produce the right people (or none), never navigation words as names → tests in Task 3.
2. **Obfuscated and odd addresses** — `sales [at] acme [dot] com`, `mailto:` with `?subject=`, uppercase, trailing punctuation, image filenames like `logo@2x.png`, addresses on other domains — are normalized or rejected correctly → tests in Task 3.
3. **Pattern learning with conflicting or thin evidence** — two published personal addresses with different patterns, a published address that doesn't match any team member, initials-only names, Indian names with a single given name — chooses deterministically or declines to guess (never invents a confirmed address from no evidence) → tests in Task 4.
4. **DNS trouble** — NXDOMAIN, no MX but an A record, timeouts, SERVFAIL — maps to `invalid` only when the domain definitely can't receive mail; timeouts are retried later and never mark addresses invalid → tests in Task 5.
5. **Dead sites in the network-outage breaker** *(follow-up)* — three or more genuinely dead domains must eventually be recorded `no_site` and back off, without a permanent "Network looks down" line → tests in Task 1.

---

## File Structure

```
config/salesfinder.yaml            # + contacts section; profiling.max_pages_per_company 6 → 8
src/salesfinder/
  config.py                        # + ContactsConfig
  settings.py                      # + email_verifier_key
  profiling/pages.py               # + team, leadership groups; tighter keyword matching (follow-up)
  profiling/service.py             # breaker hold cap (follow-up); store extraction results
  contacts/
    __init__.py
    names.py                       # normalize_name, split_name, looks_like_person_name
    extract.py                     # extract_emails, extract_people, contact_form_url
    titles.py                      # categorize_title, rank_people
    patterns.py                    # PATTERNS, learn_pattern, render_address
    dns.py                         # MxChecker (dnspython async, bounded, cached)
    verifier.py                    # Verifier protocol, MillionVerifier client, NoVerifier
    service.py                     # store_extraction, verify_company, verify_batch, set_override, sendable()
  db/models/contacts.py            # Person, EmailAddress, EmailPattern, DomainMx
  db/migrations/versions/0006_people_email.py
  worker.py                        # + contacts step
  cli.py                           # + contacts command
  cohorts/model.py, evaluate.py    # + require_sendable_contact
  web/routes/companies.py          # contacts section, override route; list column
  web/templates/company.html, companies.html, cohort_form.html, cohort.html
tests/
  fakes/dns.py                     # FakeResolver (programmable MX answers / errors / delays)
  fakes/verifier.py                # MockTransport for MillionVerifier
  test_breaker.py, test_names.py, test_extract_contacts.py, test_titles.py, test_patterns.py,
  test_dns.py, test_verifier.py, test_contacts_service.py, test_web_contacts.py, test_migrations.py
```

---

### Task 1: Breaker hold cap for dead sites *(follow-up)*

**Files:**
- Modify: `src/salesfinder/profiling/service.py` (breaker ~586-611), `src/salesfinder/db/models/companies.py`, `src/salesfinder/worker.py` (line wording if needed)
- Create: migration `0006_breaker_holds.py` *(this task's migration; Task 2 adds 0007)*
- Test: `tests/test_breaker.py` (new), `tests/test_profiling.py`, `tests/test_migrations.py`

**Interfaces:**
- Produces:
  - `Company.profile_holds: int` (default/server default 0) — consecutive breaker holds.
  - Breaker rule: when the breaker fires, each held company's `profile_holds` += 1; a company whose `profile_holds` would reach **3** is NOT held — its unreachable outcome is recorded normally (`no_site`/`failed`, `profile_failures` += 1, backoff starts) and `profile_holds` resets to 0. Any successful read (`ok`/`partial`) or recorded failure resets `profile_holds` to 0.
  - The breaker only fires when, after removing companies at their hold cap, the remaining unreachable set still has ≥ 3 companies and no site was read; otherwise all outcomes are recorded.
  - Worker line when holding: unchanged ("Network looks down — will retry."); when some were recorded because of the cap, append " N recorded as unreachable after repeated checks."
- Consumes: existing `_release`, `_write_failure`, `profile_retry_at`, `profile_failures`.

- [ ] **Step 1: Write the failing tests** — `tests/test_breaker.py`:

```python
async def test_dead_sites_recorded_after_three_holds(engine, db): ...
    # 3 pending companies whose domains don't resolve; run profile_batch at t, t+16m, t+32m
    # passes 1 and 2: held (status pending, profile_holds 1 then 2, line "Network looks down — will retry.")
    # pass 3: recorded -> profile_status "no_site", profile_failures 1, profile_holds 0
async def test_live_read_resets_holds(engine, db): ...
    # company held once, then its site comes up -> ok, profile_holds 0
async def test_cap_shrinks_breaker_below_three(engine, db): ...
    # 2 companies at holds 2 + 2 fresh unreachable -> capped two recorded; remaining 2 < 3 -> also recorded (no hold)
async def test_real_outage_still_held(engine, db): ...  # 5 fresh unreachable -> all held, nothing recorded
```

Extend `tests/test_migrations.py` round trip (head `"0006"`).

- [ ] **Step 2: Run to verify they fail** — `uv run pytest tests/test_breaker.py tests/test_migrations.py -q` → FAIL.
- [ ] **Step 3: Implement** the column, migration (autogenerate against dev DB with throwaway `SALESFINDER_SECRET_KEY` in the command env, no `.env`; leave dev DB at head), and breaker rule.
- [ ] **Step 4: Run to verify they pass** — same + `tests/test_profiling.py` → PASS.

---

### Task 2: Data model and config for people and email

**Files:**
- Create: `src/salesfinder/db/models/contacts.py`, migration `0007_people_email.py`
- Modify: `db/models/companies.py`, `db/models/__init__.py`, `config.py`, `config/salesfinder.yaml`, `settings.py`, `.env.example`, `audit.py`, `pyproject.toml` (+ `dnspython>=2.6`), `uv.lock`
- Test: `tests/test_migrations.py`, `tests/test_config.py`, `tests/test_settings.py`

**Interfaces:**
- Produces (tables exact):
  - `Person` → `people` (UUIDPk + Timestamps): `company_id` FK companies `ON DELETE CASCADE` (indexed), `full_name: str(200)`, `first_name: str(100) | None`, `last_name: str(100) | None`, `title: str(200) | None`, `title_category: str(24)` (check `IN ('procurement','supply_chain','rnd','owner_exec','export','business_dev','marketing','other')`), `is_target: bool` default false, `rank: int | None`, `source_url: str(2048)`, `source_snippet: str(300) | None`. Unique index `uq_people_company_id_name_key` on `(company_id, name_key)` where `name_key: str(200)` = normalized lowercase ASCII name (stored column).
  - `EmailAddress` → `email_addresses` (UUIDPk + Timestamps): `company_id` FK cascade (indexed), `person_id` FK people `ON DELETE SET NULL` nullable, `address: str(320)` (normalized lowercase), `kind: str(8)` (check `IN ('personal','role')`), `status: str(20)` (check `IN ('published','pattern_confirmed','pattern_guess','verified','risky','invalid')`), `source: str(16)` (check `IN ('page','pattern','guess')`), `pattern: str(32) | None`, `evidence_url: str(2048) | None`, `evidence_snippet: str(300) | None`, `verifier_result: str(32) | None`, `verified_at: timestamptz | None`, `blocked_reason: str(300) | None` (exclusion detail), `override_sendable: bool | None`, `override_by_id` FK users nullable, `override_at: timestamptz | None`. Unique `(company_id, address)`.
  - `EmailPattern` → `email_patterns`: `domain: str(255)` PK, `pattern: str(32)`, `support: int`, `examples: JSONB` (list of addresses), `learned_at: timestamptz`.
  - `DomainMx` → `domain_mx`: `domain: str(255)` PK, `state: str(12)` (check `IN ('has_mx','no_mail','unknown')`), `mx_hosts: JSONB` default `[]`, `checked_at: timestamptz`, `error: str(200) | None`.
  - `Company` gains: `contact_form_url: str(2048) | None`, `contacts_checked_at: timestamptz | None`, `contacts_found: int` default 0, `sendable_contacts: int` default 0.
  - `config.ContactsConfig`: `max_targets_per_company: int = 2` (1–5), `dns_timeout_seconds: float = 5` (>0), `dns_concurrency: int = 8` (1–32), `mx_cache_days: int = 7` (≥1), `batch_size: int = 10` (≥1), `recheck_after_days: int = 30` (≥1), `verifier: Literal["none","millionverifier"] = "none"`, `verifier_max_per_day: int = 100` (≥0). `AppConfig.contacts`. `profiling.max_pages_per_company` default and shipped YAML → `8`.
  - `Settings.email_verifier_key: SecretStr | None = None` (empty → None); `.env.example` documents it.
  - Audit constants: `EMAIL_OVERRIDE_SET = "email.override_set"`, `EMAIL_OVERRIDE_CLEARED = "email.override_cleared"`.

- [ ] **Step 1: Write the failing tests** — extend `tests/test_migrations.py` (round trip head `"0007"`; people cascade with company; `email_addresses.person_id` set null on person delete; status/kind/source/title_category/state checks accept every model value and reject "bogus" — parametrize from model constants; unique `(company_id, address)`), `tests/test_config.py` (contacts defaults; `verifier: "zerobounce"` → ConfigError; `dns_concurrency: 0` → ConfigError), `tests/test_settings.py` (empty key → None; repr hides key).
- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Implement** models, migration 0007 (autogenerate + hand-review + explicit check names), config, settings, audit, dependency.
- [ ] **Step 4: Run to verify they pass** — plus full suite green.

---

### Task 3: Extraction — names, emails, people, contact form, page groups

**Files:**
- Create: `src/salesfinder/contacts/{__init__,names,extract,titles}.py`
- Modify: `src/salesfinder/profiling/pages.py`
- Test: `tests/test_names.py`, `tests/test_extract_contacts.py`, `tests/test_titles.py`, `tests/test_pages.py`

**Interfaces:**
- Consumes: `PageText` (`text`, `title`, `links`, `url`) from `signals/text.py`; `normalize_domain`, `domain_of_email`, `FREE_MAIL_DOMAINS` from `domains.py`; selectolax for structure where needed (extraction may take the raw HTML too).
- Produces:
  - `pages.PAGE_GROUPS` gains, after `contact` and before `careers`: `("team", ("team", "our-people", "people", "staff", "directors", "board"))`, `("leadership", ("leadership", "management", "founder", "promoter"))`. *(follow-up)* keyword matching uses path-segment / word boundaries (so `range` no longer matches `orange`, `people` not `peoples-choice`); query-string-only links (`/index.php?page=about`) keep the `page=` value when it's the only routing signal.
  - `names.normalize_name(raw: str) -> str` — strip honorifics (`Mr`, `Mrs`, `Ms`, `Miss`, `Dr`, `Shri`, `Smt`, `Sri`, `Prof`, `Er`, with or without `.`), collapse whitespace, title-case only when ALL CAPS. `names.name_key(name) -> str` — NFKD → ASCII, lowercase, letters/spaces only. `names.split_name(name) -> tuple[str | None, str | None]` — first and last given tokens; initials (`R.`, `K.`) are kept as initials and never used as `first` (so "R. K. Sharma" → `(None, "Sharma")`; "Rajesh K. Sharma" → `("Rajesh", "Sharma")`; single token → `(token, None)`). `names.looks_like_person_name(text: str) -> bool` — 2–4 tokens, each capitalized or an initial, total ≤ 40 chars, no digits, not containing any stop word (company/navigation/business words: `Ltd`, `Limited`, `Pvt`, `Inc`, `LLC`, `Corporation`, `Company`, `Industries`, `Chemicals`, `Group`, `Home`, `About`, `Contact`, `Products`, `Services`, `Team`, `Our`, `Us`, `Read`, `More`, `Click`, `Department`, `Dept`, `Office`, `Sales`, `Support`, `Quality`, `Purchase`, `Export`, `Manager` etc. — keep the list in one tuple).
  - `extract.EmailFound` (frozen): `address: str`, `kind: Literal["personal","role"]`, `url: str`, `snippet: str`.
  - `extract.extract_emails(pages, *, company_domain: str) -> list[EmailFound]` — from page text and `mailto:` links; deobfuscates `name [at] domain [dot] com`, `name(at)domain(dot)com`, `name AT domain DOT com` (only with brackets/parentheses or uppercase AT/DOT surrounded by spaces); strips `mailto:` query, trailing `.,;:)`; lowercases; validates with `email_validator` (no deliverability check); rejects image/asset-looking locals/domains (`@2x`, `.png`, `.jpg`, `.webp`, `.svg`, `.gif` in domain), `example.com`, `domain.com`, `sentry`/`wixpress` hosts. Keeps an address when its registrable domain == `company_domain`, OR (free-mail domain AND found on a contact/about/team/leadership page). `kind="role"` when the local part is a role word (`info`, `sales`, `contact`, `enquiry`, `enquiries`, `inquiry`, `purchase`, `purchasing`, `procurement`, `export`, `exports`, `marketing`, `admin`, `office`, `support`, `hello`, `mail`, `customercare`, `care`, `hr`, `careers`, `jobs`, `accounts`, `orders`) or the domain is free-mail and no person matches it; else `personal`. Deduped by address (first evidence wins).
  - `titles.CATEGORIES` as in Task 2's check list; `titles.categorize_title(title: str) -> str` — keyword rules (case-insensitive, whole words): procurement (`purchas*`, `procurement`, `buyer`, `sourcing`), supply_chain (`supply chain`, `logistics`, `materials manager`), rnd (`R&D`, `research`, `formulat*`, `technical director`, `chemist`), owner_exec (`owner`, `founder`, `co-founder`, `proprietor`, `partner`, `president`, `CEO`, `chief executive`, `managing director`, `MD`, `director`, `chairman`, `promoter`, `general manager`), export (`export*`, `international sales`), business_dev (`business development`, `BD`, `sales head`, `head of sales`, `VP sales`, `sales director`), marketing (`marketing`); first match in this order; else `other`.
  - `extract.PersonFound` (frozen): `full_name`, `title: str | None`, `category: str`, `url`, `snippet`.
  - `extract.extract_people(pages_html: Sequence[tuple[str, str]]) -> list[PersonFound]` — only on pages whose URL/title matches the about/contact/team/leadership groups. Layouts: (a) one line/element `Name <sep> Title` or `Title <sep> Name` with sep in ` – `, ` - `, ` | `, `, `, `: ` (b) adjacent block elements (heading/strong/b/p/span/li/div text) where one is a person name and the next sibling-ish text (within the same card container, or the next text node) is a title containing a title keyword. A candidate needs `looks_like_person_name(name)` AND a title that `categorize_title` doesn't map to `other` — or, for layout (a), any title with ≥1 known title word. Deduped by `name_key`; max 20 per company.
  - `titles.rank_people(people, *, side: str, limit: int) -> list[PersonFound]` — buyer priority: procurement, supply_chain, rnd, owner_exec, business_dev, marketing, export, other; supplier priority: export, owner_exec, business_dev, marketing, procurement, supply_chain, rnd, other; stable within a category by page order; returns the top `limit` excluding `other`.
  - `extract.contact_form_url(pages_html) -> str | None` — first same-site page containing a `<form>` with a `textarea` or an email-type input, on a contact-group page.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_names.py
@pytest.mark.parametrize("raw,first,last", [
    ("Mr. Rajesh K. Sharma", "Rajesh", "Sharma"), ("R. K. Sharma", None, "Sharma"),
    ("PRIYA NAIR", "Priya", "Nair"), ("Dr Anil Mehta", "Anil", "Mehta"), ("Madonna", "Madonna", None),
    ("José Álvarez", "José", "Álvarez"),
])
def test_split(raw, first, last): ...
def test_name_key_ascii(): ...  # name_key("José Álvarez") == "jose alvarez"
@pytest.mark.parametrize("text,ok", [("Priya Nair", True), ("Contact Us", False), ("Acme Chemicals Pvt Ltd", False),
    ("Read More", False), ("R. K. Sharma", True), ("Purchase Dept", False), ("John 3:16", False)])
def test_looks_like_person(text, ok): ...

# tests/test_extract_contacts.py
def test_emails_obfuscated_and_mailto(): ...
    # "sales [at] acme [dot] com", "mailto:Priya.Nair@ACME.com?subject=Hi", "logo@2x.png", "a@example.com", "x@other.com"
    # company_domain "acme.com" -> {"sales@acme.com": role, "priya.nair@acme.com": personal}
def test_free_mail_only_on_contact_pages(): ...  # acmechem@gmail.com on /contact kept (role); on /blog dropped
def test_people_layouts(): ...
    # "Mr. Rajesh K. Sharma – Managing Director" -> (Rajesh K. Sharma, owner_exec)
    # "Priya Nair | Head of Procurement" -> procurement
    # card: <div class="member"><h3>Anil Mehta</h3><p>R&D Manager</p></div> -> rnd
    # "Contact: Purchase Dept." -> no person; nav "Home | About Us | Products" -> no person
def test_people_only_on_people_pages(): ...  # same text on /products -> []
def test_contact_form(): ...

# tests/test_titles.py
def test_categorize(): ...  # "Head of Procurement"->procurement, "MD"->owner_exec, "Export Manager"->export, "Accountant"->other
def test_rank_buyer_vs_supplier(): ...  # same people -> buyer picks procurement first; supplier picks export first; others excluded

# tests/test_pages.py (extend)
def test_team_and_leadership_groups(): ...
def test_word_boundary_keywords(): ...  # "/orange-juice" not products; "/our-range" is products
```

- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Implement** per the Interfaces block (pure; selectolax.lexbor).
- [ ] **Step 4: Run to verify they pass** — plus existing profiling tests still green (page picking changed).

---

### Task 4: Pattern learning and candidate addresses

**Files:**
- Create: `src/salesfinder/contacts/patterns.py`
- Test: `tests/test_patterns.py`

**Interfaces:**
- Consumes: `names.split_name`, `name_key`, `EmailFound`, `PersonFound`.
- Produces:
  - `patterns.PATTERNS: tuple[str, ...] = ("{first}.{last}", "{first}{last}", "{f}{last}", "{first}", "{first}_{last}", "{f}.{last}", "{last}.{first}", "{first}{l}", "{last}", "{last}{f}", "{first}-{last}")` — `{f}`/`{l}` = initial; all parts ASCII-lowercased via `name_key`, internal spaces removed.
  - `render_address(pattern: str, first: str | None, last: str | None, domain: str) -> str | None` — `None` if the pattern needs a part that's missing.
  - `patterns.LearnedPattern` (frozen): `pattern: str`, `support: int`, `examples: tuple[str, ...]`.
  - `learn_pattern(domain: str, people: Sequence[PersonFound], emails: Sequence[EmailFound]) -> LearnedPattern | None` — for each **personal** published address at `domain`, find patterns that render exactly that address for some person found on the site; count support per pattern (an address supporting several patterns counts for each); choose the pattern with the highest support; tie → earliest in `PATTERNS`; `None` when no published personal address matches any person.
  - `patterns.Candidate` (frozen): `address: str`, `status: Literal["published","pattern_confirmed","pattern_guess"]`, `source: Literal["page","pattern","guess"]`, `pattern: str | None`.
  - `candidates_for(person: PersonFound, *, domain: str, emails: Sequence[EmailFound], learned: LearnedPattern | None) -> list[Candidate]` — (1) a published personal address that renders from this person under any pattern → `published` (source page); (2) else if `learned` → the learned pattern's address → `pattern_confirmed` (source pattern); (3) else one guess `{first}.{last}` (or `{first}` if no last name; nothing if no first name) → `pattern_guess` (source guess). Never more than one non-published candidate per person.
  - Free-mail domains never get pattern or guess candidates.

- [ ] **Step 1: Write the failing tests** — `tests/test_patterns.py`:

```python
def test_render(): ...   # "{f}{last}" + ("Priya","Nair") -> "pnair@acme.com"; "{first}.{last}" with last None -> None
def test_learn_single(): ...  # people [Priya Nair, Anil Mehta], published "priya.nair@acme.com" -> ("{first}.{last}", 1)
def test_learn_tie_and_conflict(): ...
    # "priya.nair@" and "amehta@" -> two patterns support 1 each -> "{first}.{last}" (earlier in PATTERNS)
def test_learn_none_without_matching_person(): ...  # only "info@" and "rk@" with no "R K" person -> None
def test_candidates_published_wins(): ...
def test_candidates_confirmed_vs_guess(): ...  # learned -> pattern_confirmed; no learned -> pattern_guess "{first}.{last}"
def test_initials_only_person(): ...  # "R. K. Sharma" (first None) -> guess None; learned "{last}" -> "sharma@" confirmed
def test_free_mail_domain_never_guessed(): ...
def test_accents(): ...  # "José Álvarez" + "{first}.{last}" -> "jose.alvarez@acme.com"
```

- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run to verify they pass.**

---

### Task 5: MX checker and optional verifier

**Files:**
- Create: `src/salesfinder/contacts/dns.py`, `contacts/verifier.py`, `tests/fakes/dns.py`, `tests/fakes/verifier.py`
- Test: `tests/test_dns.py`, `tests/test_verifier.py`

**Interfaces:**
- Consumes: `ContactsConfig`, `DomainMx` model.
- Produces:
  - `dns.MxResult` (frozen): `state: Literal["has_mx","no_mail","unknown"]`, `hosts: tuple[str, ...]`, `error: str | None`.
  - `dns.Resolve = Callable[[str, str], Awaitable[list[tuple[int, str]]]]` (qname, rdtype → (preference, host) list or raise); default built on `dns.asyncresolver.Resolver()` with `lifetime=dns_timeout_seconds`.
  - `dns.MxChecker(config: ContactsConfig, *, resolve: Resolve | None = None)`; `async def check(domain: str) -> MxResult` — MX query: answers → `has_mx` (hosts sorted by preference; a single MX of `.` = null MX → `no_mail`); `NXDOMAIN` → `no_mail`; `NoAnswer` → query A/AAAA: present → `has_mx` (implicit MX per RFC 5321) with hosts `(domain,)`, absent → `no_mail`; timeout / `NoNameservers` / SERVFAIL / any other error → `unknown` with a short fixed error ("DNS timed out", "DNS server failure"). Bounded by an `asyncio.Semaphore(dns_concurrency)`.
  - `async def mx_for(db_factory, checker, domain, *, now, config) -> MxResult` — reads `domain_mx`; fresh (< `mx_cache_days`) `has_mx`/`no_mail` rows are reused; `unknown` rows are retried after 1 hour; writes/updates the row (own short transaction).
  - `verifier.VerifyResult = Literal["verified","risky","invalid","unknown"]`.
  - `verifier.Verifier` protocol: `async def verify(address: str) -> VerifyResult`.
  - `verifier.NoVerifier` (always `"unknown"`, never called by the service when configured `none`).
  - `verifier.MillionVerifier(api_key: str, *, transport=None, timeout: float = 20)` — `GET https://api.millionverifier.com/api/v3/?api=<key>&email=<address>&timeout=10`; maps JSON `result`: `ok` → verified, `catch_all` → risky, `invalid`/`disposable` → invalid, anything else/`unknown`/errors/timeouts/non-200 → unknown; key never in exceptions/logs.
  - `verifier.daily_count(db, *, now) -> int` and the cap check use `email_addresses.verified_at` within the current UTC day.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_dns.py  (FakeResolver; no network)
async def test_mx_hosts_sorted(): ...          # [(20,"b.mx"),(10,"a.mx")] -> has_mx ("a.mx","b.mx")
async def test_null_mx(): ...                   # [(0,".")] -> no_mail
async def test_nxdomain(): ...                  # -> no_mail
async def test_no_mx_but_a_record(): ...        # MX NoAnswer, A present -> has_mx (domain,)
async def test_no_mx_no_a(): ...                # -> no_mail
async def test_timeout_and_servfail_unknown(): ...  # -> unknown "DNS timed out" / "DNS server failure"
async def test_concurrency_bounded(): ...       # 20 lookups with dns_concurrency 3 -> max in flight 3
async def test_cache(engine, db): ...           # fresh has_mx reused (resolver not called); unknown retried after 1h; stale after 7d re-queried

# tests/test_verifier.py
@pytest.mark.parametrize("payload,expected", [({"result":"ok"},"verified"), ({"result":"catch_all"},"risky"),
    ({"result":"invalid"},"invalid"), ({"result":"disposable"},"invalid"), ({"result":"unknown"},"unknown")])
async def test_mapping(payload, expected): ...
async def test_errors_are_unknown_and_key_hidden(): ...  # 500, timeout, bad JSON -> "unknown"; key not in any log record
```

- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Implement** (`uv add dnspython` already in Task 2).
- [ ] **Step 4: Run to verify they pass.**

---

### Task 6: Contacts service, profiling integration, worker step, CLI

**Files:**
- Create: `src/salesfinder/contacts/service.py`
- Modify: `src/salesfinder/profiling/service.py`, `src/salesfinder/worker.py`, `src/salesfinder/cli.py`, `README.md`
- Test: `tests/test_contacts_service.py`, `tests/test_profiling.py`, `tests/test_worker.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: Tasks 2–5; `exclusion_for`; Phase 3 profiling flow.
- Produces:
  - Profiling integration: `profile_company` runs `extract_emails`, `extract_people`, `contact_form_url` in the same thread as detectors (raw HTML is passed along with `PageText`) and, in the same final write transaction, calls `contacts.service.store_extraction(db, company, people, emails, form_url, *, side, config)` which upserts `people` (by `name_key`), sets `is_target`/`rank` from `rank_people`, upserts `published` addresses (link `person_id` when a person's name renders the address under any pattern), sets `contact_form_url`, and sets `contacts_checked_at = NULL` (due for verification). Existing pattern/guess rows are kept until re-verification. Only on `ok`/`partial`; a partial run never deletes people or published addresses found earlier.
  - `contacts.service.verify_company(db_factory, company_id, *, checker: MxChecker, verifier: Verifier | None, config: AppConfig, now) -> VerifyOutcome` (frozen: `company_id`, `targets: int`, `addresses: int`, `sendable: int`, `mx_state: str`, `error: str | None`) — skips excluded companies; looks up MX for the company domain (and any free-mail domains of published addresses are treated as `has_mx` without lookup); learns the pattern from stored people + published personal addresses (persist to `email_patterns`); for each target person creates/updates its candidate via `candidates_for`; applies MX: domain `no_mail` → every non-free-mail address at it becomes `invalid`; `unknown` → statuses unchanged, `contacts_checked_at` left NULL (retry next pass), outcome `error="DNS unavailable"`; if a verifier is configured and the daily cap allows, verifies target addresses whose status is `pattern_confirmed` or `pattern_guess` (never `published`) and sets `verified`/`risky`/`invalid` (`unknown` leaves the status) + `verifier_result` + `verified_at`; sets `blocked_reason` from `exclusion_for(email=address)` (`None` clears it); computes `contacts_found` (targets) and `sendable_contacts`; sets `contacts_checked_at=now`. DNS and verifier calls happen outside DB transactions; the final write is one transaction under the company row lock with an exclusion re-check.
  - `contacts.service.is_sendable(addr: EmailAddress, *, mx_state: str) -> bool` — the spec §6.2.1 rule incl. override (`override_sendable` True makes `pattern_guess`/`risky` sendable; False makes anything unsendable; `invalid` never), MX `has_mx` (free-mail always ok), and `blocked_reason is None`. Single source of truth used by counts, pages, and (later) the send gate.
  - `contacts.service.set_override(db, address_id, *, sendable: bool | None, actor: User) -> EmailAddress` — `None` clears; recomputes the company's `sendable_contacts`; flush.
  - `contacts.service.verify_batch(db_factory, *, checker, verifier, config, now, limit) -> list[VerifyOutcome]` — picks non-excluded companies with `profile_status IN ('ok','partial')` and (`contacts_checked_at IS NULL` OR older than `recheck_after_days`), oldest first, `FOR UPDATE SKIP LOCKED` claim (stamp `contacts_checked_at=now` in a short transaction; unknown-DNS outcomes reset it to NULL); one company's crash → `error="Contact check error (<ExcType>)."` and the batch continues.
  - Worker: step `contacts` after `profiling`, line "Checked contacts for N companies (M sendable addresses)." (pluralized); builds `MillionVerifier` only when `config.contacts.verifier == "millionverifier"` and the key is set (otherwise no verifier; if configured but key missing, line notes "Email verifier configured but SALESFINDER_EMAIL_VERIFIER_KEY is not set."). `WorkerDeps` gains `dns_resolve` / `verifier_transport` for tests.
  - CLI: `salesfinder contacts --company <domain>` prints people (target ★, title, category) and addresses (status, sendable yes/no + reason); `salesfinder contacts --verify --limit N` runs one `verify_batch`.
  - README: People & email section — where names come from, statuses table, sendable rule, override, optional verifier and its daily cap, no SMTP probing.

- [ ] **Step 1: Write the failing tests** — `tests/test_contacts_service.py` (real DB, FakeWeb for profiling, FakeResolver, fake verifier):

```python
async def test_profile_stores_people_emails_form(engine, db): ...
    # buyer site: /team "Priya Nair | Head of Procurement", "Anil Mehta – Owner"; /contact "priya.nair@acme.com",
    # "info@acme.com", a form -> 2 people targets (Priya rank 1), 2 published addresses (Priya's personal linked), form url
async def test_verify_learns_pattern_and_confirms(engine, db): ...
    # Anil gets "anil.mehta@acme.com" pattern_confirmed; MX has_mx -> sendable_contacts 2 (Priya published + Anil confirmed)
async def test_guess_is_held(engine, db): ...      # no published personal -> guesses pattern_guess, sendable 0 (info@ is role, not a target)
async def test_no_mail_domain_invalidates(engine, db): ...  # NXDOMAIN -> all invalid, sendable 0
async def test_dns_unknown_retries(engine, db): ...  # timeout -> statuses unchanged, contacts_checked_at None, error "DNS unavailable"
async def test_verifier_cap_and_never_published(engine, db): ...
    # cap 1, two guesses -> one verified call; published never sent to verifier
async def test_override_and_invalid(engine, db, admin): ...
    # override pattern_guess True -> sendable; invalid + override True -> not sendable; clear -> held again
async def test_exclusion_blocks_address(engine, db): ...  # DNC email -> blocked_reason set, not sendable
async def test_partial_profile_keeps_people(engine, db): ...
async def test_excluded_company_skipped(engine, db): ...
async def test_batch_isolates_crash(engine, db): ...
```

Extend `tests/test_worker.py` (contacts step line; verifier-configured-without-key line), `tests/test_cli.py` (`contacts --company acme.com` output; unknown domain exit 1).

- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Implement** service, profiling integration, worker step, CLI, README.
- [ ] **Step 4: Run to verify they pass** — full suite green.

---

### Task 7: Dashboard — contacts on company pages, list column, cohort option; full verification

**Files:**
- Modify: `web/routes/companies.py`, templates `company.html`, `companies.html`, `cohort_form.html`, `cohort.html`; `cohorts/model.py`, `cohorts/evaluate.py`, `web/cohort_form.py`; `README.md`
- Test: `tests/test_web_contacts.py`, `tests/test_cohorts.py`, `tests/test_web_cohorts.py`

**Interfaces:**
- Consumes: Task 6 services (`is_sendable`, `set_override`), models.
- Produces:
  - Company page "People & email" section (hidden for excluded companies): targets first (★, name, title, category label, source link with `rel="noopener noreferrer"` via `web_url`, evidence snippet), then other people found; addresses table: address, kind, status badge with plain-language meaning ("Published on their site", "Matches their email pattern", "Our best guess — held", "Verified", "Risky (catch-all) — held", "Won't deliver"), sendable Yes/No with reason ("No mail server", "Existing relationship: …", "Held: guess", "Overridden by <name>"), MX state line for the domain, learned pattern line ("Pattern: first.last@acme.com (from 2 addresses)"), contact form link. Override controls per address (admins and members): "Allow sending" / "Don't send" / "Clear" → `POST /companies/{id}/emails/{email_id}/override` (CSRF; audit `email.override_set`/`email.override_cleared` with old/new and address; PRG with fixed flash). `invalid` addresses show no "Allow sending" button.
  - Companies list: "Contacts" column `sendable / found` (blank when never checked).
  - Cohorts: `CohortDefinition.require_sendable_contact: bool = False`; when True only companies with `sendable_contacts > 0` match; form checkbox "Only companies with a sendable contact"; `describe()` adds "· has a sendable contact".
  - README: contacts screens.

- [ ] **Step 1: Write the failing tests** — `tests/test_web_contacts.py`:

```python
async def test_company_page_shows_people_and_statuses(client, member, db): ...  # badges text, ★ on targets, pattern line
async def test_override_flow_audited(client, member, db): ...  # allow -> sendable Yes "Overridden by"; clear -> held; audit rows
async def test_invalid_has_no_allow_button_and_route_rejects(client, member, db): ...  # POST allow on invalid -> 400
async def test_override_csrf_and_escaping(client, member, db): ...  # no token -> 403; snippet "<script>" escaped
async def test_list_contacts_column(client, member, db): ...
```

Extend `tests/test_cohorts.py` / `tests/test_web_cohorts.py` (`require_sendable_contact` filters; form round-trip; describe text).

- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run to verify they pass.**
- [ ] **Step 5: Full verification** — `make check` and the hermetic polluted-env run (add `SALESFINDER_EMAIL_VERIFIER_KEY=z`). Smoke (only if no `.env`: temporary `.env`, cleaned after; no real sites, no real DNS beyond what `doctor` already does, no verifier calls): migrate to head; throwaway admin; render `/companies` and a 404 for a random company id; remove the throwaway admin + its rows; delete the temporary `.env`.
- [ ] **Step 6: Hand back** — report `git status --short`. Do not commit.
