# Grounding timer, clock and action claims

This change targets a concrete failure mode: Sofia suggested a break, then spoke
as though a timer existed despite no saved timer record. Conversation turns and
previous assistant promises cannot serve as action receipts.

## Supported flow

- `set a 20 minute timer`, `set 20min timer`, `start a timer for 20 minutes`,
  and `timer 20min` use an anchored direct-user grammar, with optional polite
  addressing. Supported durations are whole minutes/hours, from 1 minute to
  7 days. Missing, fractional, compound or unsupported durations ask for a
  supported expression rather than inventing one.
- `I'm taking a 20-minute break`, an offered timer, quoted text, code examples
  and hypothetical requests do not create a timer.
- A timer is a task with an explicit `kind='timer'`. Each request creates a
  distinct ID, even for an identical duration. The typed confirmation is built
  only after reading the saved row. A failed/uncertain write or readback says
  verification failed and advises checking `/tasks` before retrying.
- Timer alerts use the existing durable delivery lease and retry path. They
  finish after one acknowledged alert; ordinary reminders keep their existing
  repeat policy. `/cancel <id>`, `/snooze <id> <minutes>`, `/done <id>` and pause
  controls continue to work. Ambiguous conversational changes such as “cancel
  the timer” make no changes and direct the user to the explicit ID control.
- Standalone “how much time is left on my timer?” and timer-status questions
  read current rows and delivery claims. They distinguish missing/unreadable,
  future due, overdue but unconfirmed, paused, retrying, cancelled, marked done
  without a delivery receipt, and an acknowledged alert. They never infer a
  running timer from conversation history. Up to the latest 10 are shown;
  an ID can be appended to a status question.

### Conversational command envelopes

Timer commands accept composable greetings, acknowledgements and polite lead-ins,
with ordinary whitespace and punctuation: for example, `ok , set a timer for 2
minutes`, `Hey, Sofia, sure; could you please, set a timer for 2 minutes?`, and
`All right. Also, set a timer for 2 minutes, thanks!`. Unicode compatibility
normalization handles full-width punctuation/digits. This is an anchored grammar:
it consumes only approved lead-in particles and a trailing courtesy. It never
searches arbitrary prose for a command. Quotes, logs, reported speech, negations,
conditional permission and hypothetical frames do not authorize a save. Unknown
wording may still require a clearer direct command.

A standalone timer-capability question gets an accurate deterministic answer:
chat timers are supported and require a saved-ID confirmation. Generated blanket
claims that timers cannot be set through chat are corrected separately from
unsupported action claims. Valid duration/device limitations and hypothetical
statements are preserved. The model policy now describes the same chat workflow
as the deterministic handler.

The action guard also covers implicit countdown assertions such as “Two minutes
starting now”, and timer-context claims of “keeping track” without a verified
result. Quotations, ordinary discussion and conditional offers remain supported.
The exact acknowledgement-prefixed two-minute request is tested through the real
text handler, durable task store, due poller and delivery receipt. Independent
QA also exercised the registered scheduler callback and Telegram send boundary
with a mocked Telegram transport, confirming no early send and no later repeat.

## Clock and history

Standalone current-time/date questions return the host clock directly without
an LLM. Other generation calls, including specialist, refinement and the final
tool-loop fallback, receive a fresh UTC/local/IANA-zone snapshot immediately
before the call. A bad configured zone is disclosed as unknown and uses UTC,
instead of silently falling back to India. The configured zone does not prove
where the user is; the host clock is not an independent check of NTP accuracy.

Chat history retains recording timestamps and explicitly labels old assistant
claims as historical, not receipts or current observations. Existing saved
context remains dated lower-trust evidence. Notebook entries, generated
summaries and past guesses do not become facts merely because they were saved.

## Generated-action boundary

Normal model generation remains read-only. No new model tools are introduced.
Explicit timer/reminder/control handlers produce confirmations from their own
operation results. The narrow bare-`done` handler now checks the mutation result
instead of announcing completion after a failed update.

A shared, bounded English-language guard catches common affirmative
saved/scheduled/running/sent/completed claims and unconditional later-ping
promises. It runs after the final model/specialist/refinement/tool-limit output,
at normal message delivery, image captions and optional background text.
Unsupported foreground claims receive an honest verification fallback;
unsupported unsolicited messages remain silent. It replaces the draft as a
whole, rather than deleting words and changing the meaning of remaining prose.
Balanced quoted claims, code examples, negations, hypothetical offers and normal
user-progress acknowledgements are preserved by regression cases. It adds no
LLM calls.

This is a targeted backstop, not a complete semantic verifier. Unusual wording,
other languages, mixed factual claims and open-ended answers can still be
wrong. Some ambiguous action-like prose may fall back conservatively. There is
no claim that every hallucination is eliminated, and no live-model evaluation
was performed. Model output and saved text never grant an action permission.

## Migration and delivery limits

The additive `tasks.kind TEXT NOT NULL DEFAULT 'reminder'` migration preserves
existing task rows and their old reminder semantics. Local SQLite and the Turso
bootstrap use the same inspected-column migration; readiness validates the new
column before startup. No production database or deployment is part of this PR.

The scheduler polls about every 30 seconds while Sofia is online. Timers are not
exact-second alarms. Paused/offline service, provider/network outages and retry
backoff can delay delivery. Telegram acceptance followed by a lost response or
process crash can still duplicate an alert on recovery; this PR does not claim
exactly-once delivery across that boundary. A status read describes the records
at its check time and cannot guarantee a running process or future delivery.

## Verification

The offline suite exercises direct parser/save/readback/receipt flows, the
multi-turn suggestion → break → continued chat incident, failed writes,
unreadable status, current/stale/cancelled/completed/paused timers, delivery
claims, one-shot versus ordinary reminder repeats, clock freshness/invalid
zones, prompt-history timestamps, final generation exits and quote/conditional
preservation. Existing accountability, presence, cancellation/snooze, persistent
context and backend tests remain required. All provider/Telegram boundaries use
mocks or local fixtures; no live provider calls or private database access.
