# SalesFinder for Elchemy — Design

Date: 2026-11-07
Status: Draft, awaiting approval

## 1. Intent

Elchemy (elchemy.com) is a cross-border specialty chemical distributor. It buys
from audited manufacturers in India, Southeast Asia and the USA and sells to US
commercial end-users and regional distributors, out of US warehouses in New
Jersey (Bridgewater), Houston, Illinois and Santa Fe Springs, CA.

Its growth depends on two kinds of relationships:

- **Customers (buyers):** US companies that consume the chemicals Elchemy
  stocks, across its eight industries: Personal Care, Home Care & Industrial
  Cleaning, Flavours & Fragrance, Food & Nutrition, Oil & Gas, Chemical
  Intermediates, Water Treatment & Mining, and C.A.S.E. (coatings, adhesives,
  sealants, elastomers).
- **Suppliers:** manufacturers, mainly in India and Southeast Asia, that make
  those chemicals at export quality and can join Elchemy's supplier network.

SalesFinder is an internal outreach engine for Elchemy's sales and sourcing
team. It finds both kinds of company, works out why each is a fit, finds the
person who decides, checks their email address, drafts a personal email, sends
it after a human approves, and handles the replies. One engine, two pipelines,
one dashboard.

It is modeled on the design of Sellbird (github.com/ethanplusai/sellbird): a
deterministic heartbeat loop, a user-confirmed signal catalog, free website
profiling, an approval-gated outbox, and Claude called through the operator's
own subscription. **Sellbird is AGPL-3.0, so we borrow its ideas, not its
code.** Everything here is written fresh.

## 2. Goals

1. A steady, explainable list of qualified **buyers** and **suppliers**, each
   with the specific reasons it qualifies (signals) recorded as evidence.
2. Decision makers and verified email addresses for those companies, found
   without paid prospecting tools (no Apollo, ZoomInfo, Clearbit, Clay).
3. Personal first emails and follow-ups that cite real facts about the
   recipient and only true facts about Elchemy.
4. Nothing leaves without human approval (autopilot can be enabled later,
   per pipeline, by the operator).
5. Replies are read, classified (interested, objection, not now, wrong person,
   unsubscribe, auto-reply), and drafted answers wait for approval.
   Unsubscribes take effect immediately.
6. Runs on one local machine using the operator's **personal Claude
   subscription** through the Claude Code CLI (no Anthropic API key).
7. Production-quality codebase: typed, tested, migrated schema, CI, clear
   docs, 12-factor config, so moving to a server or Docker later is mechanical.

## 3. Non-goals (for this version)

- Not a marketplace or portal. Suppliers and customers never log in.
- No Docker or cloud deployment yet. Local server only (decided by the user).
- No Anthropic API billing path (decided by the user). The brain module keeps a
  narrow interface so an API backend can be added later.
- No LinkedIn automation, no scraping of sites whose terms forbid it
  (LinkedIn, IndiaMART/TradeIndia/Alibaba listings, ZoomInfo, Crunchbase).
  Facts come from companies' own websites and a search provider.
- No phone or voice outreach. No SMS/WhatsApp.
- No CRM replacement. A CSV export (and later a HubSpot push) covers handoff.
- No multi-tenant / multi-company support.

## 4. Anti-goals (would be a failure even if it "works")

- Damaging the deliverability or reputation of elchemy.com (sending cold email
  from the main domain, high bounce rates, spam complaints).
- Emails that state anything false about Elchemy: invented prices, stock,
  lead times, certifications, or customers. The writer may only use facts from
  the Elchemy fact sheet (§7.3) and the recipient's own profile.
- Contacting anyone who opted out, or a company the team marked do-not-contact.
- Text from a scraped website or a reply steering what the system sends
  (prompt injection). Model output is always checked by deterministic code
  before it can reach the outbox.
- A list the team can't explain. Every company shows why it is on the list.
- Silent failure. "Search provider throttled" or "no team page found" is
  recorded and shown, never mistaken for an empty market.

## 5. Constraints

- **Brain:** Claude Code CLI on the operator's Pro/Max subscription, called the
  way Sellbird does it: `claude -p --output-format json --tools "" 
  --strict-mcp-config --safe-mode --setting-sources "" --disable-slash-commands
  --no-session-persistence`, prompt on stdin, fresh empty temp working
  directory, instruction-loading env vars stripped. Refuse to call if any flag
  is missing from `claude --help`. Verified present in installed CLI 2.1.319.
- **Budget:** a configurable daily cap on Claude calls (default 150/day) and a
  heartbeat that sleeps in quiet hours, leaving headroom for the operator's own
  Claude use.
- **Host:** one macOS machine, Python 3.13 (Homebrew), Postgres 17 (running
  locally), `uv` for environments. The worker runs on this machine because the
  Claude subscription login lives here. **Several teammates** use the
  dashboard (decided by the user), so it is reachable over the office network
  (§6.7), not only on 127.0.0.1.
- **Sending domain:** a **new, separate sending domain** (e.g.
  elchemy-sourcing.com) will be bought and warmed up; it does not exist yet
  (decided by the user). Nothing cold is sent from elchemy.com. Sending stays
  paused until DNS and warm-up checks pass (§6.5).
- **Existing relationships:** Elchemy's current customers and suppliers live
  in **HubSpot** (decided by the user) and must never receive cold email (§6.8).
- **Compliance:** US CAN-SPAM for buyers (accurate headers, physical address,
  working opt-out honoured within 10 days; we honour it immediately). Supplier
  outreach to India/SEA is B2B and also carries an opt-out line. A
  do-not-contact list (emails and domains) is checked before every send.
- **Licensing:** no Sellbird code copied (AGPL-3.0).

## 6. How it works

### 6.1 Heartbeat

A worker process (`salesfinder run`) wakes every 15 minutes (configurable):

```
wake → quiet hours? → budget left? → decide → act → log → sleep
```

The decision is deterministic, not a model call:

```
handle replies > send approved mail > queue due follow-ups > write drafts
  > enrich/verify > profile > idle
```

Each step is idempotent and resumable; a crash mid-step loses nothing. Every
run writes a `runs` row with what it did, counts, cost, and any failures.

**Discovery is never run by the loop** (Claude's call). It spends money, so a
teammate starts it from the dashboard or CLI: pick side, products, industries
and regions → see the number of searches and estimated cost → confirm. The
job then runs in the worker and its results flow into profiling.

**Campaigns** (Claude's call): a campaign = side + cohort + playbook +
mailbox + persona. A teammate creates and activates it. The loop drafts only
for companies in active campaigns, up to a per-day drafting cap (default 40
first emails/day) so Claude usage stays inside budget. Every message —
first email, follow-up, reply — goes through the outbox for approval in v1;
a follow-up is drafted and re-queued for approval when it falls due.

### 6.2 The two pipelines

Both pipelines share stages and tables; a `side` column (`buyer` | `supplier`)
selects ICP, signals, search templates, titles, prompts and templates.

| Stage | Buyer pipeline | Supplier pipeline |
|---|---|---|
| **Discover** | Web search with templates built from catalog × industry × US region, e.g. `"sodium laureth sulfate" shampoo manufacturer Texas`, `private label household cleaner manufacturer New Jersey`. | Web search with catalog × producing region, e.g. `cocamidopropyl betaine manufacturer exporter Gujarat`, `caustic soda flakes manufacturer India export`. |
| **Profile** (free HTTP, no model) | Read homepage, products, about, contact, careers. Detect industry fit, chemicals they use (by name, synonym, INCI name, CAS), company type, US location. | Read homepage, products, about, quality/certifications, contact. Detect chemicals made (name/CAS), manufacturer vs trader, certifications, export markets, stated capacity. |
| **Score** (deterministic) | Weighted rules over confirmed signals → 0–100 fit score. | Same, supplier weights. |
| **People** | From the company's own pages: Purchasing/Procurement Manager, Supply Chain Manager, R&D/Formulation lead; owner/president for small firms. Max 2 per company. | Export Manager, Director/Managing Director, Business Development, Marketing Head. Max 2 per company. |
| **Verify email** | See §6.2.1. | Same. |
| **Write** (Claude) | 2-email sequence: first email + one follow-up after 4 business days. Angle: fast samples (24–48h), US stock near them, landed cost, documentation (CoA/SDS). | 2-email sequence. Angle: invitation to supply a US distributor, request for price, CoA/TDS and MOQ for named products they make. |
| **Gate** (deterministic) | See §6.4. | Same. |
| **Outbox** | Human approves, edits, or rejects. | Same. |
| **Replies** (Claude) | Classify intent, draft answer, hand off "interested" to a human with a summary. | Same; "interested" suppliers get flagged for the sourcing team with the products they offered. |

#### 6.2.1 Email verification (Claude's call)

No SMTP RCPT probing: home/office ISPs usually block port 25 and probing
hurts sender reputation. Statuses:

- `published` — the exact address appears on the company's own site.
- `pattern_confirmed` — built from a pattern learned from at least one other
  address published on the same domain, and the domain has MX records.
- `pattern_guess` — common-pattern guess with MX but no evidence on the domain.
- `verified` / `risky` / `invalid` — only produced by the optional paid
  verifier (pluggable; e.g. MillionVerifier or ZeroBounce) when a key is set.

Sendable by default: `published`, `pattern_confirmed`, `verified`.
`pattern_guess` and `risky` are held unless a teammate overrides per address.
The bounce kill switch (§6.5) is the backstop.

### 6.3 Signals (confirmed by the user before collection)

Signals are stored as **observations**: `(company, signal_code, value, evidence
url, evidence snippet, observed_at, source)`. Re-observation over time is
supported by the schema (change detection is a later feature).

Initial catalog (all free; collected only after the user confirms them in the
dashboard or CLI):

Buyer signals:
- `INDUSTRY_MATCH` — which Elchemy industries the site matches.
- `USES_CATALOG_CHEMICAL` — catalog chemicals named on their site (with which).
- `COMPANY_TYPE` — manufacturer / contract manufacturer / private label /
  distributor / retailer / service. Retailers and services are disqualifying by default.
- `US_LOCATION` — state/city from their site.
- `NEAR_WAREHOUSE` — nearest Elchemy warehouse and rough distance.
- `HIRING_PROCUREMENT` — careers page lists purchasing/supply-chain/formulation roles.
- `QUALITY_CERTS` — ISO 9001, GMP, NSF, cGMP, etc. stated on site.

Supplier signals:
- `MAKES_CATALOG_CHEMICAL` — catalog chemicals they manufacture (name/CAS).
- `MANUFACTURER_NOT_TRADER` — owns a plant (plant address, capacity, "our
  manufacturing facility") vs trader/agent language.
- `EXPORTS` / `EXPORTS_TO_US` — export markets stated.
- `SUPPLIER_CERTS` — ISO 9001/14001/45001, REACH, Kosher, Halal, FSSC 22000,
  GMP, RSPO, ECOCERT.
- `STATED_CAPACITY` — MT/month or MT/year when stated.
- `REGION` — country/state (Gujarat, Maharashtra, Tamil Nadu, etc.).

Shared contact signals: `CONTACT_FOUND`, `DECISION_MAKER_TITLE`,
`EMAIL_PATTERN`, `EMAIL_STATUS`, `CONTACT_FORM_URL`.

A **cohort** is a saved query over signals (must have / must not have / score
range), e.g. *buyer, Personal Care, uses SLES or CAPB, manufacturer, within 500
miles of NJ*. Campaigns are written per cohort, so every email has a reason.

### 6.4 Deterministic pre-send gate

Runs on every draft (first emails, follow-ups and replies) before it can enter
the outbox, and again just before sending. Rejects with a visible reason when:

- unfilled merge tags or template braces remain;
- banned phrases/claims appear (configurable list: price figures, "guarantee",
  "lowest price", certifications not in the fact sheet, etc.);
- body exceeds the length limit (default 140 words for first email);
- more than one link, or any link to a domain other than elchemy.com (and the
  configured booking link);
- recipient is on do-not-contact, suppressed (unsubscribed/bounced), invalid,
  or already in an active sequence from the other pipeline;
- compliance footer (company name, postal address, opt-out line) is missing;
- the draft quotes the system's own instructions (injection echo check).

### 6.5 Sending and replies

- **Mailboxes:** Google Workspace mailboxes on the new sending domain, via
  SMTP (send) + IMAP (read) with app passwords. Several mailboxes are
  supported (`mailboxes` table); each campaign uses one. Expected setup
  (Claude's call): one sales persona mailbox for buyers and one sourcing
  persona mailbox for suppliers, each with its own name, title and signature
  in `facts.yaml`. Provider sits behind an interface; Microsoft 365 later.
- **Domain readiness:** `doctor` and the dashboard check SPF, DKIM and DMARC
  for the sending domain and MX for each mailbox. Cold sending is blocked
  until these pass.
- **Warm-up ramp:** each mailbox has a warm-up start date (set when an
  external warm-up service is switched on). No cold email for the first 14
  days; then the cap starts at 5/day and rises by 3/day up to the mailbox's
  max (default 30, configurable up to 50). Caps are **per mailbox**. Replies
  to people who wrote to us are never held by the ramp.
- **Links:** emails may link to elchemy.com (product pages, sample request)
  and the configured booking link; the footer names Elchemy and its postal
  address so the recipient sees who is writing despite the sending domain.
- **Limits:** per-recipient office hours (US time zones for buyers; IST/SEA
  time zones for suppliers), weekdays only, random spacing between sends.
- **Bounce kill switch:** pause all sending if the bounce rate over the last
  50 sends exceeds 5%. `salesfinder sending pause` stops everything at once.
- **Threading:** follow-ups and replies use `In-Reply-To`/`References` so they
  thread correctly.
- **Reply handling:** IMAP poll each heartbeat; match to conversation by
  Message-ID headers; auto-replies set aside; unsubscribe → suppression list
  immediately, no further mail; others classified and a drafted answer goes to
  the outbox. "Interested" and anything the model is unsure about are flagged
  "needs you" on the dashboard and assigned to a teammate.
- **Human takeover** (Claude's call): a teammate can answer inside
  SalesFinder (sent from the campaign mailbox, through the gate) or directly
  from that mailbox in Gmail. The worker also reads each mailbox's Sent folder;
  once a human-sent message appears in a thread, automation stops for that
  person: no more drafts, and pending follow-ups are cancelled.

### 6.6 The brain (Claude subscription)

- `brain.complete(task, system_prompt, user_content, schema) -> result`: one
  isolated CLI call; output requested as JSON and validated with Pydantic;
  one retry on invalid JSON, then the item is marked failed with the reason.
- Untrusted text (scraped pages, replies) is wrapped in clearly delimited
  blocks and labeled as data. The model has no tools, so an injected
  instruction has nothing to act with; the gate catches what it might write.
- Tasks: `write_sequence`, `classify_reply`, `draft_reply`, `summarize_fit`
  (optional, only for top-scored companies). Everything else is plain code.
- Every call logs task, duration, token/cost fields from the CLI JSON, and
  success/failure to `usage_events`; the daily budget reads from it.
- Prompts and sales playbooks are editable Markdown in `prompts/` and
  `playbooks/` (buyer and supplier variants).

### 6.7 Dashboard

FastAPI app serving server-rendered pages (Jinja2 + HTMX, no JS build step).

**Multi-user** (decided by the user: several teammates):
- User accounts with email + password (argon2id hashing), server-side
  sessions in Postgres, secure/HttpOnly/SameSite cookies, CSRF tokens on
  every form, login rate limiting. Invitations are created by an admin; no
  public sign-up. `salesfinder users create-admin` bootstraps the first user.
- Roles: `admin` (settings, caps, mailboxes, users, autopilot, HubSpot) and
  `member` (discover, confirm signals, build cohorts/campaigns, approve,
  edit, reject, reply).
- **Audit log** of who approved, edited, rejected, sent, overrode an email
  status, changed settings, or started a paid discovery run.
- **Network:** binds to 127.0.0.1 by default. LAN mode (config) binds the
  office-network interface and requires HTTPS (a locally trusted certificate,
  e.g. mkcert, or a reverse proxy); the app refuses LAN mode over plain HTTP.
  Host/Origin checks against DNS rebinding. Not exposed to the internet in v1.

Screens:
- **Today:** needs-you items, outbox count, sends today vs cap, replies, last
  run status and any failures.
- **Companies:** Buyers / Suppliers tabs, filters by signal and score; company
  page shows signals with evidence links, people, email status, history.
- **Signals & cohorts:** confirm/skip signals; build cohorts with live counts.
- **Outbox:** approve, edit, reject (with reason, fed back to the writer),
  bulk approve.
- **Conversations:** threads with intent labels and drafted answers.
- **Catalog & facts:** view the product catalog and Elchemy fact sheet (edited
  as YAML files; the page shows what the writer is allowed to say).
- **Runs:** heartbeat log, failures, Claude usage vs budget, discovery spend.
- **Settings:** sending pause/resume, caps, mailboxes and warm-up status,
  domain DNS checks, autopilot per pipeline (off by default), do-not-contact
  list, HubSpot connection and last sync, users, audit log, CSV export.

### 6.8 HubSpot: existing customers and suppliers

Decided by the user: HubSpot holds the existing relationships.

- A HubSpot private-app token (read scopes for companies and contacts) is
  stored in `.env`.
- The worker syncs daily (and on demand) into `known_relationships`:
  company domains, contact emails, HubSpot lifecycle stage/type and owner.
- **Before discovery results are kept** and **before every send**, the domain
  and address are checked against `known_relationships` and do-not-contact.
  Matches are kept out of cold outreach and shown as "existing relationship"
  with the HubSpot owner. A failed or stale sync (older than 48h) pauses cold
  sending, because an outdated exclusion list is not safe.
- v1 is read-only. Pushing interested leads into HubSpot is a later feature.

## 7. Configuration and data

### 7.1 Files

- `.env` — secrets only (DB URL, mailbox credentials, search API key,
  dashboard token salt). `.env.example` documents every variable.
- `config/salesfinder.yaml` — tracked defaults (caps, quiet hours, weights).
- `config/salesfinder.local.yaml` — operator overrides, git-ignored.
- `config/catalog.yaml` — Elchemy products: name, synonyms, INCI name, CAS
  number(s), industries, grades. Seeded with ~25 products from elchemy.com
  (SLES, CAPB, LABSA 96%, caustic soda flakes/prills, soda ash, KOH, HCl,
  cetyl and cetostearyl alcohol, glycerine, citric acid, polysorbates, silica,
  xylene, mineral oil, etc.). The team maintains it.
- `config/facts.yaml` — the only claims the writer may make about Elchemy
  (warehouses, sample dispatch time, documentation, compliance statements,
  persona name/title/signature, postal address for the footer). Seeded from
  the public website and marked for the team to confirm.

### 7.2 Data model (Postgres, Alembic migrations)

`companies` (side, name, domain, country, region, source, score, status),
`observations`, `signal_definitions`, `signal_confirmations`, `people`,
`email_addresses` (status, evidence), `email_patterns`, `cohorts`, `campaigns`,
`sequences`/`messages` (draft → gated → approved → sent → bounced/replied),
`conversations`, `outbox_feedback`, `suppressions`, `do_not_contact`,
`known_relationships`, `hubspot_syncs`, `mailboxes`, `users`, `sessions`,
`audit_log`, `discovery_jobs` (estimate, approved by, actual spend), `runs`,
`usage_events`, `settings`.

Unique constraints prevent duplicates: one company per normalized domain per
side (a company can legitimately be both buyer and supplier, kept as two
rows linked by domain); one sendable sequence per email address.

### 7.3 Discovery source

- **Default: Serper.dev** web search (2,500 free searches, then ~$0.30–1.00
  per 1,000). Every discovery job is started by a teammate, shows the
  estimated number of searches and cost, and runs only after confirmation.
  An admin-set monthly search budget hard-stops jobs that would exceed it.
- Provider interface allows adding DataForSEO or others later.
- Junk filter: directories, marketplaces (IndiaMART, TradeIndia, Alibaba,
  Made-in-China), news, Wikipedia, government and education domains are
  dropped by pattern; the drop reason is recorded.

## 8. Engineering practices

- **Stack:** Python 3.13, `uv`, FastAPI, Jinja2 + HTMX, SQLAlchemy 2 (async)
  + Alembic, Postgres 17, httpx, selectolax/BeautifulSoup, dnspython,
  aiosmtplib + aioimaplib, Pydantic v2 + pydantic-settings, Typer CLI,
  structlog.
- **Layout:** `src/salesfinder/` package with `config/`, `db/`, `brain/`,
  `discovery/`, `profile/`, `signals/`, `people/`, `email/`, `writer/`,
  `gate/`, `mail/`, `replies/`, `loop/`, `web/`, `cli.py`. Pure logic
  separated from I/O so it's unit-testable.
- **Tests:** pytest + pytest-asyncio; TDD for gate, scoring, signal
  detectors, email pattern learning, reply matching, decision logic. HTML
  fixtures for profilers. A fake Claude CLI and fake SMTP/IMAP servers for
  integration tests; a test Postgres database. No network in tests.
- **Quality:** ruff (lint + format), mypy (strict on core modules),
  pre-commit hooks, GitHub Actions CI running lint, types and tests against a
  Postgres service container.
- **Security:** secrets only in `.env` (git-ignored); dashboard local-only
  with token auth; SMTP header-injection safe; all external text treated as
  untrusted; dependency pinning via `uv.lock`.
- **Ops:** `salesfinder doctor` checks DB, migrations, Claude CLI flags and
  login, mailbox connectivity, search key, DNS for the sending domain
  (SPF/DKIM/DMARC). Structured logs to file and console. `make`/`just`
  targets for setup, dev, test, run.
- **Deployability:** runs as two processes (`salesfinder web`,
  `salesfinder run`) configured entirely by env + YAML, so a later Docker
  image or systemd/launchd service is a thin wrapper. A launchd plist example
  is included for keeping it running on the Mac.
- **Docs:** README (setup in under 15 minutes), `docs/` for configuration,
  signals, sending and deliverability, troubleshooting, architecture.

## 9. Build order (milestones)

1. Foundation: repo, tooling, config loading, DB + migrations, CLI skeleton,
   `doctor`, CI.
2. Users, sessions, roles, audit log; HubSpot sync into
   `known_relationships`; do-not-contact.
3. Catalog + facts + discovery jobs (Serper) with estimates, budget and junk
   filter, excluding known relationships.
4. Profiler + signal detectors + confirmation + scoring + cohorts.
5. People finder + email pattern learning + verification.
6. Brain (isolated Claude CLI) + campaigns + writer + gate + outbox.
7. Dashboard screens (on top of the auth from milestone 2).
8. Mailboxes, DNS readiness, warm-up ramp, sending (SMTP), follow-ups,
   bounce kill switch, IMAP replies + Sent-folder takeover detection, reply
   classification and drafting, suppressions.
9. Heartbeat loop wiring, LAN/HTTPS mode, launchd example, docs, end-to-end
   run against fakes, then a real dry run with sending paused.

## 10. Left to the builder

- Exact scoring weights and thresholds (start simple, make them config).
- Exact search query templates (start with catalog × industry × region).
- HTML parsing approach and heuristics per signal.
- Dashboard visual design (clean, legible, light/dark).
- Internal module boundaries beyond §8.

## 11. Calls made by Claude (not yet confirmed by the user)

- Project name "SalesFinder" (from the folder name).
- Postgres over SQLite (it's installed and running; closer to a later server deploy).
- Serper as the default discovery provider.
- Google Workspace via SMTP/IMAP as the first mail provider.
- HTMX server-rendered dashboard instead of a React SPA.
- Default caps: 150 Claude calls/day, 40 first-email drafts/day, 30
  sends/day per mailbox after warm-up, 2 people per company, 2-email sequences.
- Human approval required for every outgoing email in v1, follow-ups included.
- Discovery only runs when a teammate starts it; the loop never spends.
- No SMTP probing; sendable statuses are `published`, `pattern_confirmed`,
  `verified` (§6.2.1).
- Campaigns are created and activated by a teammate; drafting only for
  active campaigns.
- One sales mailbox for buyers and one sourcing mailbox for suppliers.
- Human replies (in SalesFinder or straight from Gmail) stop automation for
  that person.
- A HubSpot sync older than 48h pauses cold sending.
- LAN access requires HTTPS; no internet exposure in v1.
