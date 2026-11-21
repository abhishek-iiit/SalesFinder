# Phase 5 follow-ups (deferred review items)

From per-task and whole-branch reviews of Phase 5 (brain, campaigns, writer,
gate, outbox, dashboard). Earlier lists (`2026-11-07-phase{1,2,3}-followups.md`,
`2026-11-08-phase4-followups.md`) still apply where not done — Phase 4's
"hard requirements for Phases 5–6" items 5, 6, 8 and 10 are Phase 6 work.

## Hard requirements for Phase 6 (sending + replies)
1. **Step 2 is half-built.** It is stored `pending`, never gated, never seen
   by a teammate, `due_at` unset. `approve`/`edit`/`reject`/`recheck` require
   `sequence.status == "awaiting_approval"` and `waiting_for_review()` is
   step 1 only. Phase 6 must re-queue step 2 for approval when due (allow
   approval while the sequence is `active` and step 1 `sent`), and never send a
   `pending` message directly. Step 2 is stored with `gate_errors=[]` — store
   `null` or the real gate result.
2. **Re-gate immediately before every send**, in one transaction holding the
   message lock and the recipient advisory lock: refresh MX via `mx_for` first
   (outside the transaction), then live exclusions (incl. new suppressions and
   unsubscribes), `Company.status`, `Company.disqualified`, `campaign.status`
   (archived never sends; **paused never sends** — approval is allowed while
   paused), and the global sending pause.
3. **Send exactly the stored `subject`/`body`** — only MIME (UTF-8) encoding;
   never re-render or append anything after the gate. Store the final headers
   (Message-ID; In-Reply-To/References for step 2).
4. **Record what was in force at send time:** address status,
   `override_sendable`, `blocked_reason`, MX state + `checked_at`, gate result,
   mailbox and persona used. `messages` has no columns for these yet.
5. **Mailbox + persona:** campaigns/sequences have no mailbox; add one (backfill
   or refuse existing `approved` messages). From name/mailbox must match the
   side's signature persona (the gate checks the exact tail).
6. **One domain form for SMTP:** `Sequence.recipient` is email_validator's
   normalised form (may keep a Unicode domain); MX uses IDNA. Decide SMTPUTF8.
7. **Withdrawing approval:** there is no un-approve/cancel. Pausing,
   archiving, exclusion, bounce, unsubscribe and human takeover must cancel
   queued `approved`/`pending` messages and stop their sequences (archive
   already does this as of the Phase 5 fix wave).
8. **Writing must not starve sending:** the writing step runs inline in the
   worker loop (worst case ~40 min). Time-box it or run it as its own task.
9. **Exclusions stop sequences:** when a company is excluded or an address
   suppressed, stop matching in-progress sequences in the worker, not only at
   the send-time gate. Phase 3 carry-over: filter excluded companies from
   exports.
10. **Lock order:** message → sequence → advisory(hashtext(recipient)). Never
    take a campaign `FOR UPDATE` after the advisory lock (drafting takes
    campaign → advisory). Archive takes campaign → messages (id order) →
    sequences (id order).
11. **Select on status only:** cancelled messages keep `approved_by_id` /
    `approved_at`; the sender must select `status == 'approved'`, never on
    `approved_at`.
12. **Sender vs archive:** archive can cancel an approved message while the
    sender works on it. Claim each message under a row lock consistent with
    archive's order, re-check message + campaign status inside that
    transaction, and do no SMTP inside a transaction.

## Can wait
- **Gate false negatives** (fine while a human approves every email; Important
  before any autopilot): certifications outside `CERT_NAMES` ("FDA approved",
  HACCP, SQF, Non-GMO); "price is 4,500 for the full container"; "Rate: 900
  MT."; `tel:`/`sms:` links; samples offer while `sample_dispatch` unconfirmed.
- **Brain:** cancelled Claude calls aren't recorded; budget overshoots slightly
  when worker and CLI draft concurrently.
- **Outbox:** `bulk_approve` holds one advisory lock per message until commit
  (deadlock only on a `hashtext` collision — keep batches ≤ 50); bulk skip
  hides the missing-persona reason; edit version is the transaction's `now()`
  (a version counter / `clock_timestamp()` would be stricter); last-error
  shows only the latest campaign error.
- **Bulk approve vs archive:** a bulk approval spanning several sequences can
  still deadlock with archive when a later sequence's follow-up id sorts below
  an earlier sequence's first email. Rare; Postgres aborts one side and the
  route's retry-once/409 handles it. To remove it: order bulk work by each
  sequence's lowest message id, or have archive lock sequence by sequence in
  the Outbox's order.
- **MX refresh:** bulk approve refreshes up to 50 stale domains one after
  another (minutes during a DNS outage) — run concurrently under the checker's
  semaphore or set an overall deadline; the drafting pre-pass also refreshes
  domains of addresses `select_recipient` will skip (busy, held guesses).
- **Drafting vs cohorts:** disqualified companies are never drafted, even in a
  cohort with `include_disqualified`.
- **Migration 0012** refuses (rather than renames) existing case-duplicate
  campaign names.
- **Logging:** with `SALESFINDER_LOG_LEVEL=DEBUG`, 4 CLI tests fail because
  asyncio's "Using selector" debug line lands in captured CLI output.
