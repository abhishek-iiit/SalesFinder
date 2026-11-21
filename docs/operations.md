# Operations

How to run SalesFinder on the office Mac, keep it running, upgrade it, check on it, back it up, bring the first real mailbox online, and do the real dry run before any email goes out. How it works inside is in [architecture.md](architecture.md); day-to-day use of the dashboard is in the [README](../README.md).

Values below such as `YOU`, `salesfinder-mac.local`, `192.168.1.20` and `sending-domain.example` are placeholders: use your own. Secrets (the secret key, mailbox app passwords, API tokens) go only in `.env` on the Mac, never in these docs, a plist, a ticket or a chat.

## 1. Run it on the office Mac

SalesFinder runs as two processes, the web app (`salesfinder web`) and the worker (`salesfinder worker`), kept running by **launchd** as two LaunchAgents of the operator's macOS user. They are LaunchAgents, not system daemons, because the Claude CLI uses the operator's own Claude subscription login, which lives in that user's session.

### The Mac itself

- **Decide: FileVault or automatic login.** LaunchAgents start only when the operator's user logs in. macOS doesn't offer automatic login while FileVault (disk encryption) is on, so choose one:
  - **FileVault on (recommended).** The disk is encrypted. After a power cut or a restart nothing runs until someone types the operator's password at the Mac. Keep a way to notice: the Runs page and the banner go red once teammates open the dashboard again.
  - **FileVault off, automatic login on** (System Settings > Users & Groups > *Automatically log in as*). SalesFinder comes back by itself after a power cut. The cost: everything on the disk sits unencrypted, including `.env` (with the mailbox app passwords and API keys), the database, and the mkcert `rootCA-key.pem`. Anyone who takes the Mac or its disk can read them. Choose this only if the Mac is physically secure.

  Either way, lock the screen: System Settings > Lock Screen > *Require password after screen saver begins or display is turned off*: **Immediately**, with a short display-sleep time. LaunchAgents keep running while the screen is locked, so locking costs nothing.
- **Never sleep.** Stop the Mac from sleeping (the display may sleep) and have it start again after a power failure:

  ```sh
  sudo pmset -a sleep 0 disksleep 0
  sudo pmset -a autorestart 1
  ```

- **Give the Mac a fixed address** on the office network (a DHCP reservation on the router), and note its local hostname (System Settings > General > Sharing > Local hostname, e.g. `salesfinder-mac.local`).
- **Postgres 17** from Homebrew, started at login:

  ```sh
  brew install postgresql@17
  brew services start postgresql@17
  ```

- **The Claude CLI**, installed and signed in **as the operator** (run `claude` once in Terminal and log in with the subscription). The worker uses that login. It is never given an API key.

### Install

From the folder you cloned the repo into (e.g. `/Users/YOU/SalesFinder`):

```sh
make setup        # uv sync, git hooks, creates the databases, copies .env.example to .env
```

`make setup` runs `createdb`, so your macOS user needs a Postgres role allowed to create databases. Homebrew's Postgres makes the user who installed it a superuser, so this normally just works. Otherwise ask whoever runs the database for a role with `CREATEDB`, or have them create `salesfinder` and `salesfinder_test` for you.

Edit `.env`: at least `SALESFINDER_SECRET_KEY` (see the README), plus the LAN settings in section 2. Then:

```sh
make migrate
uv run salesfinder users create-admin --email you@elchemy.com --name "You"
uv run salesfinder doctor
```

### Render and load the LaunchAgents

```sh
make plists       # writes the two plists into ~/Library/LaunchAgents and prints how to load them
make start        # loads both: they start now, at every login, and again whenever they exit
```

`make plists` runs `uv run salesfinder ops plists --out ~/Library/LaunchAgents`. It must run from the SalesFinder folder after `uv sync` (it refuses otherwise). It writes `com.elchemy.salesfinder.web.plist` and `com.elchemy.salesfinder.worker.plist`, built from what it finds on this Mac, not from fixed values:

- **the program**: this folder's `.venv/bin/salesfinder` with `web` or `worker`. It doesn't use `uv run`, which may re-sync or reach the network at start;
- **the working directory**: this folder, so `.env` and `config/` are read from here, as when you run it by hand;
- **`PATH`**: the folder holding the Claude CLI first (found from `SALESFINDER_CLAUDE_BIN`, by default `claude` on your shell's PATH), then the venv, Homebrew and the system folders. launchd's own PATH lacks `~/.local/bin` and Homebrew, so without this the worker couldn't find `claude`. If the CLI can't be found, the command stops with "Claude CLI not found: set SALESFINDER_CLAUDE_BIN to its absolute path.";
- **logs**: `SALESFINDER_LOG_FILE` set to `~/Library/Logs/SalesFinder/web.log` or `worker.log`. The app writes there itself (see *Logs* below). launchd's own output files, `web.crash.log` and `worker.crash.log`, catch only what bypasses logging, such as a traceback when a process fails to start (`--log-dir` changes the folder; it is created);
- **restarts and stops**: `KeepAlive` restarts a process that exits, at most once every 30 seconds, for as long as it stays loaded. On stop, launchd sends SIGTERM and waits before it kills: 30 seconds for the web app and 600 seconds for the worker. On SIGTERM the worker finishes its current pass but starts nothing new that could take long: no further reply or draft for Claude (a Claude call already running finishes), no new discovery job, and a running job stops before its next search. So a planned stop ends well inside those 600 seconds. A worker killed anyway (a power cut) is safe: the next start closes its run as "crashed".

A plist holds no secret. Its only `SALESFINDER_*` variable is `SALESFINDER_LOG_FILE`. Everything else comes from `.env` at start. To see what would be written without writing anything, run `uv run salesfinder ops plists`. Tracked examples, rendered for a placeholder user `YOU`, are in [`deploy/launchd/`](../deploy/launchd/).

`make plists` won't overwrite existing files. Re-render after moving the folder, moving the Claude CLI or changing the log folder, or after an upgrade whose notes say the plists changed, then restart:

```sh
uv run salesfinder ops plists --out ~/Library/LaunchAgents --force
make restart
```

| Command | Does |
| --- | --- |
| `make start` | `launchctl bootstrap` both agents (refused if one is already loaded: run `make stop` first) |
| `make stop` | `launchctl bootout` the worker, then the web app, and wait until both are gone (the worker finishes its pass first, so this can take a few minutes); then set each non-empty crash log aside as `*.crash.log.prev` |
| `make restart` | `make stop` then `make start`, always both; loading again re-reads the plists |
| `make logs` | `tail -F` both logs and both crash logs |

These are steps the operator runs by hand. Nothing in SalesFinder loads, starts or stops launchd agents by itself. To see what launchd thinks of an agent: `launchctl print gui/$(id -u)/com.elchemy.salesfinder.worker` (look at `state`, `pid` and `last exit code`).

### Logs

Each process writes its own log, `~/Library/Logs/SalesFinder/web.log` and `worker.log` (the path comes from `SALESFINDER_LOG_FILE` in the plist). Uvicorn's lines go there too. `make logs` follows both, plus `web.crash.log` and `worker.crash.log`, launchd's output files, which catch only what bypasses logging (a traceback at start-up, say). Console.app shows them all as well. Lines are plain text, never coloured. Set `SALESFINDER_LOG_JSON=true` in `.env` for JSON lines. A log file the app creates is readable only by the operator (mode 600). Logs never hold passwords, tokens, email text or anything scraped from websites: only counts, our own messages, mailbox addresses and error type names. Commands you run in Terminal (`uv run salesfinder …`) log to the terminal as before, because `.env` doesn't set `SALESFINDER_LOG_FILE`.

macOS doesn't rotate these files on its own. Add a newsyslog rule (as root), e.g. `/etc/newsyslog.d/salesfinder.conf`:

```
# logfilename                                     [owner:group]  mode count size(KB) when flags
/Users/YOU/Library/Logs/SalesFinder/web.log       YOU:staff      600  5     10240    *    N
/Users/YOU/Library/Logs/SalesFinder/worker.log    YOU:staff      600  5     10240    *    N
```

When a log reaches 10 MB, newsyslog renames it to `web.log.0` (older ones shift up to `.4`, the oldest is deleted) and creates a new, empty `web.log` with mode 600. `N` means no process is signalled, and none needs to be. The app checks its log file before every line, sees the new file and writes there, so nothing is lost and no restart is needed. Don't add the `J` or `Z` flag (compression): a line written in the moment between the rename and the app's switch would land in the file being compressed and could be lost. Leave the crash logs out of the rule, because launchd holds them open while an agent is loaded and keeps appending to them. Instead, `make stop` (and so `make restart`), once both agents are unloaded, renames each non-empty crash log to `web.crash.log.prev` or `worker.crash.log.prev`, replacing the older copy. So they stay small, and the last crash output is still there to read after a stop. `make logs` and this tidy-up always use the default folder, `~/Library/Logs/SalesFinder`: if you rendered the plists with `ops plists --log-dir <folder>`, they don't follow it, so `tail -F` the files in that folder yourself and tidy its crash logs by hand (only while both agents are unloaded).

### The firewall

Turn the macOS firewall on (System Settings > Network > Firewall), so only the apps you allow accept connections. Then allow incoming connections for the venv's Python: macOS asks the first time the web app listens on the network, or add it by hand. The process is the real Python behind `.venv/bin/python`, not the symlink:

```sh
sudo /usr/libexec/ApplicationFirewall/socketfilterfw --add "$(realpath .venv/bin/python)"
sudo /usr/libexec/ApplicationFirewall/socketfilterfw --unblockapp "$(realpath .venv/bin/python)"
```

A Python upgrade (a new uv-managed interpreter) needs this again.

## 2. LAN and HTTPS

Teammates open the dashboard from their own computers on the office network, over HTTPS only. Startup refuses LAN mode without TLS. The app is **not exposed to the internet**: don't forward its port on the router or put it behind a public proxy.

### A certificate with mkcert

On the Mac, create a local certificate authority once, then a certificate for every name teammates will type (the `.local` name and the fixed IP):

```sh
brew install mkcert
mkcert -install
mkdir -p ~/SalesFinder-certs && cd ~/SalesFinder-certs
mkcert salesfinder-mac.local 192.168.1.20
```

mkcert names the files after the first name plus the number of extra names. Here it writes:

| mkcert wrote | Setting in `.env` |
| --- | --- |
| `salesfinder-mac.local+1.pem` (certificate) | `SALESFINDER_TLS_CERTFILE=/Users/YOU/SalesFinder-certs/salesfinder-mac.local+1.pem` |
| `salesfinder-mac.local+1-key.pem` (its private key) | `SALESFINDER_TLS_KEYFILE=/Users/YOU/SalesFinder-certs/salesfinder-mac.local+1-key.pem` |

Then the rest of LAN mode in `.env`:

```sh
SALESFINDER_BIND_HOST=0.0.0.0
SALESFINDER_ALLOWED_HOSTS=["salesfinder-mac.local","192.168.1.20","127.0.0.1","localhost"]
SALESFINDER_PUBLIC_URL=https://salesfinder-mac.local:5555
```

- `SALESFINDER_ALLOWED_HOSTS` lists every name or address teammates' browsers use (a JSON list). A request for any other name gets "Invalid host header", which protects against DNS rebinding. Never use `"*"`.
- `SALESFINDER_PUBLIC_URL` is the address teammates open (https, host in the allowed hosts, no path). Invite links use it, so a link made while you browse on `localhost` still works for them.

Run `make restart`, then `uv run salesfinder doctor`. Its `https` check reads the certificate and key, and its `network` check names any LAN mistake: only loopback names allowed, `"*"`, no public URL, an `http://` public URL, or a public URL whose host isn't allowed. Admins see the same warnings in the **Network** section of Settings.

### Each teammate trusts the mkcert root CA

The certificate is signed by the Mac's own mkcert authority, so every teammate's computer must trust that authority once, or their browser warns about the certificate. On the Mac, find it:

```sh
mkcert -CAROOT
```

Copy **only `rootCA.pem`** from that folder to each teammate (a USB stick or a direct file share). **Never copy `rootCA-key.pem`.** Anyone holding it can make certificates every teammate's computer would trust for any website. It stays on the Mac.

On each teammate's computer:

- **macOS**: double-click `rootCA.pem` to add it to the login keychain in Keychain Access, open it, expand *Trust* and set *When using this certificate* to *Always Trust*. Or in Terminal: `sudo security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain rootCA.pem`.
- **Windows**: open `rootCA.pem` (rename it to `rootCA.crt` if needed) > *Install Certificate* > *Local Machine* > *Place all certificates in the following store* > *Trusted Root Certification Authorities*.
- **Firefox** keeps its own list: Settings > Privacy & Security > Certificates > *View Certificates* > *Authorities* > *Import*, and tick *Trust this CA to identify websites*.

Then open the public URL (e.g. `https://salesfinder-mac.local:5555`). If `.local` names don't resolve on a computer, use the IP address. It is in the certificate and the allowed hosts too.

### Certificate expiry

mkcert certificates last a little over two years. Check when it ends:

```sh
openssl x509 -enddate -noout -in ~/SalesFinder-certs/salesfinder-mac.local+1.pem
```

To renew, run the same `mkcert salesfinder-mac.local 192.168.1.20` again in that folder, then `make restart`. Teammates don't need to do anything: the root CA stays the same.

## 3. Upgrading

```sh
make stop
git pull
uv sync
uv run salesfinder db upgrade
make start
```

**Restart the web app and the worker together**, never one alone. They share database locks and must run the same code: after one upgrade the per-recipient lock changed form, and an old process holding the old form wouldn't see the new one's locks. `make stop`/`make start` and `make restart` always act on both. If only one was restarted, every page shows "The web app and the worker are running different versions — restart both together." until you run `make restart`.

If the upgrade changed the plists (its notes say so), re-render them before `make start`: `uv run salesfinder ops plists --out ~/Library/LaunchAgents --force`. `make stop` waits for the worker to finish its current pass, which can take a few minutes while Claude is writing.

After editing `.env` or the YAML files in `config/` (both are read only at start), run `make restart`.

## 4. Is it running?

- **The banner.** Every page shows a red banner while the worker isn't working:
  - it hasn't run yet;
  - it isn't running (stopped, or stopped without shutting down);
  - it isn't running because it lost its database lock (Postgres restarted under it; launchd restarts it);
  - it stopped responding: no beat for 3 minutes (`ops.worker_stale_seconds`), and the banner names the step it was in;
  - the web app and the worker run different versions.

  No banner means the worker is running. A worker started with `--no-send` (the dry run's) reads **"Running with --no-send (nothing will be sent)"** on Today, on Runs and in `uv run salesfinder runs`, and `uv run salesfinder doctor` warns about it: one left running after a dry run means no email ever goes out.
- **Today**'s *Worker* card shows the last pass, failures in the last 24 hours and the latest ones, plus a red alert for each mailbox whose password is set but whose connection hasn't passed **Test connection** (its replies and opt-outs aren't being read).
- **Runs** (`/runs`, any teammate) shows the heartbeat log: one row per 15-minute window (`usage.heartbeat_interval_minutes`) with its passes, failed steps, Claude calls and cost, and lines, plus Claude calls today against the daily budget and discovery spend this month against its budget. The open row reads *Running*, *Running with --no-send*, or *No beat* when its worker stopped beating.
- From the command line:

  ```sh
  uv run salesfinder runs       # health line and the 10 latest runs
  uv run salesfinder doctor     # every self-check, including worker, network, mailboxes and sending
  ```

  `/healthz` answers `{"worker": "<state>", …}` for a monitoring check.

**Quiet hours.** Between `usage.quiet_hours.start` and `end` (default 22:00–07:00 America/New_York) only the worker's **Claude steps** wait: replies aren't classified or answered, and nothing new is drafted. This leaves the Claude subscription free for the operator. Mail reading (so opt-outs and bounces are honoured at once), sending in each recipient's office hours, follow-up queuing, HubSpot, discovery, profiling and contact checks all keep running. The run log says "Quiet hours (…): Claude steps wait until 07:00." once per quiet period. A quiet worker is a running worker, so there is no banner.

## 5. Backups

All of SalesFinder's state is in the `salesfinder` database. Secrets aren't in it: keep a copy of `.env` somewhere safe and separate (a password manager), not next to the dumps. Take a nightly dump, e.g. with `crontab -e` as the operator (cron needs `%` escaped):

```
30 2 * * * mkdir -p "$HOME/Backups" && /opt/homebrew/opt/postgresql@17/bin/pg_dump -Fc salesfinder > "$HOME/Backups/salesfinder-$(date +\%F).dump"
```

By hand, the same thing is `pg_dump -Fc salesfinder > ~/Backups/salesfinder-$(date +%F).dump`. A user LaunchAgent with `StartCalendarInterval` works as well. Copy the dumps off the Mac now and then, and delete old ones.

To restore, check the dump first in a scratch database, then replace the live one with both processes stopped:

```sh
createdb salesfinder_restore_check
pg_restore -d salesfinder_restore_check ~/Backups/salesfinder-2026-11-18.dump
dropdb salesfinder_restore_check

make stop
dropdb salesfinder
createdb salesfinder
pg_restore -d salesfinder ~/Backups/salesfinder-2026-11-18.dump
uv run salesfinder db upgrade
uv run salesfinder sending pause --reason "Restored from a backup"
make start
```

Pause sending **before** `make start`: a restored database is the state at the dump's time, and sending is as the dump recorded it. Email sent after the dump isn't in it, so emails it still shows as approved may already have gone out, and the worker would send them again. Before an admin resumes sending in **Settings**, compare **Outbox › Approved — waiting to send** with each mailbox's Sent folder in Gmail, and withdraw or archive anything that already went.

## 6. First real mailbox

Until now every test used fake mail servers. Bring the first real mailbox online in this order:

1. **The sending domain.** Buy a separate domain for outreach. **Never elchemy.com**, which SalesFinder refuses anyway: a cold-email complaint must never hurt the company's own mail. Set it up in Google Workspace and create the mailbox user.
2. **DNS records** for that domain (see the README's *DNS records*): SPF including `include:_spf.google.com` and ending `~all` or `-all`; DKIM from Google Admin (Apps > Google Workspace > Gmail > Authenticate email, selector `google`); one DMARC record at `_dmarc.<domain>` with `p=none`, `quarantine` or `reject`; and MX so replies and bounces arrive.
3. **The app password.** In that account turn on 2-Step Verification, then create an app password (your Workspace admin may first have to allow app passwords and IMAP). Put it in `.env` on the Mac, in `SALESFINDER_MAILBOX_PASSWORDS`, keyed by a password name you choose:

   ```sh
   SALESFINDER_MAILBOX_PASSWORDS={"sales_box": "<the 16-character app password>"}
   ```

   Set it **in one place only**, `.env`. An exported `SALESFINDER_MAILBOX_PASSWORDS` in a shell replaces the `.env` value as a whole, and a stale one would hide this one. It never goes in the database, a page, a plist or a log. Then run `make restart`.
4. **Add and test it.** An admin opens **Mailboxes** (`/admin/mailboxes`), adds the mailbox (address, side, username, the password name `sales_box`, Gmail's servers by default) and presses **Test connection**. A new mailbox is held, "Not tested yet", until that test passes. Nothing logs in with its password before then, not even to read replies, and it can't be enabled. Then press **Check DNS**. Cold sending stays blocked until SPF, DKIM, DMARC and MX all pass, and the check must be fresh (24 hours).
5. **Warm-up.** When the external warm-up service is switched on for the mailbox, set the mailbox's warm-up start date to that day, then **Enable** it.
6. **Wait out the 14 days.** No cold email goes from it during the first 14 days of warm-up. After that its cap starts at 5 a day and rises by 3 a day.
7. **Resume sending last.** First make sure the dry run left nothing behind: the dry-run campaign shows **Archived**, **Outbox › Approved — waiting to send** is empty, and `pgrep -fl "salesfinder worker"` shows exactly one worker, without `--no-send`. Anything still approved there is real email, and it would go out the moment sending resumes. Only after the real dry run below has passed and been cleaned up does an admin press **Resume sending** in Settings (or run `uv run salesfinder sending resume --yes`). Watch Today and the bounce rate for the first days. The kill switch pauses everything if more than 5% of the last 50 sends bounce.

## 7. Real dry run (sending paused)

This is a **manual step the operator runs**, by hand, on the office Mac, with the real Claude CLI, Serper, HubSpot and real websites. No automated test or script does it, and nothing in it sends email: sending stays paused, and the worker runs with `--no-send` as a second lock.

**Precheck: sending must be paused.**

```sh
uv run salesfinder sending status
```

The first line must read "Sending is paused: …". If it says "Sending is on.", pause it first:

```sh
uv run salesfinder sending pause --reason "Dry run"
```

**Run the worker by hand.** Stop the launchd agents, then start the web app and a no-send worker in two Terminal windows:

```sh
make stop
uv run salesfinder web
```

```sh
uv run salesfinder worker --no-send
```

With `--no-send` the Sending step never runs: there is no SMTP connection at all, and each run row says "Sending skipped: this worker runs with --no-send." Mail reading and everything else run as usual.

`make stop` unloads the agents only until the next login: the plists stay in `~/Library/LaunchAgents`, so if the Mac restarts or the operator logs out and in during the dry run, launchd starts the normal web app and worker again (the Terminal worker then reports "Another SalesFinder worker is already running."). The pause still protects you, because nothing is sent while sending is paused. Still, run `make stop` again before you go on.

**Walk the loop:**

1. Confirm the signals you want collected (**Signals** page, or `uv run salesfinder signals --confirm free`).
2. Start one small discovery job on **Discover** (10 searches or fewer) and approve it. The worker runs it and profiles and checks the contacts of the companies it finds.
3. Create a cohort on **Cohorts**, then a campaign on **Campaigns** with that cohort. On the campaign's page, after it is created, choose the planned mailbox (it may stay disabled), then **Activate** it. Activation is refused until `config/facts.yaml` has a confirmed persona (sender name and title) for the campaign's side and Elchemy's confirmed postal address. Confirm those first if needed, then restart the two Terminal processes (Ctrl-C each, then start them again as above): they read `config/` only at start.
4. Watch **Runs**: no failed steps (or only ones you understand), Claude calls today well within the daily budget, discovery spend as estimated, and the quiet-hours line if you're running in the evening (drafting waits until morning).
5. In the **Outbox**, review a few drafts and approve some.
6. Check they sit in **Approved — waiting to send**, held by the pause. `uv run salesfinder sending status` shows `sent today 0` for every mailbox, and nothing appears in the mailbox's Sent folder in Gmail.

**Finish.** **Archive** the dry-run campaign on its page. This cancels all its unsent emails, approved ones included, and stops its sequences. Withdrawing approvals alone isn't enough: withdrawn emails go back to To review, where anyone could approve them again. Check that **Outbox › Approved — waiting to send** is empty. Then stop both Terminal processes (Ctrl-C; the worker finishes its pass first) and bring the launchd agents back:

```sh
make start
```

Then check that exactly one worker runs, and that it isn't the `--no-send` one (a Terminal worker left running would hold the single-worker lock, and nothing would ever be sent):

```sh
pgrep -fl "salesfinder worker"
```

It must list exactly one process, `…/.venv/bin/salesfinder worker`, without `--no-send`. If it lists a `--no-send` worker, find its Terminal window and stop it with Ctrl-C (launchd's worker takes over within 30 seconds), then check again.

**It passed when:**

- [ ] `sending status` said paused before and after, and `sent today 0` throughout;
- [ ] no mailbox's Sent folder has anything from the dry run;
- [ ] the discovery job finished within its estimate, and companies were profiled with contacts checked;
- [ ] drafts appeared in To review, written from confirmed facts only, with signature and footer, and some passed the gate;
- [ ] approved emails waited in *Approved — waiting to send*, and nothing left;
- [ ] after Finish, the dry-run campaign shows **Archived** and *Outbox › Approved — waiting to send* is empty;
- [ ] Runs showed the heartbeat every window, no unexplained failures, and Claude calls within budget;
- [ ] no banner appeared while the worker ran;
- [ ] after `make start` the banner stayed away, and `uv run salesfinder runs` says "Worker running" (not "Worker running with --no-send");
- [ ] `pgrep -fl "salesfinder worker"` shows exactly one worker, without `--no-send`.

Only then go on to step 7 of *First real mailbox*.

## 8. Troubleshooting

| You see | Means | Do |
| --- | --- | --- |
| Banner "The worker hasn't run yet — start it (see Operations)." | The worker never started on this database | `make plists` and `make start` (section 1) |
| Banner "The worker isn't running (stopped …)." | It was stopped (`make stop`, or a Ctrl-C on a Terminal worker), or it keeps exiting at start (launchd retries every 30 seconds while it is loaded) | `make logs`: `worker.crash.log` shows a start-up failure. Fix it, then `make restart` (or `make start` if it was stopped) |
| Banner "…stopped without shutting down…" or "The worker stopped responding … (during <step>)." | Killed, power loss, or hung in that step | `make logs`; `make restart`. If it keeps happening in the same step, note the step and the log lines |
| Banner "The web app and the worker are running different versions — restart both together." | Only one was restarted after an upgrade | `make restart` |
| `make start`: "Bootstrap failed: 5: Input/output error" | That agent is already loaded | `make stop`, then `make start` |
| A browser shows "Invalid host header" | The name typed isn't in `SALESFINDER_ALLOWED_HOSTS` | Add it to the list in `.env` (and to the certificate if it's new), then `make restart` |
| A certificate warning in a teammate's browser | That computer doesn't trust the mkcert root CA, the name isn't in the certificate, or it expired | Install `rootCA.pem` (section 2), use a name the certificate lists, check expiry with `openssl` |
| Runs/Today: "Writing stopped: Claude returned an error: not signed in or quota reached." (or "Replies stopped: …") | The Claude CLI is signed out or over its quota | As the operator, run `claude` in Terminal and log in again, or wait for the quota. The next pass picks it up |
| "Writing stopped: Claude CLI not found on PATH." | The CLI moved or the plists are older than it | Set `SALESFINDER_CLAUDE_BIN` to its absolute path, re-render the plists (section 1), `make stop`, `make start` |
| "HubSpot data is stale: …" | No successful sync for 48 hours. Cold sending holds until it's fresh | Check `SALESFINDER_HUBSPOT_TOKEN`, run `uv run salesfinder hubspot sync` to see the error, then **Sync now** in Settings |
| Run log "Not reading <mailbox>: test its connection first." (and a red alert on Today) | That mailbox's connection settings are new or changed and haven't passed a test, so its replies and opt-outs aren't read | An admin presses **Test connection** on Mailboxes |
| `worker.crash.log` "Another SalesFinder worker is already running." over and over | A second worker (often one in a Terminal) holds the single-worker lock, and launchd's copy keeps exiting and retrying | `pgrep -fl "salesfinder worker"` lists them. Stop the extra one (Ctrl-C), or `make stop` if you meant to run it by hand |
| `worker.crash.log` "Lost the worker lock (database restarted?) — stopping so launchd restarts me." (Runs: the row ends with it; banner "…it lost its database lock…") | Postgres restarted under the worker | Nothing: launchd restarts it. If Postgres is down, `brew services restart postgresql@17` |
| Today/Runs "Running with --no-send (nothing will be sent)", or a `doctor` warning saying so | A dry-run worker (`worker --no-send`) is still running, so nothing is sent | `pgrep -fl "salesfinder worker"`; stop the `--no-send` one with Ctrl-C in its Terminal. launchd's worker takes over |
