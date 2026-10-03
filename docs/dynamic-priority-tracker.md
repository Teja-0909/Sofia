# Dynamic priorities and one-shot work checkpoints

This is the approved first increment: one user-chosen current focus, three visible
queued outcomes, and other saved outcomes that remain retrievable. Queue position
is explicitly user-controlled; there is no hidden importance score. The feature
ships **disabled**, including checkpoint delivery. It does not import historical
CAT/study goals, notebook prose, sprints, reminders or old open threads.

## User-facing behavior

With `ENABLE_OUTCOMES=true`, ordinary direct Telegram text can propose new or
changed priorities, deadlines, progress, blockers, next steps, queue order,
work-only quiet or a one-shot checkpoint. Interpretation is a separate read-only
structured model call. It has no SQL, write or arbitrary action tools.

The application validates the proposed fields, identities, times and revisions,
then renders every meaningful change in a compact preview. Nothing is applied
until a separate Save/yes/confirmation response or the preview's explicit
`/outcome save P...` control. The preview includes parking the previous focus and
cancelling affected old work check-ins. Corrections create a new complete preview;
older previews expire or are superseded. A targeted Telegram reply to an old
preview cannot approve a newer one. Quoted, forwarded, conditional and hypothetical
requests are not direct authority; model output is never a receipt.

Short status and acknowledgement exchanges can still end without a question.
Advice follows the latest direct user choice even while a durable proposal is
pending. The model's saved context gives current outcomes space before older
notebook/summary evidence and includes bounded completed/dropped facts so old
history cannot silently restore an ended priority. Unknown importance, effort,
progress and deadlines stay unknown. User progress is labelled as a user report.
A passed real deadline is shown as needing reassessment, not perpetual urgency.

The deterministic fallback controls are:

```text
/outcomes                         current focus + up to three queued outcomes
/outcomes all                     up to 40 open/recent terminal outcomes
/outcome O1                       one outcome's fields and revision
/outcome add <title>               create a queued outcome
/outcome focus O1                  choose focus; preview linked effects
/outcome park O1                   pause an outcome
/outcome resume O1                 return to queue; no automatic checkpoint
/outcome done O1                   complete that outcome explicitly
/outcome drop O1                   drop that outcome explicitly
/outcome next O1 <next step>
/outcome progress O1 <reported progress>
/outcome block O1 <blocker>
/outcome importance O1 high <reason>
/outcome order O1 1                smaller explicit position appears earlier
/outcome deadline O1 hard 2030-10-04
/outcome quiet 2030-10-04T08:00:00+05:30
/outcome quiet off
/outcome save P<proposal ID>
/outcome discard
```

Simple explicit controls apply directly after validation/readback. Unexpected
linked checkpoint cancellation is previewed before applying. Timed deadlines and
checkpoint creation use the conversational preview so the time/zone is visible.
A date-only deadline stays a date, with no invented midnight. A supplied non-UTC
wall time/offset must round-trip through the stated IANA zone; nonexistent DST
local times are rejected. An explicit offset can disambiguate an autumn fold.
UTC instants remain valid absolute representations. Interpretation quality still
needs a supervised live trial; a model can misunderstand an ambiguous local time.

Outcomes, checkpoint conversations and notification tasks remain distinct:

- `/done <number>`, `/cancel`, `/snooze`, timers and ordinary reminder repetition
  keep their task meanings. Outcome IDs are displayed as `O1`; checkpoint IDs
  are `C1`. A timer finishing never completes an outcome
- Bare “done” resolves plausible outcomes/tasks/timers together and asks for
  scope rather than choosing the wrong record, including after a recent timer
- `/focus` remains a temporary sprint. When tracking is enabled, it is labelled
  subordinate to the canonical outcome focus, optionally linked to it, and
  finishing it does not automatically complete an outcome or another task
- `/pause` continues to pause reminder/timer delivery. `/sleep` suppresses
  unsolicited contact until a new direct incoming message; reminders remain
  saved. Durable work quiet survives restart and also gates legacy background
  routes so they cannot bypass a work-only quiet request; it does not erase alarms

## Durable state and evidence

`outcome_migrations.py` contains versioned additive DDL, indexes and readiness
checks, run by `db.init()`. The same transactional contract is used for SQLite,
the installed libsql HTTP client's batch, and the Hrana fallback batch. Startup
fails rather than hiding a partial/incompatible outcome schema or index.

- `outcomes`: typed fields, chosen state/queue position, optional hard/target
  deadline and zone, reported progress/blocker/next step, field provenance,
  notebook fingerprint and revision
- `outcome_proposals`: source-bound typed bundles, 15-minute expiry, expected
  revisions, preview, operation identity and immutable applied receipt
- `outcome_events`: minimal confirmed operations, source/confirmation references,
  application receipt identity and timestamps; not generated chain-of-thought
- `outcome_control`: per-chat revision/generation and bounded work quiet
- `outcome_checkpoints`: one-shot agreement, outcome revision, due/expiry window,
  delivery epoch, status, job reference and uncertain-send marker
- `outcome_incoming_messages`: message identities only, used to prevent duplicate
  Telegram delivery from fabricating fresh activity or approval

Canonical outcome changes, invalidation of old checkpoints, events and the typed
receipt commit in one transaction. Compare-and-swap guards bind chat, source,
latest-message identity, control/outcome revisions and activation epoch. A reused
confirmation identity cannot approve another proposal. Duplicate requests return
known receipts/current same-source previews or a read-only uncertainty response;
they never get reinterpreted as permission for a different pending proposal.
If an acknowledgement is lost after a database commit, the application reconciles
by operation identity rather than blindly creating another row.

The existing editable Turso notebook, reconciliation, CAS and suppression records
are unchanged. Outcome reads/confirmations reconcile that notebook first and apply
its suppression boundary to current records, terminal records, pending previews,
checkpoint questions and receipts. Nothing regenerates the notebook from outcomes.
A raw notebook or suppression change after confirmation conservatively withholds
that outcome's checkpoint, even if it might be unrelated. At the next relevant
conversation, the current view flags that the notebook changed. An explicit
confirmed outcome update resolves the fingerprint; a new check-in still requires
a new agreement. This conservatism avoids resurrecting deleted evidence but is
not semantic contradiction detection or universal paraphrase erasure. Historical
audit rows are retained, with no new hidden purge job.

## Checkpoint delivery guarantees and limits

Checkpoint delivery additionally requires `ENABLE_OUTCOME_CHECKPOINTS=true`.
Creating/selecting an outcome alone never schedules contact. No automatic daily
planning message, recurring checkpoint, escalating follow-up or inferred progress
loop is added. Existing useful-background policy remains in force; a priority
record alone is not a reason for a message. Requested checkpoint text is delivered
literally with a small caller-owned label; no model runs in the dispatcher.

The existing scheduler polls about every 30 seconds. At proposal time, known
pause/rest, work quiet, configured night hours, recent-conversation quiet and the
shared one-hour cooldown are checked; a conflict asks for an eligible time.
At the final send boundary the dispatcher rechecks flags/epoch, revisions,
cancellation, deadline window, configured timezone/quiet hours, raw notebook and
suppression state, work quiet, pause/rest, latest user identity/conversation time,
shared acknowledged receipts and unanswered-contact limits. Invalid timezone or
quiet-hour configuration fails closed.

It uses the existing durable `tasks.deliver_once` mechanism with a dedicated
checkpoint key plus a shared `background:user:<id>` contact slot. A final atomic
send-marker guard closes database races with new activity and reminder receipts.
A checkpoint is sent once; its outcome stays unresolved. New user activity does
not schedule a new checkpoint. A late/expired checkpoint is marked missed rather
than delivered as a catch-up backlog. Known pre-send failures retry with existing
backoff, at most three attempts inside the agreed window. Normal interpreted
checkpoints propose a 30-minute delivery window; the store rejects windows over
24 hours.

If Telegram accepts a request but its acknowledgement is lost, the result is
**uncertain**, not falsely sent. Once the durable send marker is armed, an
unacknowledged request is not automatically resent. That conservatively trades a
possible missed check-in for avoiding repeated contact. A process/network failure
between marker and request can also consume the checkpoint. Known pre-arm errors
release the contact reservation; uncertain marker writes retain it until
reconciliation. Cancellation after remote acceptance cannot retract the message.
There is no exact-time or exactly-once delivery claim. Reminder/timer retry
semantics are intentionally unchanged.

## Safe staged rollout (requires separate approval)

No deployment, production database access/export, live provider call, merge or
feature activation was performed by this implementation.

1. Verify the actual deployed commit, backend, schema, configured timezone and
   recipient ID. Run the final commit's CI and the offline tests below. Keep both
   flags false. Migrations are additive even with the feature disabled; inspect
   their readiness result and retain the migration/application logs
2. Before touching production, obtain approval for a specific backup/export and
   store it in an access-controlled private location, never the repository. Stop
   application writers for the backup/restore rehearsal when consistency requires
   it. Record the export time, source backend and an actual recoverable artifact
3. Restore to an isolated nonproduction database, never over the live database.
   Verify integrity, expected tables/row counts, notebook text, an existing task,
   and a migration/restart rehearsal. Configure no live Telegram/model credentials
   for the restored rehearsal. Restore private data only in its authorized scope
4. After explicit approval, enable `ENABLE_OUTCOMES=true` with checkpoint delivery
   still false. Restart, verify schema readiness, and try synthetic priorities
   in a supervised Telegram conversation. Check all six scenarios below. Existing
   historical goals are not imported; record only deliberate test/current choices
5. Review interpretation/tone, provider usage and latency before enabling delivery.
   The feature adds one bounded structured interpretation call on eligible text;
   ordinary replies may still need their existing generation call. Usage uses the
   existing provider accounting; aggregate interpretation latency is logged without
   chat contents. Do not claim zero additional model calls or cost
6. Only after separate approval, enable `ENABLE_OUTCOME_CHECKPOINTS=true` and
   restart. Activation creates a new durable delivery epoch and cancels any old
   pending agreements from a previous disabled epoch. Agree a **new**, synthetic
   checkpoint outside all quiet/cooldown windows and observe receipt/no-repeat

### Backup/export specifics

The existing `db.backup_database()` local SQLite branch uses SQLite's backup API.
Verify the returned file exists, has nonzero size, can be opened read-only, and
returns `ok` for `PRAGMA integrity_check`; restore a copy in the isolated rehearsal.
A copied WAL-mode main file alone is not proof of a consistent backup.

The existing Turso branch returns only `turso:cloud`. That placeholder is **not**
evidence of a recoverable backup. For an approved export using an already
configured official CLI, the documented SQL dump flow is:

```sh
# Run only after confirming the intended source and private output destination.
turso db shell <verified-database-name> .dump > <private-backup-path>.sql
# Rehearse in a NEW, explicitly approved nonproduction database, never live.
turso db create <new-rehearsal-name> --from-dump <private-backup-path>.sql
```

Check command exit status and the artifact, and verify the reconstructed schema
and data before treating it as a restore point. Turso also documents `turso db
export <database> --output-file <path>`, but warns that its generation snapshot can
omit the latest changes unless synchronized. Do not substitute an unverified
snapshot for the required current recovery point. Official references checked for
this runbook: [dump/load](https://docs.turso.tech/cli/db/shell),
[create from dump](https://docs.turso.tech/cli/db/create), and
[export caveat](https://docs.turso.tech/cli/db/export). No export was run here.

### Rollback and re-enable

Set both flags false and restart this feature-capable version **before** reverting
binaries. Let startup's `sync_delivery_state` finish and verify the persisted
`outcome_tracking_enabled=false` / `outcome_checkpoint_delivery_enabled=false`
state, superseded pending proposals, and cancelled/uncertain old checkpoints.
This observed transition increments the epoch/generation once, not every poll.
Then revert code if required. Preserve the additive tables and receipts; do not
DROP, truncate, purge or overwrite them. Existing alarms remain independent.

An ordinary enabled restart preserves unexpired, still-current checkpoints;
stale windows expire without catch-up. Re-enabling after an observed disable
never replays the old queue and requires new agreements. An old binary cannot
report an unobserved configuration transition: do not skip the disabled-start
step and assume that re-enabling can detect an offline rollback that never ran.

## Verification map

Run from the repository root with dependencies installed:

```sh
python -m ruff check .
python -m pytest tests
python -m compileall -q app scripts run.py
git diff --check
```

- `test_outcome_store.py`: SQLite and synthetic Turso transactions, migrations,
  partial failures/index readiness, provenance, quiet, ordering, source/concurrent
  CAS, commit-ack loss, stale/duplicate confirmations and backup/restore
- `test_outcome_checkpoints.py`: registered dispatcher behavior, shared contact
  limits, configured night hours/DST, final-send races, notebook suppression,
  cancellation after acceptance, uncertain sends, bounded retry/expiry and epochs
- `test_outcome_acceptance.py`: real text/command handler → SQLite → registered
  30-second scheduler job, six conversations, twelve acceptance contracts,
  natural-language mock responses, protected forwarded/quoted/negated/conditional
  cases, preview corrections, priority changes, timers and manual notebook edits
- `test_outcome_wire_backends.py`: actual installed libsql HTTP and Hrana fallback
  argument/result/batch encoders against synthetic SQLite transport, including
  bundle receipts, checkpoint delivery, stale rollback and activity deduplication
- Existing reminder/backend, notebook, action-honesty, presence, security,
  persistence and merged reply-style suites remain required

Six supervised conversations after approval: replace an old priority; distinguish
a hard application deadline from an opt-in check-in; record a blocker and smaller
step; distinguish timer completion from work; persist a quiet night and ignore an
unanswered checkpoint; reassess an overdue portfolio while choosing interview
practice. Automated model fixtures demonstrate safe application contracts, not
live interpretation quality or guaranteed tone. Production deployment, real Turso,
Telegram delivery and paid-model evaluation remain unverified until that trial.
