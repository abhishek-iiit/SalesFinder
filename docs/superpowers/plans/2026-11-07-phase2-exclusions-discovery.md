# SalesFinder Phase 2: Exclusions and Discovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Teammates can find candidate buyers and suppliers: an editable Elchemy product catalog and fact sheet, a read-only HubSpot sync plus a do-not-contact list that keep existing relationships out, and paid web-search discovery jobs that show their cost, need confirmation, respect a monthly budget, filter junk, and land in a Companies page.

**Architecture:** Pure logic (domain normalization, junk filter, query building, cost estimates, catalog/facts models) has no I/O. Services take an `AsyncSession`, flush, never commit; routes/CLI/worker commit. External APIs (HubSpot, Serper) sit behind small `httpx.AsyncClient` wrappers that tests drive with `httpx.MockTransport`. A new `salesfinder worker` process runs a poll loop (`run_once`) that executes approved discovery jobs and due HubSpot syncs; Phase 7 grows it into the full heartbeat.

**Tech Stack:** Phase 1 stack, plus `tldextract` (offline bundled suffix list) for registrable domains.

**Spec:** `docs/superpowers/specs/2026-11-07-elchemy-salesfinder-design.md` — §6.8 HubSpot, §7.1 catalog/facts files, §7.2 data model, §7.3 discovery source, §6.7 Catalog & facts / Companies / Settings screens, §4 anti-goals. Roadmap: `docs/superpowers/plans/2026-11-07-salesfinder-roadmap.md`. Phase 1 follow-ups folded in here are marked *(follow-up)*: `docs/superpowers/plans/2026-11-07-phase1-followups.md`.

## Global Constraints

- **Never run `git commit`, `git push`, `git add`, or create branches.** The user commits.
- All Phase 1 constraints still hold (Python 3.13/uv, UUID PKs, `timestamptz` UTC, ruff line 100, mypy strict, tests never touch the dev DB, never call the real `claude`).
- **No network in tests.** HubSpot and Serper are exercised only through `httpx.MockTransport`. `tldextract` must use its bundled snapshot (`suffix_list_urls=()`, no cache dir writes outside a tmp/cache path).
- HubSpot access is **read-only**: only `GET` requests to `https://api.hubapi.com`. Auth header `Authorization: Bearer <token>`.
- Secrets: `SALESFINDER_HUBSPOT_TOKEN`, `SALESFINDER_SERPER_API_KEY` are `SecretStr | None` in `Settings`; never logged, never stored in the DB, never shown in pages or audit details.
- Money is `decimal.Decimal`, stored as `Numeric(10, 4)` (USD). Budget months are UTC calendar months.
- Services flush, callers commit; every state-changing route writes an audit row in the same transaction.
- Every dropped search result, excluded company, failed sync, and failed/stopped job records a human-readable reason (spec §4: failures are findings, never silence).
- Discovery never runs without a teammate confirming the estimate, and never past the monthly budget. The worker never starts discovery on its own.
- Both pipelines' sides are exactly `buyer` and `supplier`.
- Industry keys are exactly: `personal_care`, `home_care`, `flavours_fragrance`, `food_nutrition`, `oil_gas`, `chemical_intermediates`, `water_treatment_mining`, `case`.

## Review Focus

1. **Domain spellings that are the same company** — `WWW.Acme.com.`, `https://shop.acme.com/x`, `acme.com`, `info@ACME.com`, and country suffixes like `acme.co.in` — must normalize to one registrable domain so exclusions match → tests in Task 1 and Task 4.
2. **A HubSpot sync that fails midway** (429 after retries, 5xx, timeout, bad JSON, 401) must leave existing `known_relationships` untouched, mark the sync `failed` with a reason, and never half-replace the list → tests in Task 5.
3. **The same company found twice** — twice in one job, again in a later job on the same side — is stored once; the same domain on the other side is a separate row → tests in Task 6.
4. **Budget edges** — an estimate larger than what's left this month, two jobs approved back to back, a job that would cross the budget mid-run — are refused or stopped with a message that shows the remaining amount → tests in Tasks 6 and 8.
5. **Serper misbehaving** — 401, 429, 500, malformed JSON, empty `organic` — the job ends `failed` (or retries 429/5xx up to 3 times) with the reason shown, and spend for searches that did happen is still recorded → tests in Task 6.

---

## File Structure

```
config/salesfinder.yaml          # + discovery, hubspot, worker sections
config/catalog.yaml              # Elchemy products (team-maintained)
config/facts.yaml                # the only claims about Elchemy (team-confirmed)
src/salesfinder/
  settings.py                    # + hubspot_token, serper_api_key
  config.py                      # + DiscoveryConfig, HubSpotConfig, WorkerConfig
  catalog.py                     # Product, Catalog, Facts models + load_catalog/load_facts
  industries.py                  # INDUSTRIES (key -> label), SIDES
  domains.py                     # normalize_domain, domain_of_email, domain_of_url
  discovery/
    junk.py                      # junk_reason(url, extra_domains) -> str | None
    queries.py                   # build_queries, estimate_cost
    serper.py                    # SerperClient, SerperError, SearchResult
    budget.py                    # month_spend, remaining_budget, get/set budget
    jobs.py                      # create_estimate, approve_job, cancel_job, run_job, claim_next_job
  exclusions/
    dnc.py                       # add/remove/list do-not-contact
    check.py                     # Exclusion, exclusion_for
    hubspot.py                   # HubSpotClient, HubSpotError
    sync.py                      # run_sync, request_sync, sync_health, due_for_sync
  worker.py                      # run_once, run_forever
  db/models/companies.py         # Company, DiscoveryJob, DiscoveryDrop
  db/models/exclusions.py        # KnownRelationship, HubSpotSync, DoNotContact
  db/migrations/versions/0002_exclusions_discovery.py
  web/templating.py              # (follow-up) render + template env moved out of deps.py
  web/routes/discover.py         # /discover, estimate, approve, cancel, job status
  web/routes/companies.py        # /companies, /companies/{id}
  web/routes/catalog.py          # /catalog
  web/routes/admin.py            # + /admin/settings, /admin/do-not-contact
  web/templates/                 # discover*.html, companies*.html, catalog.html, admin_settings.html, admin_dnc.html
  doctor.py                      # + hubspot, serper checks
  cli.py                         # + worker, hubspot sync, discover jobs
tests/
  test_domains.py, test_junk.py, test_catalog.py, test_queries.py,
  test_migrations.py (+ round trip), test_exclusions.py, test_hubspot.py,
  test_discovery_jobs.py, test_worker.py, test_web_discover.py,
  test_web_companies.py, test_web_admin_settings.py, test_doctor.py, test_cli.py
  fakes/hubspot.py, fakes/serper.py   # MockTransport handlers with call logs
```

---

### Task 1: Domain normalization and junk filter

**Files:**
- Create: `src/salesfinder/domains.py`, `src/salesfinder/discovery/__init__.py`, `src/salesfinder/discovery/junk.py`
- Modify: `pyproject.toml` (+ `tldextract>=5.1`), `uv.lock`
- Test: `tests/test_domains.py`, `tests/test_junk.py`

**Interfaces:**
- Produces:
  - `normalize_domain(raw: str) -> str | None` — accepts a host, URL, or bare domain; lowercases, strips scheme/path/port/trailing dot/`www.`, and returns the registrable domain (`shop.acme.co.in` → `acme.co.in`). Returns `None` for IPs, `localhost`, empty, or no public suffix.
  - `domain_of_url(url: str) -> str | None` (= `normalize_domain`), `domain_of_email(email: str) -> str | None` (part after the last `@`, normalized).
  - A module-level `tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None)` instance (offline).
  - `junk_reason(url: str, extra_domains: Sequence[str] = ()) -> str | None` — `None` keeps the result; otherwise one of the reason strings below.
  - Built-in junk rules (reason → match):
    - `"marketplace"`: indiamart.com, tradeindia.com, alibaba.com, made-in-china.com, exportersindia.com, amazon.*, ebay.*, globalsources.com, ec21.com, go4worldbusiness.com
    - `"directory"`: thomasnet.com, yelp.com, yellowpages.com, manta.com, bbb.org, zoominfo.com, dnb.com, crunchbase.com, kompass.com, justdial.com, chemicalbook.com, echemi.com, lookchem.com, guidechem.com
    - `"social"`: linkedin.com, facebook.com, instagram.com, twitter.com, x.com, youtube.com, pinterest.com, tiktok.com, reddit.com
    - `"reference_or_news"`: wikipedia.org, sciencedirect.com, researchgate.net, pubchem.ncbi.nlm.nih.gov host, reuters.com, prnewswire.com, businesswire.com, globenewswire.com, any host whose registrable domain contains `news`
    - `"government_or_education"`: suffix `gov`, `mil`, `edu`, or second level `gov.*`, `gc.*`, `nic.in`, `ac.*`, `edu.*`
    - `"document"`: path ends with `.pdf`, `.doc`, `.docx`, `.xls`, `.xlsx`, `.ppt`
    - `"no_domain"`: `normalize_domain` returns `None`
    - `"configured"`: registrable domain in `extra_domains`
  - Matching is on registrable domain (so `in.linkedin.com` is social); `amazon.*`/`ebay.*` match any suffix.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_domains.py
import pytest
from salesfinder.domains import domain_of_email, normalize_domain

@pytest.mark.parametrize("raw", ["acme.com", "WWW.Acme.com.", "https://shop.acme.com/x?y=1",
                                 "http://acme.com:8080", " acme.COM "])
def test_same_company(raw):
    assert normalize_domain(raw) == "acme.com"

def test_country_suffix():
    assert normalize_domain("https://www.sales.acme.co.in/") == "acme.co.in"

@pytest.mark.parametrize("raw", ["", "localhost", "192.168.1.4", "http://10.0.0.1/a", "not a host"])
def test_none(raw):
    assert normalize_domain(raw) is None

def test_email_domain():
    assert domain_of_email("Info@Mail.ACME.com") == "acme.com"
    assert domain_of_email("no-at-sign") is None
```

```python
# tests/test_junk.py
import pytest
from salesfinder.discovery.junk import junk_reason

@pytest.mark.parametrize("url,reason", [
    ("https://dir.indiamart.com/x", "marketplace"),
    ("https://www.amazon.in/dp/1", "marketplace"),
    ("https://www.thomasnet.com/a", "directory"),
    ("https://in.linkedin.com/company/a", "social"),
    ("https://en.wikipedia.org/wiki/SLES", "reference_or_news"),
    ("https://chemnews.com/a", "reference_or_news"),
    ("https://www.epa.gov/x", "government_or_education"),
    ("https://www.iitb.ac.in/", "government_or_education"),
    ("https://acme.com/spec-sheet.PDF", "document"),
    ("http://127.0.0.1/", "no_domain"),
])
def test_junk(url, reason):
    assert junk_reason(url) == reason

def test_real_company_kept():
    assert junk_reason("https://www.acmecosmetics.com/products") is None

def test_configured_extra():
    assert junk_reason("https://shop.badsite.com", extra_domains=["badsite.com"]) == "configured"
```

- [ ] **Step 2: Run to verify they fail** — `uv run pytest tests/test_domains.py tests/test_junk.py -q` → FAIL (`ModuleNotFoundError`).
- [ ] **Step 3: Add `tldextract`, implement `domains.py` and `junk.py`** per the Interfaces block. Rules live in module-level constants (tuples), checked in the order listed above after `no_domain`.
- [ ] **Step 4: Run to verify they pass** — same command → all PASS; `uv run mypy src` clean.

---

### Task 2: Catalog, facts, and new config sections

**Files:**
- Create: `src/salesfinder/industries.py`, `src/salesfinder/catalog.py`, `config/catalog.yaml`, `config/facts.yaml`
- Modify: `src/salesfinder/config.py`, `config/salesfinder.yaml`, `src/salesfinder/settings.py`, `.env.example`
- Test: `tests/test_catalog.py`, `tests/test_config.py` (extend)

**Interfaces:**
- Consumes: `deep_merge`, `ConfigError`, the YAML reading helpers in `config.py`.
- Produces:
  - `industries.SIDES = ("buyer", "supplier")`; `industries.INDUSTRIES: dict[str, str]` = `{"personal_care": "Personal Care", "home_care": "Home Care & Industrial Cleaning", "flavours_fragrance": "Flavours & Fragrance", "food_nutrition": "Food & Nutrition", "oil_gas": "Oil & Gas", "chemical_intermediates": "Chemical Intermediates", "water_treatment_mining": "Water Treatment & Mining", "case": "C.A.S.E. (Coatings, Adhesives, Sealants, Elastomers)"}`.
  - `catalog.Product(BaseModel, extra="forbid")`: `id: str` (slug `^[a-z0-9_]+$`), `name: str`, `search_name: str`, `synonyms: list[str] = []`, `inci: str | None = None`, `cas: list[str] = []` (each `^\d{2,7}-\d{2}-\d$`), `industries: list[str]` (keys of `INDUSTRIES`, ≥1), `sides: list[Literal["buyer","supplier"]] = ["buyer","supplier"]`, `confirmed: bool = False`.
  - `catalog.Catalog(products: list[Product])` with unique ids and `by_id(id) -> Product` (KeyError) and `for_side(side) -> list[Product]`.
  - `catalog.Claim(id: str, text: str, confirmed: bool = False)`; `catalog.Persona(name: str = "", title: str = "", signature: str = "", confirmed: bool = False)`; `catalog.Warehouse(city: str, state: str, country: str = "US")`; `catalog.Facts(company_name: str, website: str, postal_address: str = "", postal_address_confirmed: bool = False, warehouses: list[Warehouse], claims: list[Claim], personas: dict[Literal["buyer","supplier"], Persona])`.
  - `load_catalog(config_dir: Path) -> Catalog`, `load_facts(config_dir: Path) -> Facts` — raise `ConfigError` naming file + key (same style as `load_config`). No `.local.yaml` merge for these files (the team edits them directly).
  - `config.Region(name: str, country: str, gl: str)`; `config.DiscoveryConfig`: `serper_price_per_1000_usd: Decimal = Decimal("1.00")`, `monthly_budget_usd: Decimal = Decimal("20.00")`, `pages_per_query: int = 1` (1–3), `results_per_page: int = 10` (fixed at 10 by validator), `max_queries_per_job: int = 200`, `templates: dict[Literal["buyer","supplier"], str]` defaults `{"buyer": '"{chemical}" {keyword} manufacturer {region}', "supplier": '"{chemical}" manufacturer exporter {region}'}` (validator: buyer template must contain `{chemical}`, `{keyword}`, `{region}`; supplier `{chemical}`, `{region}`), `industry_keywords: dict[str, list[str]]` (keys must be `INDUSTRIES` keys), `buyer_regions: list[Region]`, `supplier_regions: list[Region]`, `junk_domains: list[str] = []`.
  - `config.HubSpotConfig`: `sync_interval_hours: int = 24`, `stale_after_hours: int = 48`. `config.WorkerConfig`: `poll_seconds: int = 10`. `AppConfig` gains `discovery`, `hubspot`, `worker`.
  - `Settings` gains `hubspot_token: SecretStr | None = None`, `serper_api_key: SecretStr | None = None` (empty string → `None`).
- Seed values (put in YAML exactly; every product `confirmed: false`):

| id | name | search_name | cas | industries |
|---|---|---|---|---|
| sles | Sodium Laureth Sulfate (SLES 70%) | sodium laureth sulfate | 68585-34-2, 9004-82-4 | personal_care, home_care |
| capb | Cocamidopropyl Betaine (CAPB) | cocamidopropyl betaine | 61789-40-0 | personal_care, home_care |
| labsa | Linear Alkylbenzene Sulfonic Acid (LABSA 96%) | LABSA | 27176-87-0, 85536-14-7 | home_care |
| np9 | Nonylphenol Ethoxylate 9 EO | nonylphenol ethoxylate | 9016-45-9 | home_care, oil_gas |
| caustic_soda | Caustic Soda (Sodium Hydroxide) Flakes / Prills | caustic soda | 1310-73-2 | home_care, water_treatment_mining, chemical_intermediates |
| soda_ash | Soda Ash (Sodium Carbonate) | soda ash | 497-19-8 | home_care, water_treatment_mining |
| koh | Potassium Hydroxide | potassium hydroxide | 1310-58-3 | home_care, chemical_intermediates |
| hcl | Hydrochloric Acid | hydrochloric acid | 7647-01-0 | water_treatment_mining, oil_gas, chemical_intermediates |
| cetyl_alcohol | Cetyl Alcohol | cetyl alcohol | 36653-82-4 | personal_care |
| cetostearyl_alcohol | Cetostearyl Alcohol | cetostearyl alcohol | 67762-27-0, 8005-44-5 | personal_care |
| glycerine | Glycerine | glycerine | 56-81-5 | personal_care, food_nutrition |
| citric_acid | Citric Acid | citric acid | 77-92-9, 5949-29-1 | food_nutrition, home_care |
| polysorbate_80 | Polysorbate 80 | polysorbate 80 | 9005-65-6 | personal_care, food_nutrition |
| polysorbate_20 | Polysorbate 20 | polysorbate 20 | 9005-64-5 | personal_care, flavours_fragrance |
| silica | Precipitated Silica | precipitated silica | 112926-00-8 | case, personal_care |
| xylene | Xylene | xylene | 1330-20-7 | case, chemical_intermediates |
| mineral_oil | White Mineral Oil | white mineral oil | 8042-47-5 | personal_care, case |
| green_tea_extract | Green Tea Extract | green tea extract | 84650-60-2 | food_nutrition, personal_care |
| monk_fruit_extract | Monk Fruit Extract | monk fruit extract | (none) | food_nutrition |
| pepper_oleoresin | Black Pepper Oleoresin | black pepper oleoresin | (none) | food_nutrition, flavours_fragrance |

  Also add synonyms where obvious (SLES: "SLES", "sodium lauryl ether sulfate"; CAPB: "CAPB"; caustic soda: "sodium hydroxide"; soda ash: "sodium carbonate"; glycerine: "glycerin", "glycerol"; LABSA: "linear alkylbenzene sulfonic acid", "dodecylbenzenesulfonic acid") and INCI names for personal-care items.
- `facts.yaml` seed: `company_name: "Elchemy"`, `website: "https://elchemy.com"`, `postal_address: ""` (`postal_address_confirmed: false`), warehouses Bridgewater NJ; Houston TX; Illinois (city `""`, state IL); Santa Fe Springs CA; claims (all `confirmed: false`): `sample_dispatch` "Samples dispatched within 24–48 hours", `us_stock` "Stocked in US warehouses in New Jersey, Texas, Illinois and California", `documentation` "CoA and SDS supplied with every order", `certifications` "Kosher and Halal grades available on select products", `direct_import` "Bulk direct imports at competitive landed cost"; personas `buyer` and `supplier` with empty fields. A YAML comment at the top: "Only confirmed facts may be used in emails (Phase 5). Confirm each item with the team."
- `config/salesfinder.yaml` seed for `discovery`: the defaults above; `industry_keywords`: personal_care ["shampoo", "skin care", "cosmetics"], home_care ["household cleaner", "detergent", "industrial cleaning"], flavours_fragrance ["fragrance", "flavor"], food_nutrition ["food ingredients", "nutraceutical"], oil_gas ["oilfield chemicals"], chemical_intermediates ["specialty chemicals"], water_treatment_mining ["water treatment", "mining chemicals"], case ["coatings", "adhesives", "sealants"]; `buyer_regions` (country `US`, gl `us`): New Jersey, New York, Pennsylvania, Texas, Illinois, Ohio, California, Georgia; `supplier_regions`: Gujarat (IN/in), Maharashtra (IN/in), Tamil Nadu (IN/in), Telangana (IN/in), Thailand (TH/th), Malaysia (MY/my), Indonesia (ID/id), Vietnam (VN/vn).

- [ ] **Step 1: Write the failing tests** — `tests/test_catalog.py`:

```python
def test_shipped_catalog_and_facts_load():
    cat = load_catalog(Path("config")); facts = load_facts(Path("config"))
    assert cat.by_id("sles").cas == ["68585-34-2", "9004-82-4"]
    assert len(cat.products) == 20 and all(not p.confirmed for p in cat.products)
    assert {w.state for w in facts.warehouses} == {"NJ", "TX", "IL", "CA"}
    assert all(not c.confirmed for c in facts.claims)

def test_bad_cas_names_file_and_key(tmp_path): ...      # cas "12-3" -> ConfigError mentioning "catalog.yaml" and "cas"
def test_unknown_industry_rejected(tmp_path): ...       # industries ["plastics"] -> ConfigError "plastics"
def test_duplicate_product_ids_rejected(tmp_path): ...  # ConfigError "duplicate"
def test_for_side(tmp_path): ...                        # product with sides ["buyer"] absent from for_side("supplier")
```

Extend `tests/test_config.py`: shipped `salesfinder.yaml` loads with 8 buyer regions and 8 supplier regions; a buyer template missing `{keyword}` → `ConfigError` mentioning `templates`; `pages_per_query: 4` → `ConfigError`; `industry_keywords` with an unknown key → `ConfigError`. Extend `tests/test_settings.py` (keep its env-clearing fixture): `hubspot_token=""` → `None`; set value is a `SecretStr` whose `repr` hides the token.

- [ ] **Step 2: Run to verify they fail** — `uv run pytest tests/test_catalog.py tests/test_config.py tests/test_settings.py -q` → FAIL.
- [ ] **Step 3: Implement** the models, loaders, YAML seeds, config sections and settings fields; document both new env vars in `.env.example`.
- [ ] **Step 4: Run to verify they pass** — same command → PASS.

---

### Task 3: Data model and migration 0002

**Files:**
- Create: `src/salesfinder/db/models/exclusions.py`, `src/salesfinder/db/models/companies.py`, `src/salesfinder/db/migrations/versions/0002_exclusions_discovery.py`
- Modify: `src/salesfinder/db/models/__init__.py`, `tests/test_migrations.py`
- Test: `tests/test_migrations.py`

**Interfaces:**
- Produces (table names exact; all with `UUIDPk` unless noted; `created_at`/`updated_at` via `Timestamps` where listed):
  - `KnownRelationship` → `known_relationships`: `source: str(16)` (check `IN ('hubspot')`), `kind: str(8)` (check `IN ('domain','email')`), `value: str(320)` (normalized), `hubspot_object: str(16)` (`company`|`contact`), `hubspot_id: str(32)`, `name: str(200) | None`, `lifecycle_stage: str(64) | None`, `owner_name: str(200) | None`, `synced_at: timestamptz`. Unique `(source, kind, value)` named `uq_known_relationships_source_kind_value`; index on `value`.
  - `HubSpotSync` → `hubspot_syncs`: `status: str(16)` (check `IN ('queued','running','succeeded','failed')`), `requested_by_id` FK users nullable, `created_at`, `started_at | None`, `finished_at | None`, `companies_count: int = 0`, `contacts_count: int = 0`, `error: str(500) | None`. Index on `(status, finished_at)`.
  - `DoNotContact` → `do_not_contact`: `kind` (check `IN ('domain','email')`), `value: str(320)`, `reason: str(200)`, `added_by_id` FK users nullable, `created_at`. Unique `(kind, value)`.
  - `DiscoveryJob` → `discovery_jobs` (+ Timestamps): `side` (check `IN ('buyer','supplier')`), `params: JSONB` (`{"products": [...], "industries": [...], "regions": [...]}`), `queries: JSONB` (list of `{"q": str, "gl": str, "region": str, "country": str}`), `pages_per_query: int`, `estimated_searches: int`, `estimated_cost_usd: Numeric(10,4)`, `status: str(16)` (check `IN ('estimated','approved','running','succeeded','failed','stopped_budget','cancelled')`), `created_by_id` FK users, `approved_by_id | None`, `approved_at | None`, `started_at | None`, `finished_at | None`, `searches_done: int = 0`, `actual_cost_usd: Numeric(10,4) = 0`, `results_seen: int = 0`, `companies_new: int = 0`, `companies_existing: int = 0`, `companies_excluded: int = 0`, `results_dropped: int = 0`, `error: str(500) | None`. Index on `(status, approved_at)`.
  - `Company` → `companies` (+ Timestamps): `side`, `name: str(300)`, `domain: str(255)`, `website_url: str(2048)`, `country: str(2) | None`, `region: str(100) | None`, `source: str(16)` = `serper`, `discovered_by_job_id` FK discovery_jobs nullable (`ON DELETE SET NULL`), `discovery_query: str(500) | None`, `snippet: str(1000) | None`, `status: str(16)` (check `IN ('new','excluded')` — later phases add values), `exclusion_reason: str(32) | None` (`existing_relationship` | `do_not_contact`), `exclusion_detail: str(300) | None`. Unique `(side, domain)` named `uq_companies_side_domain`; index on `(side, status)`.
  - `DiscoveryDrop` → `discovery_drops`: `job_id` FK discovery_jobs `ON DELETE CASCADE` (indexed), `url: str(2048)`, `domain: str(255) | None`, `reason: str(32)`, `created_at`.
- *(follow-up)* migration round-trip helper in `db/migrate.py`: `async def downgrade_to(database_url: str, revision: str) -> None` (same injected-connection pattern as `upgrade_to_head`).

- [ ] **Step 1: Write the failing tests** — extend `tests/test_migrations.py`:

```python
async def test_round_trip(database_url, engine):
    await downgrade_to(database_url, "0001")
    current, _ = await current_and_head(database_url)
    assert current == "0001"
    await upgrade_to_head(database_url)
    current, head = await current_and_head(database_url)
    assert current == head == "0002"

async def test_company_unique_per_side(db): ...
    # Company(side="buyer", domain="acme.com", ...) twice -> IntegrityError;
    # same domain with side="supplier" -> OK
async def test_status_checks(db): ...
    # DiscoveryJob(status="bogus") -> IntegrityError; Company(status="bogus") -> IntegrityError
async def test_drops_cascade_with_job(db): ...
    # delete job -> its discovery_drops rows gone; company.discovered_by_job_id becomes NULL
```

(`test_models_match_migrations` from Phase 1 must keep passing — it now covers the new tables.)

- [ ] **Step 2: Run to verify they fail** — `uv run pytest tests/test_migrations.py -q` → FAIL.
- [ ] **Step 3: Implement models, `downgrade_to`, and autogenerate `0002`** (`uv run alembic revision --autogenerate -m exclusions_discovery --rev-id 0002` against the empty-at-0001 dev DB, then hand-review: check constraints, named uniques, `ON DELETE` clauses, `Numeric(10,4)` server default `0`, JSONB). Remove Alembic's "please adjust!" comments in 0002 (and in 0001 *(follow-up)*). Leave the dev DB at head afterwards.
- [ ] **Step 4: Run to verify they pass** — `uv run pytest tests/test_migrations.py -q` → PASS.

---

### Task 4: Do-not-contact list and the exclusion check

**Files:**
- Create: `src/salesfinder/exclusions/__init__.py`, `exclusions/dnc.py`, `exclusions/check.py`
- Modify: `src/salesfinder/audit.py` (+ constants)
- Test: `tests/test_exclusions.py`

**Interfaces:**
- Consumes: `normalize_domain`, `domain_of_email`, `emails.normalize_email`; models from Task 3.
- Produces:
  - `dnc.add_entry(db, *, raw: str, reason: str, added_by: User | None) -> DoNotContact` — `raw` containing `@` is an email (normalized with `normalize_email`), else a domain (`normalize_domain`); raises `ValueError("Enter an email address or a domain.")` when neither normalizes; `ValueError("Already on the list.")` on duplicate (check before insert); `reason` required, stripped, ≤200 chars.
  - `dnc.remove_entry(db, entry_id: uuid.UUID) -> DoNotContact` (`LookupError` if missing); `dnc.list_entries(db) -> list[DoNotContact]` (newest first).
  - `check.Exclusion` (frozen dataclass): `reason: Literal["existing_relationship", "do_not_contact"]`, `detail: str` (e.g. `"HubSpot company Acme Corp (owner: Priya Shah)"`, `"Do-not-contact: acme.com — Asked us not to email"`).
  - `check.exclusion_for(db, *, domain: str | None = None, email: str | None = None) -> Exclusion | None` — normalizes inputs; checks, in order: DNC email, DNC domain (the email's domain too), known-relationship email, known-relationship domain (the email's domain too, **unless** that domain is a free-mail provider: gmail.com, yahoo.com, outlook.com, hotmail.com, icloud.com, aol.com, proton.me, protonmail.com, rediffmail.com). Returns the first match.
  - Audit constants: `DNC_ADDED = "dnc.added"`, `DNC_REMOVED = "dnc.removed"`, `HUBSPOT_SYNC_REQUESTED = "hubspot.sync_requested"`, `DISCOVERY_ESTIMATED = "discovery.estimated"`, `DISCOVERY_APPROVED = "discovery.approved"`, `DISCOVERY_CANCELLED = "discovery.cancelled"`, `SETTINGS_UPDATED = "settings.updated"`.

- [ ] **Step 1: Write the failing tests** — `tests/test_exclusions.py`:

```python
async def test_add_dnc_normalizes(db): ...
    # add_entry(raw=" https://WWW.Acme.co.in/x ", reason="asked") -> kind "domain", value "acme.co.in"
    # add_entry(raw="Bob@ACME.com", ...) -> kind "email", value "bob@acme.com"
async def test_add_dnc_rejects_garbage_and_duplicates(db): ...
    # raw "not a thing" -> ValueError "Enter an email address or a domain."
    # same domain twice (different spelling) -> ValueError "Already on the list."
async def test_exclusion_dnc_domain_covers_emails_and_subdomains(db): ...
    # DNC domain acme.com -> exclusion_for(email="x@sales.acme.com").reason == "do_not_contact"
    # exclusion_for(domain="https://shop.acme.com") -> do_not_contact
async def test_exclusion_known_relationship(db): ...
    # KnownRelationship(kind="domain", value="acme.com", name="Acme", owner_name="Priya Shah")
    # -> exclusion_for(domain="ACME.com").reason == "existing_relationship" and "Priya Shah" in detail
async def test_free_mail_domain_not_matched_by_contact_domain(db): ...
    # KnownRelationship(kind="email", value="joe@gmail.com") -> exclusion_for(email="amy@gmail.com") is None
    # but exclusion_for(email="Joe@Gmail.com") is not None
async def test_dnc_wins_over_known(db): ...  # both present -> reason "do_not_contact"
async def test_no_match(db): ...             # -> None
```

- [ ] **Step 2: Run to verify they fail** — `uv run pytest tests/test_exclusions.py -q` → FAIL.
- [ ] **Step 3: Implement** per the Interfaces block (exact-match lookups on normalized `value`).
- [ ] **Step 4: Run to verify they pass** — same → PASS.

---

### Task 5: HubSpot read-only sync

**Files:**
- Create: `src/salesfinder/exclusions/hubspot.py`, `src/salesfinder/exclusions/sync.py`, `tests/fakes/__init__.py`, `tests/fakes/hubspot.py`
- Test: `tests/test_hubspot.py`

**Interfaces:**
- Consumes: Task 3 models, `normalize_domain`, `normalize_email`, `HubSpotConfig`.
- Produces:
  - `hubspot.HubSpotError(Exception)` with `.reason: str` (human-readable, never contains the token).
  - `hubspot.HubSpotClient(token: str, *, transport: httpx.AsyncBaseTransport | None = None, base_url: str = "https://api.hubapi.com", max_retries: int = 3, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep)`; async context manager; methods:
    - `iter_companies() -> AsyncIterator[dict[str, Any]]` — `GET /crm/v3/objects/companies?limit=100&properties=name,domain,website,hs_additional_domains,lifecyclestage,hubspot_owner_id&archived=false`, following `paging.next.after`.
    - `iter_contacts() -> AsyncIterator[dict[str, Any]]` — same for contacts with `properties=email,firstname,lastname,lifecyclestage,hubspot_owner_id`.
    - `owners() -> dict[str, str]` — `GET /crm/v3/owners?limit=100` (paged) → `{id: "First Last" or email}`; a 403 here returns `{}` (owner names are optional, scope may be missing).
    - Retries 429 and 5xx up to `max_retries` (honouring `Retry-After` seconds, else 2^attempt), 20 s timeout per request. 401 → `HubSpotError("HubSpot rejected the token (401). Check SALESFINDER_HUBSPOT_TOKEN.")`; 403 on companies/contacts → `HubSpotError("HubSpot token lacks a required scope (403): crm.objects.companies.read / crm.objects.contacts.read.")`; exhausted retries → `HubSpotError("HubSpot unavailable after 3 retries (HTTP <code>).")`; timeout/connection → `HubSpotError("Could not reach HubSpot: <ExcType>.")`; non-JSON → `HubSpotError("HubSpot returned an unexpected response.")`.
  - `sync.request_sync(db, *, requested_by: User | None) -> HubSpotSync` — inserts a `queued` row unless one is already `queued` or `running` (then returns that one).
  - `sync.due_for_sync(db, *, now: datetime, config: HubSpotConfig) -> bool` — True when no `running` sync and (a `queued` row exists, or no `succeeded` sync finished within `sync_interval_hours`).
  - `sync.run_sync(db_factory: async_sessionmaker[AsyncSession], *, token: str, now: Callable[[], datetime], transport: httpx.AsyncBaseTransport | None = None) -> HubSpotSync` — claims the oldest `queued` row (or creates one), marks it `running` and **commits** (its own sessions: this is a worker job, the documented exception to "services don't commit"); fetches everything into memory; then in **one transaction** upserts all rows (company domains from `domain`, `website` host, and each `;`-separated `hs_additional_domains` entry; contact emails), deletes `source='hubspot'` rows not seen in this sync, sets counts, `succeeded`, `finished_at`, commits. On `HubSpotError` (or any exception) the replace transaction never starts / is rolled back; the sync row is set `failed` with `error` (≤500 chars) in a separate transaction. Rows that don't normalize are skipped and counted in the log.
  - `sync.SyncHealth` (frozen dataclass): `state: Literal["not_configured","never_synced","fresh","stale","failing"]`, `last_success_at: datetime | None`, `message: str`. `sync.sync_health(db, *, now: datetime, token_configured: bool, config: HubSpotConfig) -> SyncHealth` — `fresh` when the last success finished within `stale_after_hours`; `failing` when the most recent finished sync failed (message includes its error); `stale` otherwise. Phase 6 blocks cold sending unless `state == "fresh"`.
  - `tests/fakes/hubspot.py`: `FakeHubSpot(companies: list[dict], contacts: list[dict], owners: list[dict], fail: dict[str, int] | None = None)` exposing `.transport` (`httpx.MockTransport`) and `.calls: list[httpx.Request]`; pages of 2 items to exercise paging; `fail={"companies": 500}` makes that endpoint fail with that status.

- [ ] **Step 1: Write the failing tests** — `tests/test_hubspot.py` (use a no-op `sleep`):

```python
async def test_sync_upserts_and_resolves_owner(engine, db): ...
    # 3 companies (one with website only "https://www.beta.co.in/", one with
    # hs_additional_domains "gamma.com;gamma-labs.com"), 3 contacts, owners [{id:"7", firstName:"Priya", lastName:"Shah"}]
    # run_sync -> status "succeeded", companies_count 3, contacts_count 3
    # known_relationships contains domain "beta.co.in", "gamma-labs.com", email values lowercased
    # owner_name "Priya Shah" on rows with hubspot_owner_id "7"
async def test_paging_followed(engine): ...      # fake pages of 2 -> all 5 companies stored; calls include after=
async def test_resync_removes_deleted(engine, db): ...  # second sync without one company -> its row gone
async def test_failure_keeps_existing_rows(engine, db): ...
    # successful sync, then FakeHubSpot(fail={"contacts": 500}) -> status "failed",
    # error mentions "HubSpot unavailable after 3 retries (HTTP 500)", row count unchanged
async def test_401_message_has_no_token(engine, db): ...  # error has "401", token string absent
async def test_429_retry_then_success(engine): ...        # first 429 with Retry-After: 1, then 200 -> succeeded
async def test_owner_scope_missing_is_ok(engine): ...     # owners 403 -> succeeded, owner_name None
async def test_request_sync_dedupes(db): ...              # two requests -> one queued row
async def test_due_for_sync_and_health(db): ...
    # no syncs -> due True, health "never_synced"; success 2h ago -> due False, "fresh";
    # success 30h ago -> due True; success 50h ago -> "stale"; latest failed -> "failing";
    # token_configured=False -> "not_configured"
```

- [ ] **Step 2: Run to verify they fail** — `uv run pytest tests/test_hubspot.py -q` → FAIL.
- [ ] **Step 3: Implement** client, fake, and sync per the Interfaces block. Only `GET` is ever issued (assert in the fake: any other method → test failure).
- [ ] **Step 4: Run to verify they pass** — same → PASS.

---

### Task 6: Discovery — queries, estimates, budget, Serper, job runner

**Files:**
- Create: `src/salesfinder/discovery/queries.py`, `discovery/serper.py`, `discovery/budget.py`, `discovery/jobs.py`, `tests/fakes/serper.py`
- Test: `tests/test_queries.py`, `tests/test_discovery_jobs.py`

**Interfaces:**
- Consumes: `Catalog`, `DiscoveryConfig`, `INDUSTRIES`, `junk_reason`, `normalize_domain`, `exclusion_for`, Task 3 models, `AppSetting`.
- Produces:
  - `queries.Query` (frozen dataclass: `q: str, gl: str, region: str, country: str`).
  - `queries.build_queries(*, side: str, product_ids: Sequence[str], industries: Sequence[str], region_names: Sequence[str], catalog: Catalog, config: DiscoveryConfig) -> list[Query]` — buyer: product × industry (only industries the product lists; for each, every keyword in `industry_keywords`) × region; supplier: product × region (industries ignored). `{chemical}` = `search_name`. Dedupe identical `q`+`gl`, preserve order. `ValueError` for unknown product/region/industry, empty selections, or more than `max_queries_per_job` (message: `"That's {n} searches; the limit per job is {max}. Narrow the products, industries or regions."`).
  - `queries.estimate_cost(n_queries: int, config: DiscoveryConfig) -> tuple[int, Decimal]` — `(searches, cost)` with `searches = n_queries * pages_per_query`, `cost = searches * price_per_1000 / 1000` quantized to `0.0001`.
  - `serper.SearchResult` (frozen: `title: str, url: str, snippet: str, position: int`); `serper.SerperError(Exception)` with `.reason` and `.retryable: bool`; `serper.SerperClient(api_key: str, *, transport=None, max_retries: int = 3, sleep=asyncio.sleep)` with `search(q: str, *, gl: str, page: int) -> list[SearchResult]` — `POST https://google.serper.dev/search`, header `X-API-KEY`, JSON `{"q": q, "gl": gl, "num": 10, "page": page}`; parses `organic` (missing → `[]`); 401/403 → `SerperError("Serper rejected the API key.")`; 429/5xx retried with backoff, then `SerperError("Serper unavailable after 3 retries (HTTP <code>).")`; bad JSON → `SerperError("Serper returned an unexpected response.")`; never includes the key.
  - `budget.BUDGET_KEY = "discovery.monthly_budget_usd"`; `get_budget(db, config) -> Decimal` (app_settings override else config); `set_budget(db, value: Decimal, *, actor: User) -> None` (≥0, ≤10000, upsert); `month_spend(db, *, now) -> Decimal` = Σ `actual_cost_usd` of this UTC month's jobs in `running/succeeded/failed/stopped_budget` + Σ `estimated_cost_usd` of `approved` jobs; `remaining_budget(db, *, now, config) -> Decimal`.
  - `jobs.create_estimate(db, *, side, product_ids, industries, region_names, created_by: User, catalog, config) -> DiscoveryJob` — builds queries, stores status `estimated`.
  - `jobs.approve_job(db, job_id, *, approved_by: User, now, config) -> DiscoveryJob` — locks the job row (`FOR UPDATE`, `populate_existing`) and serializes approvals with `SELECT pg_advisory_xact_lock(4242)`; only from `estimated`; refuses with `ValueError(f"This job needs ${est:.4f}; ${remaining:.2f} of this month's ${budget:.2f} budget is left.")` when `estimate > remaining`; sets `approved`, `approved_by_id`, `approved_at`.
  - `jobs.cancel_job(db, job_id, *, actor: User) -> DiscoveryJob` — only from `estimated`/`approved`.
  - `jobs.claim_next_job(db) -> DiscoveryJob | None` — oldest `approved` by `approved_at`, `FOR UPDATE SKIP LOCKED`, sets `running`, `started_at`.
  - `jobs.fail_interrupted_jobs(db, *, now, older_than: timedelta = timedelta(hours=1)) -> int` — `running` jobs with `started_at` older than that → `failed`, `error="Interrupted (worker stopped mid-job)."`.
  - `jobs.run_job(db_factory, job_id: uuid.UUID, *, client: SerperClient, catalog, config, now: Callable[[], datetime]) -> DiscoveryJob` — for each query and page 1..`pages_per_query`: before each search, stop with `stopped_budget` if `actual_cost + per_search > remaining_at_start` (remaining computed at start excluding this job's own estimate); call `search`; on success add `per_search` to `actual_cost_usd`, `searches_done += 1`; for each result: `results_seen += 1`; `junk_reason` (with `config.junk_domains`) → `DiscoveryDrop` + `results_dropped += 1`; else domain already a company on this side → `companies_existing += 1` (no duplicate, also within the same job); else `exclusion_for(domain=...)` → company with `status="excluded"`, `exclusion_reason`, `exclusion_detail`, `companies_excluded += 1`; else `status="new"`, `companies_new += 1`. Company fields: `name` = result title before the first ` - `/` | `/` – ` (trimmed, ≤300), `website_url` = `https://{host}` of the result, `country`/`region` from the query, `discovery_query` = `q`, `snippet` (≤1000). Commit after each search (progress is visible and spend survives a crash). `SerperError` that isn't retryable, or after retries → `failed` with `error=reason`. Ends `succeeded` otherwise; `finished_at` set in every terminal state.
  - `tests/fakes/serper.py`: `FakeSerper(responses: dict[str, list[dict]] | Callable, status_by_call: list[int] | None = None)` with `.transport` and `.calls`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_queries.py  (catalog/config built in-test from the shipped YAML)
def test_buyer_queries_cross_product_keyword_region():
    qs = build_queries(side="buyer", product_ids=["sles"], industries=["personal_care"],
                       region_names=["Texas"], catalog=cat, config=cfg)
    assert [q.q for q in qs] == ['"sodium laureth sulfate" shampoo manufacturer Texas',
                                 '"sodium laureth sulfate" skin care manufacturer Texas',
                                 '"sodium laureth sulfate" cosmetics manufacturer Texas']
    assert {q.gl for q in qs} == {"us"}
def test_buyer_skips_industries_the_product_lacks(): ...
    # sles + industries ["personal_care", "oil_gas"] -> only the 3 personal_care queries
    # sles + industries ["oil_gas"] only -> ValueError(
    #   "Those choices produce no searches. Pick an industry each product belongs to.")
def test_supplier_queries(): ...  # capb x Gujarat -> ['"cocamidopropyl betaine" manufacturer exporter Gujarat'], gl "in", country "IN"
def test_limit(): ...             # > max_queries_per_job -> ValueError with "limit per job is 200"
def test_estimate_cost(): ...     # estimate_cost(30, cfg with pages 2, price 1.00) == (60, Decimal("0.0600"))
```

```python
# tests/test_discovery_jobs.py
async def test_estimate_approve_run_happy_path(engine, db, admin): ...
    # FakeSerper: q1 returns acme.com, junk indiamart, acme.com again (dup), excluded beta.com (DNC)
    # -> succeeded; companies_new 1, companies_excluded 1, results_dropped 1, companies_existing 1
    # Company acme.com: status "new", name parsed from title, country "US", region "Texas"
    # discovery_drops has reason "marketplace"; actual_cost == searches_done * 0.001
async def test_existing_company_same_side_not_duplicated_other_side_separate(engine, db, admin): ...
async def test_approve_over_budget_refused(db, admin): ...
    # budget 0.05, nothing spent, estimate 0.06 -> ValueError exactly
    # "This job needs $0.0600; $0.05 of this month's $0.05 budget is left."
    # (estimate shown with 4 decimals, remaining and budget with 2)
async def test_two_approvals_cannot_overspend(db, admin): ...
    # budget 0.10, two estimates of 0.06 -> first approved, second refused
async def test_run_stops_at_budget(engine, db, admin): ...
    # budget lowered after approval so only 2 of 5 searches fit -> "stopped_budget", searches_done 2
async def test_serper_401_fails_job_with_reason(engine, db, admin): ...  # failed, error "Serper rejected the API key."
async def test_serper_500_retries_then_fails_keeps_spend(engine, db, admin): ...
    # first search 200, second always 500 -> failed after 3 retries; searches_done 1; actual_cost 0.001
async def test_malformed_and_empty_organic(engine, db, admin): ...  # bad JSON -> failed; {} -> succeeded with 0 results
async def test_claim_skip_locked_and_interrupted(db, admin): ...
    # claim_next_job returns oldest approved; fail_interrupted_jobs marks 2h-old running job failed
async def test_cancel_only_before_running(db, admin): ...
```

- [ ] **Step 2: Run to verify they fail** — `uv run pytest tests/test_queries.py tests/test_discovery_jobs.py -q` → FAIL.
- [ ] **Step 3: Implement** queries, serper client + fake, budget, jobs per the Interfaces block.
- [ ] **Step 4: Run to verify they pass** — same → PASS.

---

### Task 7: Worker process, CLI commands, doctor checks

**Files:**
- Create: `src/salesfinder/worker.py`
- Modify: `src/salesfinder/cli.py`, `src/salesfinder/doctor.py`, `README.md`
- Test: `tests/test_worker.py`, `tests/test_doctor.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: `run_sync`, `due_for_sync`, `claim_next_job`, `run_job`, `fail_interrupted_jobs`, `sync_health`, loaders.
- Produces:
  - `worker.WorkerDeps` (dataclass): `session_factory`, `settings: Settings`, `config: AppConfig`, `catalog: Catalog`, `now: Callable[[], datetime]`, `hubspot_transport: httpx.AsyncBaseTransport | None = None`, `serper_transport: ... | None = None`.
  - `async def run_once(deps: WorkerDeps) -> list[str]` — returns human-readable lines of what it did. Order: (1) `fail_interrupted_jobs`; (2) if `hubspot_token` set and `due_for_sync` → `run_sync`; (3) if `serper_api_key` set, claim one job and `run_job` it; if an approved job exists but no key → mark it `failed` with `error="SALESFINDER_SERPER_API_KEY is not set."`. Each step isolated: an exception in one is logged and reported as a line, the next still runs.
  - `async def run_forever(deps, *, stop: asyncio.Event) -> None` — loops `run_once` then waits `config.worker.poll_seconds` or until `stop` is set; SIGINT/SIGTERM set `stop`.
  - CLI: `salesfinder worker` (runs `run_forever`, logs each line); `salesfinder hubspot sync` (one sync now, prints counts or the error, exit 1 on failure); `salesfinder discover jobs` (table of the 20 latest jobs: created, side, status, searches done/estimated, cost, new/excluded/dropped).
  - Doctor: two new checks appended in this order: `hubspot` (fails with `"SALESFINDER_HUBSPOT_TOKEN is not set — cold sending will stay paused."` when missing; otherwise passes/fails from `sync_health`: pass only when `fresh`, detail = its message) and `serper` (fails with `"SALESFINDER_SERPER_API_KEY is not set — discovery jobs can't run."` when missing; else passes `"key configured"`). No network calls in doctor. Update the Phase 1 doctor tests that assert the six names to the new eight.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_worker.py
async def test_run_once_runs_due_sync_and_approved_job(engine, db, admin): ...
    # token + key set, fakes wired; approved job queued -> lines mention "HubSpot sync succeeded" and "Discovery job";
    # job status succeeded, a hubspot_syncs row succeeded
async def test_run_once_never_starts_unapproved_jobs(engine, db, admin): ...  # "estimated" job untouched
async def test_missing_serper_key_fails_approved_job(engine, db, admin): ...
async def test_one_step_failing_does_not_block_next(engine, db, admin): ...
    # HubSpot fake raises -> sync failed, but approved job still runs
async def test_run_forever_stops(engine): ...  # stop event set -> returns within poll_seconds
```

Extend `tests/test_doctor.py`: names list is now `["config","database","migrations","admin_user","claude_cli","https","hubspot","serper"]`; missing token/key messages exact; token set + fresh success row → `hubspot` ok. Extend `tests/test_cli.py`: `hubspot sync` with no token → exit 1 and message naming the env var; `discover jobs` with no jobs → exit 0 and "No discovery jobs yet."

- [ ] **Step 2: Run to verify they fail** — `uv run pytest tests/test_worker.py tests/test_doctor.py tests/test_cli.py -q` → FAIL.
- [ ] **Step 3: Implement** worker, CLI commands, doctor checks; README: new env vars, `salesfinder worker` (run it alongside `salesfinder web`), how to create the HubSpot private app (scopes `crm.objects.companies.read`, `crm.objects.contacts.read`, optional `crm.objects.owners.read`), Serper signup and pricing note.
- [ ] **Step 4: Run to verify they pass** — same → PASS.

---

### Task 8: Dashboard — Discover, Companies, Catalog

**Files:**
- Create: `src/salesfinder/web/templating.py`, `web/routes/discover.py`, `web/routes/companies.py`, `web/routes/catalog.py`, templates `discover.html`, `discover_job.html`, `_job_status.html`, `companies.html`, `company.html`, `catalog.html`
- Modify: `web/deps.py`, `web/app.py`, `web/templates/base.html` (nav), `web/static/app.css`
- Test: `tests/test_web_discover.py`, `tests/test_web_companies.py`

**Interfaces:**
- Consumes: Task 2 loaders, Task 6 services, Phase 1 deps (`AuthDep`, `CsrfDep`, `DbDep`).
- Produces:
  - *(follow-up)* `web/templating.py` holds the Jinja environment and `render(...)`; `deps.py` re-exports nothing it no longer owns; all routes import `render` from `templating`.
  - *(follow-up)* `create_app` loads `AppConfig`, `Catalog`, `Facts` once into `app.state.config/catalog/facts` (a `ConfigError` at startup is raised, not swallowed); deps `ConfigDep`, `CatalogDep`, `FactsDep`.
  - *(follow-up)* `optional_auth` updates `last_seen_at` only when it's older than 60 s.
  - *(follow-up)* `LoginRequired` handler returns `200` with header `HX-Redirect: /login` when the request has `HX-Request: true`.
  - Nav (signed in): Today, Discover, Companies, Catalog, then admin links.
  - `GET /discover` — form: side (radio), products (checkboxes, filtered by side via `for_side`, grouped by industry label), industries (checkboxes; buyer only — note on page "ignored for suppliers"), regions (checkboxes from the side's region list); shows this month's spend, budget, remaining; lists the 20 latest jobs.
  - `POST /discover/estimate` (CSRF) → creates the estimate, audits `discovery.estimated` (details: side, searches, cost), redirects 303 to `/discover/jobs/{id}`; `ValueError` re-renders the form with the message (400) and keeps selections.
  - `GET /discover/jobs/{id}` — status, the first 20 queries (and "+N more"), searches, estimated cost, remaining budget, and for `estimated`: a "Run — spend about $X" button (`POST /discover/jobs/{id}/approve`) and Cancel (`POST .../cancel`); for `approved`/`running`: a status fragment that polls `GET /discover/jobs/{id}/status` every 3 s via `hx-get`/`hx-trigger="every 3s"` (fragment `_job_status.html`, stops polling in a terminal state by omitting the trigger); terminal states show counts, error, and a link to the job's companies.
  - Approve/cancel (CSRF) audit `discovery.approved`/`discovery.cancelled`; approve `ValueError` → job page with the budget message (400).
  - `GET /companies?side=buyer|supplier&status=new|excluded&job={id}&q=<text>&page=N` — 50 per page, newest first; columns name, domain (link to website, `rel="noopener noreferrer"`), country/region, status (+ exclusion reason), found by (job link); tabs Buyers/Suppliers with counts.
  - `GET /companies/{id}` — fields above + snippet, discovery query, exclusion detail.
  - `GET /catalog` — products table (name, CAS, INCI, industries, sides, confirmed ✓/✗) and the facts sheet with confirmed ✓/✗ per item and the note "Only confirmed facts will be used in emails. Edit config/catalog.yaml and config/facts.yaml, then restart."
  - All pages require login; members and admins both allowed.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_web_discover.py
async def test_discover_page_lists_side_products_and_budget(client, member): ...
    # GET /discover -> 200, contains "Sodium Laureth Sulfate", "Remaining this month"
async def test_estimate_then_job_page_shows_cost_and_run_button(client, member, db): ...
    # POST estimate side=buyer products=sles industries=personal_care regions=Texas
    # -> 303 /discover/jobs/<id>; page contains '"sodium laureth sulfate" shampoo manufacturer Texas',
    #    "Run — spend about $0.0030"; audit has "discovery.estimated"
async def test_estimate_error_keeps_form(client, member): ...  # no products -> 400 with message, side kept checked
async def test_approve_and_budget_refusal(client, member, db): ...
    # set_budget 0.00 -> approve -> 400 with "budget is left"; set_budget 1 -> approve -> 303, status approved, audit
async def test_cancel(client, member, db): ...
async def test_status_fragment_and_htmx_redirect(client, member, db): ...
    # GET /discover/jobs/<id>/status -> 200 fragment (no <html>); approved job fragment has hx-trigger;
    # succeeded job fragment has no hx-trigger
    # logged-out GET with HX-Request: true -> HX-Redirect: /login
async def test_csrf_required(client, member): ...  # POST estimate without token -> 403
```

```python
# tests/test_web_companies.py
async def test_companies_tabs_filters_and_paging(client, member, db): ...
    # 3 buyer new, 1 buyer excluded, 1 supplier -> /companies?side=buyer shows 4, tab counts "Buyers (4)" "Suppliers (1)";
    # status=excluded shows the reason; q=acme filters by name/domain; 55 rows -> page 2 has 5
async def test_company_detail_escapes_snippet(client, member, db): ...  # snippet "<script>x</script>" rendered escaped
async def test_catalog_page(client, member): ...  # contains "68585-34-2" and the confirm note
async def test_last_seen_throttled(client, member, db): ...  # two GETs within 60s -> last_seen_at unchanged
```

- [ ] **Step 2: Run to verify they fail** — `uv run pytest tests/test_web_discover.py tests/test_web_companies.py -q` → FAIL.
- [ ] **Step 3: Implement** templating split, app-state config, routes, templates and CSS (no inline scripts/styles; HTMX attributes only).
- [ ] **Step 4: Run to verify they pass** — same, then the full Phase 1 web suite still passes.

---

### Task 9: Admin — settings (budget, HubSpot) and do-not-contact

**Files:**
- Create: templates `admin_settings.html`, `admin_dnc.html`
- Modify: `web/routes/admin.py`, `web/templates/base.html`, `web/templates/error.html` + `web/app.py` *(follow-up: nav on error pages)*
- Test: `tests/test_web_admin_settings.py`

**Interfaces:**
- Consumes: `get_budget/set_budget/month_spend`, `sync_health`, `request_sync`, `dnc` service.
- Produces:
  - `GET /admin/settings` — monthly discovery budget form (`POST /admin/settings/budget`, value in dollars, 2 decimals, 0–10000; audit `settings.updated` with old/new), this month's spend; HubSpot panel: `sync_health` message, last success time, last 5 syncs (status, counts, error), and "Sync now" (`POST /admin/hubspot/sync` → `request_sync`, audit `hubspot.sync_requested`, flash "Sync queued — the worker will run it within a minute."); when the token is missing the panel says so and hides the button.
  - `GET /admin/do-not-contact` — list (value, kind, reason, added by, date) + add form (`POST /admin/do-not-contact`, fields `entry`, `reason`; audit `dnc.added`) + remove buttons (`POST /admin/do-not-contact/{id}/remove`; audit `dnc.removed`). Adding a domain also re-marks matching `new` companies (both sides) as `excluded` with reason `do_not_contact` in the same transaction; the flash says how many.
  - Nav (admin): Users, Settings, Do-not-contact, Audit log.
  - *(follow-up)* error pages render with the nav when the requester is signed in.
  - All POSTs CSRF-protected and admin-only.

- [ ] **Step 1: Write the failing tests** — `tests/test_web_admin_settings.py`:

```python
async def test_member_forbidden(client, member): ...                  # GET /admin/settings -> 403
async def test_budget_update_audited(client, admin, db): ...          # POST 35.50 -> get_budget == Decimal("35.50"); audit old/new
async def test_budget_validation(client, admin): ...                  # "-1" and "abc" -> 400 with message
async def test_hubspot_panel_without_token(client, admin): ...        # "SALESFINDER_HUBSPOT_TOKEN is not set", no Sync button
async def test_sync_now_queues_once(client_with_hubspot_token, admin, db): ...  # two POSTs -> one queued row, audit
async def test_dnc_add_remove_and_reexclude(client, admin, db): ...
    # Company buyer acme.com status new -> add "acme.com" -> company excluded, flash "1 company"
    # add garbage -> 400 "Enter an email address or a domain."; remove -> row gone, audit dnc.removed
async def test_error_page_keeps_nav_when_signed_in(client, member): ...  # GET /admin/users as member -> 403 page has "Sign out"
```

(`client_with_hubspot_token`: a fixture variant whose `test_settings` sets `hubspot_token`.)

- [ ] **Step 2: Run to verify they fail** — `uv run pytest tests/test_web_admin_settings.py -q` → FAIL.
- [ ] **Step 3: Implement** routes, templates, nav, error-page nav.
- [ ] **Step 4: Run to verify they pass** — same → PASS.
- [ ] **Step 5: Full verification** — `make check` → ruff clean, mypy clean, all tests pass; hermetic run `SALESFINDER_BIND_HOST=0.0.0.0 SALESFINDER_HUBSPOT_TOKEN=x SALESFINDER_SERPER_API_KEY=y uv run pytest -q` → all pass. Manual smoke (dev DB, temporary `.env` if none exists, cleaned up afterwards, no real API keys): `make migrate`; create a throwaway admin; `salesfinder doctor` shows the two new checks failing with their "not set" messages; `salesfinder web` → sign in → `/discover` estimate a buyer job → job page shows cost and Run button; `/catalog` renders; then remove the throwaway admin and its rows.
- [ ] **Step 6: Hand back** — report `git status --short`. Do not commit.
