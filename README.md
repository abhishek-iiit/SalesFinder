# SalesFinder

SalesFinder is Elchemy's internal outreach app: a small web dashboard (FastAPI + Postgres) with a `salesfinder` CLI and a background worker, driven by the Claude Code CLI. It finds chemical buyers and suppliers (budgeted, teammate-approved searches), checks every company against a do-not-contact list and a read-only HubSpot sync so existing customers and suppliers never get cold email, profiles their websites for signals, finds the right people and addresses, drafts outreach with Claude for teammates to approve, sends approved email from warmed-up mailboxes on a separate sending domain, and reads replies, bounces and opt-outs. Sending ships paused. The worker records a heartbeat log, and admins can export a clean companies CSV.

- [docs/operations.md](docs/operations.md): running it on the office Mac (launchd, logs, LAN and HTTPS, upgrades, backups), the first real mailbox, the real dry run and troubleshooting.
- [docs/architecture.md](docs/architecture.md): processes, data flow, the worker's pass, modules, safety rails, lock order and where state lives.
- The [design spec](docs/superpowers/specs/2026-11-07-elchemy-salesfinder-design.md).

## Prerequisites

- Python 3.13 and [uv](https://docs.astral.sh/uv/)
- PostgreSQL 17 (with `psql` and `createdb` on PATH)
- Claude Code CLI (`claude`), installed and logged in

## Setup

```sh
make setup        # uv sync, git hooks, creates databases salesfinder + salesfinder_test, copies .env.example to .env
```

`make setup` runs `createdb`, so your macOS user needs a Postgres role that may create databases. Homebrew's Postgres makes the installing user a superuser. Otherwise ask for a role with `CREATEDB`, or have the two databases created for you.

Edit `.env` (git-ignored; it holds your secrets, never commit it). At minimum set `SALESFINDER_SECRET_KEY` (32+ characters):

```sh
python -c "import secrets;print(secrets.token_urlsafe(48))"
```

Then:

```sh
make migrate
uv run salesfinder users create-admin --email you@elchemy.com --name "You"   # prompts for a password
uv run salesfinder doctor     # environment self-checks; exits 1 if any fail
make dev                      # http://127.0.0.1:5555/login
```

That is the development setup. On the office Mac launchd keeps the web app and the worker running instead (`make plists`, `make start`): see [docs/operations.md](docs/operations.md).

Account recovery and deactivation from the command line:

```sh
uv run salesfinder users reset-password --email you@elchemy.com     # prompts twice; signs the user out everywhere
uv run salesfinder users set-active --email you@elchemy.com --inactive   # or --active
```

## Inviting teammates

Sign in as an admin, open `/admin/users`, and create an invite. Share the generated one-time invite link with the teammate; they set their own password from it. Admins can also deactivate/reactivate users there, and review activity at `/admin/audit`.

## Configuration

All settings are environment variables prefixed `SALESFINDER_` (read from `.env`); see `.env.example` for the full list and defaults:
`DATABASE_URL`, `SECRET_KEY`, `BIND_HOST`, `PORT`, `TLS_CERTFILE`, `TLS_KEYFILE`, `ALLOWED_HOSTS`, `PUBLIC_URL`, `CONFIG_DIR`, `LOG_LEVEL`, `LOG_JSON`, `SESSION_TTL_HOURS`, `CLAUDE_BIN`, `HUBSPOT_TOKEN`, `SERPER_API_KEY`, `EMAIL_VERIFIER_KEY`, `MAILBOX_PASSWORDS`.
App config lives in `config/salesfinder.yaml` (optionally overridden by `config/salesfinder.local.yaml`). The product catalog is `config/catalog.yaml` and the only claims SalesFinder may make about Elchemy are in `config/facts.yaml`; the team maintains both.

Both `web` and `worker` read `.env` and the YAML files only at startup: **after editing any of them, restart both `salesfinder web` and `salesfinder worker`** (`make restart` on the office Mac).

Admins set the monthly discovery budget at `/admin/settings` (it overrides `discovery.monthly_budget_usd`; HubSpot status and "Sync now" are there too) and manage the do-not-contact list at `/admin/do-not-contact`.

## Worker, HubSpot and discovery

`salesfinder worker` is a separate process; run it alongside `salesfinder web` and **keep it running**: it does the daily HubSpot sync (without it the HubSpot data goes stale and cold sending stays paused) and runs approved searches. Each pass it fails work stuck from a crashed run, re-excludes stored companies that the do-not-contact list or HubSpot now covers, syncs HubSpot when a sync is due (every `hubspot.sync_interval_hours`; after a failed sync it backs off from 5 minutes, doubling, up to that interval), runs one teammate-approved discovery job, profiles up to `profiling.batch_size` companies (see [Signals and profiling](#signals-and-profiling)), checks the contacts of up to `contacts.batch_size` companies (see [People and email](#people-and-email)), and, at most once every `writing.min_interval_minutes` (default 5), asks Claude to draft for up to `writing.batch_size` (default 10, 1–100) companies of active campaigns (see [Campaigns and the Outbox](#campaigns-and-the-outbox)). It also reads the mailboxes, classifies replies, sends approved email and queues due follow-ups (see [Sending and replies](#sending-and-replies)). It never starts discovery on its own, and it never sends anything a teammate didn't approve.

Only one worker may run at a time: a second `salesfinder worker` (against the same database, `--once` included) prints "Another SalesFinder worker is already running." and exits 1. Stop it with Ctrl-C or SIGTERM: it stops once the current pass is done (still marked alive while it finishes). From that moment the pass starts nothing new that could take long: no further reply is classified, no further company is drafted (a Claude call already running finishes), no approved discovery job is started, and a job already running stops before its next search ("Stopped after N of M searches: the worker was shutting down. Start a new search for the rest.", keeping the searches paid for). A HubSpot sync already in progress runs to the end.

**The runs log.** Every pass is recorded in the `runs` table: one row covers `usage.heartbeat_interval_minutes` (default 15) of passes, with their log lines (counts and our own messages only; at most `ops.max_run_lines` per row), any "<Step> step failed: <Type>" failures, and whether quiet hours were on. While it runs, the worker refreshes its row every `ops.beat_seconds` (default 30) with the step it is in, so a long step never looks like a dead worker. A clean stop closes the row; a worker that died without shutting down (killed, power loss) leaves it open, and the next start closes it as "crashed". Finished rows older than `ops.runs_keep_days` (default 30) are pruned. A failed write to the runs log never stops the worker (it logs `run_record_failed` and carries on). A pass that crashes the worker leaves its row open, to be closed as "crashed" by the next start.

**Never unlocked.** With every beat the worker also checks that it still holds the single-worker lock. If Postgres restarted under it (the lock goes with the connection) it finishes the current pass, prints "Lost the worker lock (database restarted?) — stopping so launchd restarts me." and exits 1; launchd starts it again under a fresh lock. If it finds another worker's row open (a second worker started meanwhile) it stops, prints "Another SalesFinder worker is already running." and exits 1, leaving the other one running.

**Is it running?** Every page shows a red banner while the worker isn't working: it hasn't run yet, it stopped, it stopped responding (no beat for `ops.worker_stale_seconds`, default 180; the banner names the step it was in), or it runs different code from the web app (restart both together after an upgrade). Today has a Worker card (its last pass, failures in the last 24 h and the latest ones) and a red alert for each mailbox whose password is set but whose connection hasn't passed a test: its replies and opt-outs aren't read until an admin presses Test connection. The Runs page (`/runs`, any teammate) shows the log with each window's Claude calls and cost, Claude calls today and discovery spend against their budgets. `salesfinder doctor` checks `worker` too (ok before its first run), `salesfinder runs` prints the same health line and the latest runs, and `/healthz` reports `"worker": "<state>"`.

**Quiet hours.** Between `usage.quiet_hours.start` and `end` (in `usage.quiet_hours.timezone`; default 22:00–07:00 America/New_York, `end` exclusive, `start == end` means none) the worker's Claude steps wait: replies aren't classified or answered and nothing new is drafted, which leaves the Claude subscription free for the operator. Everything else keeps running: mail is read (so opt-outs and bounces are honoured at once), approved email is sent in each recipient's office hours, and follow-ups, HubSpot, discovery, profiling and contact checks go on. The log says "Quiet hours (…): Claude steps wait until 07:00." once per quiet period. `salesfinder draft`, run by hand, ignores quiet hours.

**`--once` and `--no-send`.** `salesfinder worker --once` runs one recorded pass (its runs row ends "once"), prints its lines and exits 1 if any step failed; Ctrl-C or SIGTERM lets that pass finish. `--no-send` skips the Sending step entirely (no SMTP connection at all, whatever the paused state; its mail sender refuses to send as well: it never connects, and answers any send that reaches it with a retry, so the email isn't left "sending"; that retry counts as a send attempt), saying "Sending skipped: this worker runs with --no-send." in each runs row (Today, Runs, `salesfinder runs` and `doctor` then show "Running with --no-send (nothing will be sent)"); mail reading and everything else still run. The Sending step also *settles* sends whose outcome was unclear (it looks for them in the Sent folder), so under `--no-send` any email still marked "sending" stays unsettled until a worker runs without it. Use both for a dry run.

```sh
uv run salesfinder worker            # keep running next to `make dev`
uv run salesfinder worker --once --no-send  # one recorded pass that never sends; exit 1 if a step failed
uv run salesfinder runs              # the worker's health and its 10 latest runs (--limit N)
uv run salesfinder hubspot sync      # one read-only sync now; exit 1 on failure
uv run salesfinder discover jobs     # the 20 latest discovery jobs
uv run salesfinder profile --limit 5 # profile up to 5 due companies now and print the outcomes
uv run salesfinder contacts --company acme.com  # people and addresses found for a company
uv run salesfinder contacts --verify --limit 5  # check up to 5 due companies' contacts now
```

**HubSpot** (optional; without it cold sending stays paused). In HubSpot go to Settings > Integrations > Private Apps, create a private app and grant the read-only scopes `crm.objects.companies.read` and `crm.objects.contacts.read` (optionally `crm.objects.owners.read` to show account owners). Put the token in `SALESFINDER_HUBSPOT_TOKEN`. SalesFinder only ever issues GET requests.

**Serper** (needed for discovery). Sign up at [serper.dev](https://serper.dev), create an API key and set `SALESFINDER_SERPER_API_KEY`. Serper bills per search; each job shows an estimated cost that a teammate must approve, and approval is refused past the monthly budget.

`salesfinder doctor` reports both: `hubspot` passes only when the last sync is fresh, `serper` when a key is set (no network calls).

## Signals and profiling

Profiling reads each discovered company's **own website** (its homepage plus up to `profiling.max_pages_per_company - 1` pages it links to: about, products, contact, careers, quality, export) and records what it finds as *signals*. It only looks for signals the team has **confirmed**; nothing else is collected. The worker profiles up to `profiling.batch_size` companies per pass (`profiling.concurrency` at a time), new companies first, and re-profiles a company after `profiling.refresh_after_days`. A site that couldn't be read (`failed` or `no_site`) is tried again sooner: 1 day after the first failure in a row, 7 days after the second, then every `refresh_after_days`. If no site in a pass could be read and at least 3 of them couldn't even be reached (no connection or a timeout) without already failing before, the worker assumes its own network is down: it records nothing, logs "Network looks down — will retry.", and tries those companies again 15 minutes later. Excluded companies are never profiled.

It reads politely and safely: it identifies itself with `profiling.user_agent`, obeys `robots.txt`, reads at most `max_pages_per_company` pages of at most `max_page_bytes` each, gives each page `page_timeout_seconds`, only follows redirects within the company's own domain, and never connects to private or local network addresses.

| Signal | Side | Means |
| --- | --- | --- |
| `INDUSTRY_MATCH` | buyer | their site describes products in one of our eight industries |
| `USES_CATALOG_CHEMICAL` | buyer | their site names chemicals from our catalog |
| `COMPANY_TYPE` | buyer | manufacturer / contract or private-label maker / distributor; retailers and service businesses are disqualified |
| `US_LOCATION` | buyer | a US address on their site (and which state) |
| `NEAR_WAREHOUSE` | buyer | that address is within `scoring.near_warehouse_miles` of an Elchemy warehouse |
| `HIRING_PROCUREMENT` | buyer | a purchasing, procurement or supply-chain job on their careers page |
| `QUALITY_CERTS` | buyer | ISO 9001, GMP, Kosher and similar certifications |
| `MAKES_CATALOG_CHEMICAL` | supplier | they say they make chemicals from our catalog |
| `MANUFACTURER_NOT_TRADER` | supplier | a real manufacturer rather than a trader |
| `EXPORTS` / `EXPORTS_TO_US` | supplier | they export / already sell to the US |
| `SUPPLIER_CERTS` | supplier | ISO, GMP, Kosher, Halal and similar certifications |
| `STATED_CAPACITY` | supplier | a production capacity figure (e.g. tons per month) |
| `REGION` | supplier | where they are based |

Detectors look at the first 200,000 characters of each page's text. Each company gets a score from 0 to 100 from the latest finding of each confirmed signal (weights in `scoring` in `config/salesfinder.yaml`), with the reasons listed. A signal with no evidence is stored as an explicit "not found"; a site that can't be read is marked `blocked`, `no_site`, `failed` or `partial` with the reason. A failed profile keeps the company's earlier findings and score, and a `partial` one (some pages couldn't be read) never replaces an earlier finding with "not found".

Weights left out of `scoring.buyer` / `scoring.supplier` keep their defaults. Scores are stored, so after changing scoring weights run `salesfinder signals --rescore` and restart web/worker.

Confirm signals (every state change is audited and rescores all companies). A newly confirmed signal has no findings yet: confirming one queues that side's already-profiled companies again, so the new signal fills in as the worker re-reads their sites over the next passes (`batch_size` per pass).

```sh
uv run salesfinder signals                   # code, side, state, companies observed, label
uv run salesfinder signals --confirm free    # confirm every free signal (all of them today)
uv run salesfinder signals --confirm QUALITY_CERTS --reject REGION --skip HIRING_PROCUREMENT
uv run salesfinder signals --rescore         # recompute every score (after editing weights)
```

## People and email

**Where names come from.** Only the company's own website: while profiling, the about, contact, team and leadership pages are read for named people with a job title next to their name ("Priya Nair | Head of Procurement"), for email addresses published on the company's domain (and free-mail addresses such as Gmail on those pages), and for a contact form. Nothing comes from LinkedIn or other directories. People who work somewhere else are skipped: someone whose title is followed by another organisation ("Rakesh Gupta – Purchase Manager, Hindustan Unilever"; the company's own name there is fine), and names in testimonial or quote blocks or in sections headed as clients, customers, partners, associates, bankers, "trusted by" or "what … say". At most `contacts.max_targets_per_company` people (default 2) are marked as **targets** (★), chosen by job title: buyers' purchasing, supply-chain and R&D people first, then owners/directors; suppliers' export managers first, then owners/directors, business development and marketing. A profile that couldn't read every page never drops people or addresses found earlier; a complete one drops people no longer on the site (unless one of their addresses has an override). Published addresses are kept (at most 50 per company), but one a complete profile no longer finds on the site is marked *no longer on their site*: it is unlinked from its person, no longer used to learn the pattern, and not sendable unless a teammate allows it. The company page says when no people were found because no team, about or contact page was found, because those pages named nobody with a title, or because reading them failed.

**Checking addresses.** The worker then checks each profiled company's contacts (and again every `contacts.recheck_after_days`): it looks up the domain's mail servers (DNS MX records only, `dns_timeout_seconds` per lookup, `dns_concurrency` at once, cached `mx_cache_days`), learns the company's address pattern (e.g. `first.last@acme.com`) from addresses it publishes for people named on its site, and gives each target one address. **SalesFinder never probes mail servers (no SMTP connection, ever)**: home and office connections usually block it and it hurts sender reputation. If DNS doesn't answer (a timeout or server failure), nothing changes: a domain looked up before keeps its last known answer (so a DNS blip never makes addresses unsendable), and a domain never looked up successfully is tried again after an hour. A check that fails for any other reason is also retried after an hour. The worker line then says how many couldn't be checked, e.g. "Checked contacts for 3 companies (4 sendable addresses). (1 couldn't be checked)".

| Status | Means | Sendable |
| --- | --- | --- |
| `published` | the exact address is on their website | yes |
| `pattern_confirmed` | built from the pattern their other published addresses follow, and the domain has mail servers | yes |
| `pattern_guess` | a common pattern (`first.last`), no evidence on their domain | held |
| `verified` | the paid verifier says it delivers | yes |
| `risky` | the paid verifier can't tell (catch-all domain) | held |
| `invalid` | the domain has no mail server, or the verifier says it won't deliver | never |

**The sendable rule** (one rule for the counts, the pages and, later, sending): an address is sendable when its status is sendable (above), the company's domain has mail servers (free-mail addresses always do), it is still on their site (for a published one), it isn't a no-reply-style mailbox (`noreply@`, `postmaster@`, `abuse@`, `mailer-daemon@`, `bounce@`…: "Not a person's mailbox"), and it isn't on the do-not-contact list or a HubSpot contact (`exclusion_for`). `is_sendable` answers as of the last refresh of the address's status and block: adding or removing a do-not-contact entry and every HubSpot sync refresh the blocks and the sendable counts at once, and sending (Phase 6) re-checks the exclusion lists live before every email. A teammate can **override** one address: "Allow sending" makes a held `pattern_guess` or `risky` address, or one no longer on their site, sendable (the button only shows on such an address), "Don't send" stops any address, and clearing it goes back to the rule. `invalid` and no-reply-style addresses are never sendable, override or not. When the same learned pattern gives the same address to two people on the site (Amit Shah and Anil Shah under `{f}{last}`), it is only a guess for either of them. A company's *sendable contacts* is the number of its targets with a sendable address.

**Optional paid verifier.** Set `contacts.verifier: millionverifier` in `config/salesfinder.yaml` and put the API key in `SALESFINDER_EMAIL_VERIFIER_KEY`. It is only asked about targets' pattern addresses (never about published ones), at most `contacts.verifier_max_per_day` addresses per UTC day (default 100), never about an address a teammate marked "Don't send", and never again about an address once it gave a definite answer (an "unknown" is retried at the next check). The key never appears in logs, errors or the database. Configured without a key, the worker notes "Email verifier configured but SALESFINDER_EMAIL_VERIFIER_KEY is not set." and checks without it. A call that fails (a bad key, no credits left, an HTTP error) records nothing and doesn't count towards the daily cap; after 3 failures in a row the verifier isn't called again until the next batch, and the worker line (and `contacts --verify`) ends with "Email verifier failed — check the key and credits."

**On the dashboard.** A company's page has a *People & email* section (hidden for excluded companies): the people found (★ marks targets) with title, role and a link to the page they came from, then every address with a plain-language status ("Published on their site", "Matches their email pattern", "Our best guess — held", "Verified", "Risky (catch-all) — held", "Won't deliver"), whether it is sendable and why not ("No mail server", "Existing relationship: …", "Held: guess"), the domain's mail-server state, the learned pattern ("Pattern: first.last@acme.com (from 2 addresses)", or "No email pattern learned yet.") and the contact form link. Any signed-in teammate can press **Allow sending**, **Don't send** or **Clear** on an address (*Allow sending* shows only on a held address an override can lift); each change is audited (`email.override_set` / `email.override_cleared`, with the address and the old and new values). The email pattern is kept per domain, so a buyer and a supplier row on the same domain show the same one. The Companies list has a **Contacts** column, `sendable / found` (blank until the company's contacts were first checked).

```sh
uv run salesfinder contacts --company acme.com   # ★ targets, titles, every address with status and sendable yes/no + why
uv run salesfinder contacts --verify --limit 5   # check up to 5 due companies now (like a worker pass)
```

## Cohorts

A **cohort** is a saved filter over companies' latest signals, for example "Uses Sodium Laureth Sulfate (SLES 70%) or Cocamidopropyl Betaine (CAPB) · not a distributor · score ≥ 40". Open **Cohorts** in the dashboard and pick **New buyer cohort** or **New supplier cohort**.

- The form lists the side's **confirmed** signals. For each one choose *Must match*, *Must not match* or *Ignore*, and optionally tick the values that count (chemicals, industries, certifications, company types); with nothing ticked any finding counts. *Close to an Elchemy warehouse* takes a distance in miles and *States production capacity* a minimum in MT per year. Unconfirmed signals are greyed out with a link to the Signals page.
- Add a minimum score (0–100) and whether disqualified companies may match. Excluded companies never match. By default only companies whose website has been read match (*Profiled* or *Partly profiled*, or read before and now waiting for a re-profile or after a failed re-read, which keeps the earlier findings): a company never read — still waiting, failed, blocked or without a website — has no findings, so a "must not" condition can't rule it out. Tick *Include companies we couldn't read yet* to include them anyway. Tick *Only companies with a sendable contact* to keep only companies with at least one target who has a sendable address (it reads "· has a sendable contact" in the summary).
- A condition on a signal that isn't confirmed any more is kept, but nothing collects that signal, so the cohort and the Cohorts list warn about it, e.g. "Not collected now: “not a distributor” currently excludes nobody."
- The count under the name updates as you change the form ("23 companies match"); it is computed live from the latest findings, so it changes as companies are profiled and signals confirmed.
- A cohort's page shows its definition in plain English, the live count, and the matching companies (highest score first, 50 per page, with each one's profile status) linking to each company.

Anyone signed in can create, edit and delete cohorts; names are unique (the database constraint is case-sensitive; saving from the form also refuses a name that differs from an existing one only in case). Every change is audited (`cohort.created`, `cohort.updated`, `cohort.deleted`, with the name, side and definition).

## Campaigns and the Outbox

A **campaign** is a side (buyer or supplier), a cohort of that side, a playbook (the side's) and a daily draft cap (default 40, 1–200). A teammate creates it as a *draft* and **activates** it; only active campaigns draft. Activation (and resuming a paused campaign) is refused, with the reason, when:

- the cohort uses signals that aren't confirmed any more: "This cohort uses signals that aren't being collected: Works in our industries.";
- the side's persona (name and title, used to sign the emails) isn't confirmed in `config/facts.yaml`: "Persona for buyer isn't confirmed in facts.yaml.";
- Elchemy's postal address (it goes in every footer) isn't confirmed: "Confirm Elchemy's postal address in facts.yaml first."

The same checks run before every drafting pass, so a campaign whose facts were un-confirmed after activation drafts nothing (its last error says why) instead of spending Claude calls on drafts the gate would hold. A campaign can be paused, resumed and archived (archived for good). Campaign names are unique regardless of case (enforced by the database too).

- **Paused**: no new drafts, but its waiting emails can still be approved (the Outbox card shows a *Campaign paused* badge). Nothing cold (first emails, follow-ups) is sent for a paused campaign; approved answers to replies still go.
- **Archived**: in the same step, its unfinished sequences are stopped ("Campaign archived") and its unsent emails (waiting, held, approved or pending follow-ups) are cancelled, so they leave the Outbox and their addresses are free for other campaigns. The `campaign.archived` audit row records how many of each. The Outbox refuses any action on an archived campaign's email ("This campaign is archived.").

**Who gets an email.** One sequence per company per campaign, to one person: the company's targets in rank order, and for each the best address that is sendable *right now* — published on their site, then verified, then built from their confirmed pattern, then one a teammate allowed (a held guess, a catch-all, or one no longer on their site). Sendability is recomputed live (the sendable rule above, the do-not-contact list and HubSpot, and a mail-server check no older than `contacts.mx_cache_days`). A mail-server check that is older (or missing) is looked up again before choosing — at most 2 × the pass's limit lookups per pass, never inside a database transaction; if DNS can't answer, that address is passed over this time. Disqualified companies (see scoring) never get an email, even from a cohort that includes them. An address already in a sequence that is awaiting approval or active, in any campaign of either side, is skipped. Companies come from the cohort's live matches with at least one sendable contact (highest score first), skipping companies already in the campaign; a company with no usable address is passed over until it has one. If another campaign's draft to the same address is stored while Claude is writing, this draft is dropped rather than stored as held.

**What Claude sees.** Each draft is one isolated `claude -p` call through the operator's own Claude Code login (no API key): no tools, none of your CLAUDE.md, settings, hooks, plugins or MCP servers, the prompt on stdin, in a fresh empty folder. It gets the shared writing rules, the side's playbook, Elchemy's **confirmed** facts only (company name, website, the confirmed claims from `facts.yaml`, the persona's name and title), warehouse locations (as locations only), and about the recipient only: the company's name, the person's name and title, the catalog products they use or make, their industries, their US location or region, and the nearest Elchemy warehouse — never page text or evidence snippets. Scraped values (names, titles, locations) are passed as a clearly marked data block that Claude is told never to follow. Teammates' recent rejection reasons for the campaign are included as feedback. Claude writes only the subject and body of a first email and one follow-up; code adds the signature and the compliance footer (company name, postal address, opt-out line).

**What Claude may say.** Only what the confirmed facts support: no prices or currency amounts, no lead times or stock claims, no certifications that aren't in the confirmed facts (not even the recipient's own), no link but the configured `writing.booking_url`, no email addresses, nothing about how the company was found. Leave a fact unconfirmed in `facts.yaml` and Claude never hears of it.

**The gate.** Every first email is checked before a teammate sees it as ready: subject present and within `writing.subject_max_chars`; no unfilled placeholders; within `writing.first_email_max_words` (follow-ups `followup_max_words`); no banned phrase (`writing.banned_phrases`), price, or unconfirmed certification; at most one link and only to our website or the booking link; no stranger's email address; signature and footer exactly as code wrote them; the postal address confirmed; no echo of our own instructions; and the recipient still OK (company not excluded or disqualified, address not excluded, still one of our chosen contacts — the sequence's own person — and sendable, mail-server check fresh, not already being contacted by another campaign). A draft that passes waits in the Outbox as *ready* (`awaiting_approval`); one that doesn't is stored as *held by the gate* (`gate_failed`) with the reasons, for a teammate to edit or reject. The follow-up is stored with the first email and is gated when it falls due. Approval re-runs the gate.

**The budget.** At most `usage.max_daily_claude_calls` (default 150) Claude calls per UTC day, counting every attempt, failed or not (a reply that isn't the JSON asked for is retried once, which counts too). Each campaign stores at most its daily cap of sequences per UTC day, whatever the gate said. A worker pass (or one `salesfinder draft`) asks Claude about at most `writing.batch_size` companies (`--limit`), counting every company asked: drafted, held by the gate, or failed. If the day's budget is already spent when a pass starts, drafting does nothing that pass (no log line). Drafting stops for the pass, the worker logs "Writing stopped: <reason>", and **every active campaign's** last error shows the reason, when:

- the budget runs out during the pass ("Daily Claude budget reached (150 calls).");
- the Claude CLI can't be used: not installed, or missing the isolation flags (run `claude update`);
- Claude says it isn't signed in or is over quota ("Claude returned an error: not signed in or quota reached."): only one call is spent finding that out.

Three Claude failures in a row (timeouts, error exits, replies that aren't usable even after the retry) also stop the pass, with "Claude kept failing — stopped for this pass." on the campaign being drafted. Each failure for one company — a Claude failure short of the third in a row, or a reply or context that can't be used — is reported (e.g. by `salesfinder draft`) and the pass goes on; that company is then left alone in that campaign for 6 hours, and for the rest of the UTC day once it has failed 3 times that day. A stored draft clears its failure record. A campaign's last error stays until it next stores a draft (or is activated or resumed).

Each worker pass logs e.g. "Drafted 3 emails for review (1 held by the gate)." Approved emails are sent by the worker (see [Sending and replies](#sending-and-replies)).

**Campaigns page.** **Campaigns** in the dashboard lists every campaign with its side, cohort, status, drafts today / daily cap, first emails ready to approve, and its last writing error. **New buyer campaign** / **New supplier campaign** asks for a name, a cohort of that side and the daily draft cap (1–200) and creates a *draft*. A campaign's page shows its numbers, the cohort in plain English, what stops it from activating (or from drafting, when active), and the buttons its status allows: **Activate**, **Pause**, **Resume**, **Archive**. A refused button shows the reason (e.g. "Persona for buyer isn't confirmed in facts.yaml."). Every change is audited (`campaign.created`, `campaign.activated`, `campaign.paused`, `campaign.resumed`, `campaign.archived`).

**Outbox page.** **Outbox** (the nav shows `Outbox (N)` while N emails are waiting for review) has three tabs, each filterable by campaign and kind, 25 per page: **To review** (first emails, follow-ups that fell due and Claude's drafted answers to replies, each with a *First email* / *Follow-up* / *Reply* badge; a follow-up shows when the first email went out, a reply shows their message quoted), **Approved — waiting to send** and **Problems** (see [Sending and replies](#sending-and-replies)). In To review, oldest first, each card shows the campaign, the company (linked), the person and their title, the address, *Ready* or *Held by the gate* with the gate's reasons, the subject and the full email as it would be sent. On each card:

- **Approve** (ready emails) re-runs the whole gate on live data first: if anything changed (say the address went on the do-not-contact list) the email is held with the reason instead of approved.
- **Re-check** (held emails) re-runs the gate on the email as it is, for a hold that has since cleared (a lifted exclusion, a mail server DNS can now confirm); a clean one goes back to *Ready*, never straight to approved.
- Before Approve, Re-check, saving an edit or Approve selected, a recipient domain whose mail-server check is older than `contacts.mx_cache_days` (or missing) is looked up again, so the gate sees fresh data. If DNS can't answer, the email is held with "Couldn't confirm the recipient's mail server just now — try Re-check later."
- **Edit** opens the subject and the written part. The signature and the compliance footer are shown read-only as "Added automatically": code adds them, they can't be edited. Saving re-runs the gate (*Ready* or held with the reasons); it never approves. If the email changed after you opened it (a teammate saved or approved it), saving is refused with "Someone else changed this email — reload and try again." and nothing changes.
- Without a sender persona for the campaign's side in `facts.yaml`, approve, re-check and saving an edit are refused with "No sender persona is set for this side in facts.yaml." (nothing changes).
- **Reject** needs a reason (1–500 characters). The sequence stops and the reason is shown to Claude when it drafts this campaign's next emails.
- Tick **Select** on ready emails and press **Approve selected** to approve up to 50 at once; each is gated on its own, and the page says e.g. "Approved 4; 1 held — see below."

Any signed-in teammate (member or admin) can do all of this. Every action is audited with the message id, campaign and address only — never the email text or the reject reason (just its length): `message.approved`, `message.held` (an approval the gate refused), `message.rechecked`, `message.edited`, `message.rejected`, and one `message.bulk_approved` summary per bulk approval. The **Today** page shows whether sending is on (or why it is paused, with a *Pause all sending* button), emails ready to approve and held, follow-ups due for review, approved emails waiting to send, replies in the last 24 hours, the conversations that need you (yours and everyone's, with links), sends that couldn't be confirmed or failed, each mailbox's sends today against today's cap and why it may be holding, the last mail-reading error, sequences drafted today, Claude calls today against `usage.max_daily_claude_calls`, and each campaign's last writing error.

```sh
uv run salesfinder campaigns                                   # name, side, status, cohort, drafted today/cap, awaiting approval
uv run salesfinder draft --campaign "Texas soap makers" --limit 5  # one drafting pass for that campaign now
```

## Sending and replies

**Sending is paused by default.** On a new install every page says "Sending is paused: Sending hasn't been turned on yet." Nothing is sent until a mailbox exists, is enabled, has its password set, and an admin has resumed sending.

**Mailboxes (admin).** Cold email goes out from Google Workspace mailboxes on a separate sending domain, never elchemy.com (that is refused). Admins register them at **Mailboxes** (`/admin/mailboxes`): address, side (buyer or supplier; each campaign picks one mailbox of its side on its page), username, a *password name*, the SMTP and IMAP servers (Gmail's by default), the Sent folder (blank: found automatically), the DKIM selector (default `google`), the daily maximum (default 30, at most `sending.max_daily_cap`, 50) and the warm-up start date. Admins are trusted: they choose the servers a mailbox's app password is sent to, so only enter servers you control or trust. A new mailbox starts held ("Not tested yet — test the connection, then enable it."), and changing a mailbox's servers, ports, username or password name disables it and holds it again ("Connection settings changed — test it, then enable it again."): until **Test connection** passes, nothing logs in with its password, not even to read replies, and it can't be enabled.

**App passwords live only in `.env`.** For each mailbox, turn on 2-Step Verification for that Google Workspace account, then create an app password (Google Account > Security > 2-Step Verification > App passwords; your Workspace admin may have to allow app passwords and IMAP first). Put it in `SALESFINDER_MAILBOX_PASSWORDS`, a JSON object keyed by the mailbox's password name (lowercase letters, digits and `_`, up to 32):

```sh
SALESFINDER_MAILBOX_PASSWORDS={"sales_box": "abcd efgh ijkl mnop", "sourcing_box": "qrst uvwx yzab cdef"}
```

**Set it in one place**, normally `.env`: an exported `SALESFINDER_MAILBOX_PASSWORDS` in the shell replaces the `.env` value as a whole (the two are never merged), so a stale export would hide a password you removed or changed in `.env`. Restart the web app and the worker after changing it. Mail servers' TLS certificates are always verified (SMTP and IMAP). The database stores the password name only; passwords never appear in the database, logs, pages or audit rows. **Test connection** logs in (SMTP login, NOOP, QUIT; IMAP and the Sent folder) and nothing else: SalesFinder never probes anyone else's mail server.

**DNS records.** Cold sending is blocked until the sending domain passes **Check DNS** (fresh within `sending.dns_max_age_hours`, default 24):

- SPF: one TXT record on the domain that includes `include:_spf.google.com` (when sending through Google) and ends `~all` or `-all` (never `+all`);
- DKIM: the TXT record `<selector>._domainkey.<domain>` from Google Admin (Apps > Google Workspace > Gmail > Authenticate email);
- DMARC: exactly one TXT record at `_dmarc.<domain>` with `p=none`, `quarantine` or `reject`;
- MX: the mailbox's domain must receive mail (replies and bounces come back to it).

**Warm-up.** Set the warm-up start date when an external warm-up service is switched on. No cold email for the first 14 days; then the cap starts at 5 a day and rises by 3 a day up to the mailbox's maximum. Caps are per mailbox and count first emails and follow-ups per UTC day. Cold email also waits for each recipient's office hours (09:00–17:00 local, weekdays; US time zones for buyers, IST/South-East Asia for suppliers), spaces sends 3–9 minutes apart per mailbox, and is held while the HubSpot sync is stale (older than 48 h) or the campaign is paused. Answers to people who wrote to us are held by none of these: only a global pause or a disabled mailbox stops them. Every email is gated again just before it goes, exactly as stored (headers are the only additions, with `In-Reply-To`/`References` so follow-ups and answers thread, and a `List-Unsubscribe` mailto).

**Pause and resume.** Any signed-in teammate can pause all sending at once with **Pause all sending** on Today (or Settings): an emergency stop. Only an admin resumes, from **Settings**, which also shows the bounce rate ("b of last 50 (p%)") and each mailbox's sends today against today's cap and why it is holding. A resume is refused with "Sending state changed meanwhile — check it and try again." if the state changed after the page was opened (for example the kill switch fired), so look again before resuming. **Kill switch:** when more than 5% of the last 50 sends bounced, sending pauses itself ("Paused automatically: 3 of the last 50 emails bounced (6%)."). Sends before the latest resume don't count. Pausing and resuming are audited (`sending.paused`, `sending.resumed`, `sending.auto_paused`) only when they change something: resuming while sending is already on does nothing (the bounce window keeps counting from the last real resume).

```sh
uv run salesfinder sending status        # state, bounce rate, each mailbox's cap and hold
uv run salesfinder sending pause --reason "Checking bounces"
uv run salesfinder sending resume --yes  # refused if the state changed since it was read
```

**Follow-ups.** One follow-up per sequence, due 4 business days after the first email went out. When it falls due its subject becomes `Re: <first subject>`, it is gated and it waits in **To review** like a first email: nothing is sent without approval.

**After approval.** **Approved — waiting to send** lists approved emails with why their mailbox may be holding them (warm-up, cap, office hours, DNS, disabled, *Campaign paused*); **Withdraw** puts one back in To review. **Problems** lists sends that need a teammate: *We couldn't confirm this went out — check the mailbox's Sent folder* (a send whose outcome is unknown, never retried automatically): **It was sent** if you find it there, else **Send again** (with the same Message-ID, so a duplicate is traceable); and failed sends with the server's last error and **Send again**. Each is audited (`message.withdrawn`, `message.marked_sent`, `message.resend`).

**Replies.** Every `mail.poll_interval_minutes` (default 5) the worker reads each mailbox's INBOX and Sent folder (every mailbox with a password, enabled or not, so opt-outs are never missed; except one whose connection settings (new or changed) haven't passed **Test connection** yet):

- a definite bounce stops the sequence and suppresses the address; a bounce we can't read flags the conversation (nothing suppressed);
- an unsubscribe (the `List-Unsubscribe` link or a reply saying so; our own footer's opt-out line quoted back doesn't count) suppresses the address immediately and stops everything for that person. An unsubscribe outranks every other suppression: an address already suppressed as bounced or manual becomes *unsubscribed* (audited `suppression.upgraded`) and can no longer be removed;
- auto-replies are set aside;
- anything else is a reply: the follow-up is cancelled and Claude classifies it (interested, objection, not now, wrong person, unsubscribe, auto-reply, other) and drafts an answer into **To review**;
- a mail a teammate sent from the mailbox (Gmail) to that person **takes the conversation over**: automation stops for them, no more drafts, pending follow-ups cancelled.

**Conversations.** **Conversations** (the nav shows `Conversations (N)` while N need you) lists everyone who wrote back, with views *Needs you*, *Mine*, *Open*, *Handled* (handled or taken over) and *All*. A conversation's page shows the thread in time order (our emails, their replies, bounces, unsubscribes and takeovers), the answers waiting in the Outbox, and "Waiting for Claude: …" while a reply hasn't been classified yet (with the reason when it is known: today's Claude budget is used up, the CLI isn't installed, or Claude is signed out). Any teammate can **Assign to me**, **Mark handled** (Claude's unsent drafts are cancelled; an answer a teammate wrote still goes), **Take over** (final: the sequence stops, Claude's drafts and the follow-up are cancelled; an answer a teammate wrote still goes) or write their own answer, which is gated, approved by them and sent from the campaign's mailbox; once it is sent the conversation is taken over. Every action is audited (`conversation.assigned`, `conversation.handled`, `conversation.taken_over`, `reply.written`).

**What "needs you" means.** A teammate should read the conversation: they sounded interested ("Interested — reply soon"), Claude wasn't sure what they want, couldn't classify the reply or couldn't draft an answer, the reply came from a different address (answer from the mailbox), a drafted answer was rejected, a bounce or a mail couldn't be read. When Claude's reading of a reply flags it (interested, unsure, couldn't classify or draft), the conversation is assigned to whoever approved the first email, if they are still active; flags raised while reading the mailbox (a different address, a bounce or a mail we couldn't read) leave it unassigned, for anyone to pick up with **Assign to me**. A conversation that was taken over stays taken over: marking it handled does nothing, and later replies never bring Claude back (they are still classified, so an unsubscribe only Claude recognises is honoured, but nothing is drafted, flagged or assigned). Every flag is audited (`conversation.needs_you`).

**Mail reading problems.** The Mailboxes page lists each folder's last reading error ("Couldn't read the mailbox: …", or "Skipped a mail we couldn't read", which stays until an admin presses **Dismiss**, audited `mailbox.error_dismissed`) and, under *Mail we couldn't place*, bounces and mails we couldn't read that matched none of our emails, so someone checks them in the mailbox.

**Upgrading.** Restart the web app and the worker together after upgrading: the per-recipient lock they share changed form, and an old process holding the old form wouldn't see the new one's locks. The steps are in [docs/operations.md](docs/operations.md#3-upgrading). The first real mailbox and the real dry run with sending paused are there too.

## LAN mode

By default the server binds to `127.0.0.1` only. To serve teammates on your network, set `SALESFINDER_BIND_HOST` to a non-loopback address (e.g. `0.0.0.0`). Startup refuses unless TLS is configured, so also set both `SALESFINDER_TLS_CERTFILE` and `SALESFINDER_TLS_KEYFILE`. Generate a local certificate with [mkcert](https://github.com/FiloSottile/mkcert):

```sh
mkcert -install
mkcert salesfinder-mac.local 192.168.1.20
```

mkcert names the files after the first name and the number of extra names. Point the settings at them:

| mkcert wrote | Setting |
| --- | --- |
| `salesfinder-mac.local+1.pem` | `SALESFINDER_TLS_CERTFILE=/path/to/salesfinder-mac.local+1.pem` |
| `salesfinder-mac.local+1-key.pem` | `SALESFINDER_TLS_KEYFILE=/path/to/salesfinder-mac.local+1-key.pem` |

Add the hostnames clients use to `SALESFINDER_ALLOWED_HOSTS`, which must be a JSON list, and set `SALESFINDER_PUBLIC_URL` to the address teammates open (https in LAN mode; invite links use it):

```sh
SALESFINDER_ALLOWED_HOSTS=["127.0.0.1","localhost","192.168.1.20","salesfinder-mac.local"]
SALESFINDER_PUBLIC_URL=https://salesfinder-mac.local:5555
```

**Each teammate's computer must trust the mkcert root CA**, or their browser warns about the certificate. Copy only `rootCA.pem` from the folder `mkcert -CAROOT` prints (never `rootCA-key.pem`) and add it as a trusted root on their computer. Doctor's `network` check and the Network section of Settings name any LAN misconfiguration. The full steps, including certificate expiry, are in [docs/operations.md](docs/operations.md#2-lan-and-https).

## Development

| Command | Does |
| --- | --- |
| `make setup` | install deps and git hooks, create databases, create `.env` |
| `make migrate` | apply database migrations |
| `make dev` | run the server with auto-reload |
| `make test` | run pytest (uses `SALESFINDER_TEST_DATABASE_URL`, default `postgresql+asyncpg://localhost/salesfinder_test`; the test schema is dropped and recreated) |
| `make lint` / `make fmt` | ruff check + format check / auto-fix |
| `make typecheck` | `mypy src` |
| `make check` | lint, typecheck and tests (what CI runs) |
| `make e2e` | the end-to-end run of the whole loop against fakes |
| `make plists` / `make start` / `make stop` / `make restart` / `make logs` | office Mac only: render and load the launchd agents, stop them, restart both together, follow the logs (see [docs/operations.md](docs/operations.md)) |

## Project layout

```
src/salesfinder/
  cli.py        salesfinder CLI (web, db upgrade, users create-admin/reset-password/set-active, doctor, worker, runs, hubspot sync, discover jobs, signals, profile, contacts, campaigns, draft, sending pause/resume/status, ops plists)
  worker.py     background worker loop (reap stuck work, exclusion sweep, HubSpot sync, mail, replies, sending, follow-ups, approved jobs, profiling, contacts, writing; recorded in the runs log, quiet hours)
  settings.py   environment settings
  config.py     YAML app config loader
  catalog.py    catalog.yaml / facts.yaml models and loaders
  industries.py industry keys and pipeline sides
  domains.py    domain normalization (offline public-suffix list)
  hosts.py      host-name matching shared by the web app's Origin check and the LAN checks
  export.py     the companies CSV export (exclusions and suppressions filtered, formula-safe cells)
  doctor.py     environment self-checks
  audit.py      audit log recording
  auth/         passwords, sessions, tokens, CSRF, rate limiting, users
  db/           engine, models, Alembic migrations
  brain/        Claude CLI integration
  discovery/    search queries, Serper client, junk filter, budget, discovery jobs
  exclusions/   do-not-contact list, exclusion check and sweep, HubSpot client and sync
  profiling/    safe fetcher, page picking, profiling service, scoring
  contacts/     names, people/email extraction, titles, email patterns, MX checks, verifier, contacts service
  signals/      signal catalog and settings, text extraction, detectors
  cohorts/      cohort condition language, evaluation and CRUD
  campaigns/    campaign lifecycle, recipient selection, drafting passes
  writer/       what Claude may see (context), prompt, reply schema, assembly with signature and footer
  gate/         the pre-send gate (content and live recipient checks)
  outbox/       approve, re-check, edit, reject and bulk approve drafts (re-gated); withdraw, mark sent, send again; the Outbox tabs
  outreach/     the one lock order (messages, sequence, recipient) and stopping a sequence
  suppressions/ the suppression list (bounced, unsubscribed, manual)
  mail/         MIME, SMTP and IMAP adapters, mail parsing (bounces, auto-replies, quoted text), DNS readiness
  sending/      sending state and kill switch, policy (ramp, office hours), mailboxes, the sender, bounces
  followups/    queueing due follow-ups for review
  replies/      mailbox polling and ingest, matching, classification and drafting, conversations
  ops/          the worker's heartbeat log (runs), health, quiet hours, code version, LAN checks, launchd plists
  web/          FastAPI app, routes (home, auth, admin, mailboxes, discover, companies, catalog, signals, cohorts, campaigns, outbox, conversations), security
config/         salesfinder.yaml, catalog.yaml (products), facts.yaml (claims about Elchemy)
deploy/launchd/ example LaunchAgent plists (rendered by `salesfinder ops plists`)
docs/           operations.md, architecture.md; superpowers/ holds the spec and plans
tests/          pytest suite (HubSpot, Serper, web, DNS, mail, verifier and Claude fakes in tests/fakes/)
```
