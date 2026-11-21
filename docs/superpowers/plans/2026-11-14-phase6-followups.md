# Phase 6 follow-ups (deferred review items)

These come from the per-task and whole-branch reviews of Phase 6: sending, follow-ups, bounces, the kill switch, IMAP replies and unsubscribes, Claude reply drafts, and the dashboard.

Phase 6 closed with:
- `make check` green: 2225 tests, ruff, format and mypy strict.
- The polluted-env hermetic run green.
- Migration head at `0015`.

The Phase 5 hard requirements 1–12 are all met. The final review checked each one with file:line evidence.

Earlier follow-up lists still apply where they aren't done yet. The Phase 5 "Can wait" items are unchanged.

## For Phase 7 (ops / loop)
1. **Deploy note.** Restart web and worker together after upgrading. The advisory-lock key form changed in Phase 6. The README says so; the launchd/ops docs must repeat it.
2. **CSV export + excluded filter.** This was deferred by the Phase 6 plan. There is no company export in the code yet. When it is added, filter out excluded and suppressed companies (Phase 3 carry-over).
3. **Quiet hours.** Replies are exempt from office hours (P6-E). Cold mail and follow-ups respect them. Check the heartbeat cadence against this when the loop is formalised.
4. **First real mailbox.** No real mailbox has been exercised yet; every test uses fakes.
   - Before going live, an admin adds a Workspace mailbox on the sending domain (never elchemy.com).
   - Put the app password in `.env` (`SALESFINDER_MAILBOX_PASSWORDS`, one place only).
   - Press Test connection. New mailboxes are held until a test passes (P6-W).
   - Check DNS, then resume sending.
5. **Unread mail on held mailboxes.** A mailbox held as "Not tested yet" or "Settings changed" isn't read (P6-V/P6-W). The worker prints a line for each one. Consider an alert in the ops heartbeat so an opt-out can't sit unread.

## Can wait
- **Mail parsing:**
  - `_cap_raw` counts any `--` line, not only declared boundaries.
  - References keeps the last 100 ids, but not the thread root.
  - The EHLO hostname isn't IDNA-encoded.
  - IMAP folder names aren't mUTF-7.
- **Ingest:**
  - `after_bounce` can be lost on a crash, with no repair job.
  - A colleague's From is suppressed without its own recipient lock.
  - A phrase-only unstructured bounce counts as hard ("mailbox unavailable" can be a 4xx).
  - A mail with no Message-ID can be stored twice after a UIDVALIDITY change.
  - While "Skipped a mail we couldn't read" shows, newer reader errors aren't stored on the folder. A separate column would fix that.
  - A client that turns the straight quotes in our opt-out line into curly quotes defeats the footer cut, which falls back to the P6-M false positive.
- **Locks:**
  - `app_settings` isn't listed in the documented lock order. It is only taken after the outreach locks, or alone.
  - `refresh_suppressed_blocks` holds up to 500 addresses' company locks in one transaction.
  - The Sweep runs self-heal before `stop_excluded`; an exception skips the stop for that pass.
  - In theory, a suppression `FOR UPDATE` can deadlock three ways with an admin remove and a contacts-check delete. Postgres aborts one side and ingest retries.
- **Bounces:** a bounced pattern can be re-confirmed after an A→B→A relearn.
- **Sender:**
  - `_may_have_gone` scans audit_log. It is now indexed by `(action, target_id)`; consider a `may_have_gone_at` column.
  - The gate wrapper is triplicated (outbox, sender, followups).
  - `apply_sent` has a function-local import cycle.
  - Withdraw leaves `last_send_error`.
- **Mailboxes:**
  - Test connection doesn't verify that an admin-set Sent folder exists.
  - `retry_on_conflict` on check-dns and test re-runs the network I/O.
  - The "untested" check compares exact hold texts.
- **Admin:** the suppressions page shows the newest 200, with no paging.
- **Config:**
  - The length of `retry_minutes` isn't checked against `max_send_attempts`.
  - There are stale comments in `0013_sending.py` and the `outreach.py` gate_errors comment.
- **Tests:**
  - No pytester test shows the network and claude guards' teardown fail firing when an error is swallowed.
  - The guards are function-scoped, so session fixtures and threads aren't covered.
  - Some sender and followups paths aren't tested: re-check under lock in `queue_due`, resend or mark-sent of an archived campaign.
  - The CLI doctor's all-pass exit 0 path isn't covered.
- **Design notes, fail-safe by ruling:**
  - An unmatched unsubscribe trusts an unauthenticated From (P6-L).
  - A prompt-injected `auto_reply` can close the sender's own conversation.

## Rulings made in Phase 6
P6-A … P6-W. They are recorded in the Phase 6 SDD ledger and summarised in the final hand-off message. Key ones for future work:
- **P6-J/V/W:** credentialed mailboxes are read whether enabled or not, except while held untested.
- **P6-L/M/S:** opt-out recall over precision, and an unsubscribe outranks every other suppression reason.
- **P6-N/Q/R:** a teammate-written reply always goes. `taken_over` is terminal for automation. A Gmail-side takeover cancels every unsent message.
- **P6-U:** `SALESFINDER_MAILBOX_PASSWORDS` is read as one JSON string, so env and `.env` are never merged.
