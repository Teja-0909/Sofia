# Background research, browser and specialist workers

Sofia remains the single conversational coordinator. Long research is saved as a
job and runs separately while Telegram keeps accepting conversation, timers and
priority controls. This is a bounded implementation, not an unrestricted agent
framework or a claim of improved live-model accuracy.

## Review and deployment state

This change builds on the confirmed-priority implementation merged in PR8
(main `8c4c6d55eee0764963304767dbc31e5f3c3b43fb`). Preserve that feature and the
merged direct/natural reply policy. Do not deploy an older branch over either
change. Both new feature flags default to `false`:

- `ENABLE_RESEARCH_JOBS`: admits and processes requested background research
- `ENABLE_RESEARCH_BROWSER`: allows the optional isolated public-page renderer

Code publication does not activate either flag, create credentials, configure the
VPS or authorize production database access. The additive research schema is
created during normal database initialization even while the feature is disabled.
Use the existing backup/export and restore-rehearsal prerequisites before an
approved production upgrade. The Turso backup placeholder is not a recovery point.

## User controls

- `/research <question or public URL>`: save and queue work; receive an R-number
- `Sofia, can you research ...?`, `investigate ...`, `look into ...`, or
  `read https://...`: equivalent narrow, direct private-chat requests
- `/research status`: recent saved jobs and current phase
- `/research status R1`: current state, delivery receipt and the saved answer
- `/research steer R1 <new direction>`: supersede unfinished work with a new
  revision; old workers cannot overwrite its result
- `/research cancel R1`: cancel before result delivery has started
- `/pause on`: pause research processing and result delivery along with existing
  background controls; ordinary conversation remains available
- `/pause off`: resume remaining eligible work, subject to its attempt budget

With research enabled, `/search` and `/read` also use the background queue.
Without it, their existing foreground behavior is unchanged. Plain conversation
does not receive a new planner call. Research status is an optional read-only
model tool; model output cannot create, steer or cancel jobs. Forwarded material,
page instructions, files and quoted research commands do not grant authority.
Telegram ingress is restricted to the configured user's private chat. The same
user speaking in a group cannot expose shared notebook/history or research there.
Research tool admission additionally requires caller-owned private-chat scope.

Acknowledgements name a saved job and read its current state. They do not promise
that a queued job has already started. Progress phases are available through
status without repeated unsolicited updates. A result or terminal failure is
sent once through Sofia's central bot; workers never become independent senders.

## Execution and recovery

- `research_conversation.py` owns direct requests, controls and deterministic
  receipts, before ordinary generation or the optional priority interpreter
- `research_store.py` owns additive SQLite/Turso tables, source-operation dedup,
  immutable operation receipts, admission limits, revisions and atomic leases
- `research_jobs.py` polls and creates at most two execution/delivery tasks; it
  never waits for the research pipeline inside the Telegram update handler
- `research_engine.py` runs a query planner, deterministic parallel searcher,
  evidence-based solver and separate verifier under one finite budget
- `research_evidence.py` validates queries, normalizes source identities and
  produces bounded provenance records
- `browser_client.py` calls the optional isolated browser service; it does not
  start Chromium in the Telegram process

Tasks do not acquire the ordinary generation tool lock. Background model calls
have a separate shared two-slot semaphore. Foreground chat therefore does not
wait for a research semaphore or research tool lock. Existing application and
provider/network latency still applies; this is not a guaranteed live-response
time. There is no recursive delegation and no state-changing worker toolset.

Leases, source IDs and revision comparisons fence duplicate workers and stale
results. A restart requeues eligible interrupted work after lease expiry; it does
not reset attempts. A lease is not proof a provider request was cancelled. A
request already dispatched can finish remotely even after local cancellation.

Result delivery has a durable send-start boundary. Cancellation/steering wins
before that boundary or receives an honest delivery-started response afterward.
Telegram has no idempotency key here: timeout, lost acknowledgement or process
failure after send-start becomes `uncertain` and is never automatically resent.
The saved answer remains readable through status. This avoids claiming exactly
once delivery. Known local pause/disabled preflights before any Telegram request
can safely return the result to pending delivery.

## Bounded resource use

Hard ceilings, optionally lowerable through environment configuration:

- Three active jobs, two running workers, ten admissions per UTC day
- A new request or steering revision consumes an admission; replay/coalescing
  does not. Up to four steering additions, within the cumulative text limit
- An original request is at most 2,000 characters; cumulative steering at most
  3,000 characters. No full conversation/notebook is copied to specialists
- At most two execution attempts per revision, including restart/pause recovery
- Each execution: 120 seconds, six queries, six page reads, two browser renders,
  eight physical model attempts, 60,000 conservative input-token reservations
  and 10,000 output-token reservations; each model request asks for at most 2,048
  output tokens
- Research uses the first configured provider/model only, with at most one
  transient retry per stage. It never silently upgrades to a costlier fallback

The input reservation uses UTF-8 bytes plus framing as a conservative estimate,
not a tokenizer measurement. Successful response usage includes Gemini thinking
tokens. Failed/abruptly interrupted calls may have incurred unknown usage and
retain their reservation. Recovery can repeat one execution: worst case per
revision is twice the per-execution ceiling, including 16 model attempts. The
daily admission limit is not a monetary billing cap or the provider's full-account
quota. Recheck the configured model's price and account limits before activation.
Other existing Sofia features retain their separate usage behavior.

## Evidence quality and boundaries

Search snippets are discovery leads. Conclusions cite fetched E-records with
source URL, retrieval time, method, truncation, hash and bounded passage text.
Publication dates remain unknown unless supplied as explicit source metadata;
retrieval time is never substituted. Source ranking prefers likely primary
domains and recent dated results, but neither ranking nor agreement proves truth.

The solver receives source passages before judging sufficiency. One bounded
follow-up round can fill gaps. The verifier independently checks the draft
against its cited evidence. Invalid schemas, fabricated citation IDs, unavailable
sources and unsupported action-success claims are rejected. Partial results say
what could not be established. This remains model-assisted consistency checking;
live factual quality needs a supervised evaluation set.

Webpages, browser observations, filenames and worker output remain untrusted.
No specialist receives desktop, shell, account, timer, priority, message-sending
or recursive-agent tools. Research completion never marks a user's outcome done.
The existing public HTTP reader retains DNS pinning, per-redirect checks and size
limits. Old labels claiming a complete webpage were corrected to partial HTTP
extraction. DDGS is still the search provider; no paid search account is created.

## Optional browser

See [browser-worker.md](browser-worker.md) for the separate container, public-only
egress proxy, Chromium sandbox and setup requirements. It is signed out, has no
user browser profile and supports bounded public page rendering only. It cannot
log in, submit forms, purchase items, download files or run model-supplied scripts.
Page JavaScript can execute only inside the isolated rendering environment.
Public GET requests can still have remote effects; this is not a universal
guarantee that arbitrary websites are side-effect-free.

Do not expose CDP, copy the bot's environment or disable sandbox/network controls.
A real sandbox-enabled Chromium launch in the development environment failed at
`socket()` with `Operation not permitted`. No unsafe fallback was used. Offline
tests cover policy, proxy/client handling and mocked rendering contracts; actual
Chromium rendering and Docker firewall behavior must pass on an approved host
before enabling the browser. The browser is not production-validated by these
tests.

## Staged acceptance and rollout

1. Review the stacked code and run `ruff check .`, `PYTHONPATH=. pytest tests`,
   `python -m compileall -q app browser_worker scripts tests run.py`, and
   `git diff --check`. Tests use temporary state and mocked providers/Telegram;
   they do not establish production delivery or live-model quality
2. Independently authorize the deployment target, verified database export and
   restore rehearsal. Keep all new flags off for the initial deployment and
   verify readiness, existing timers, priorities and quiet controls
3. For an approved limited trial, enable jobs only with lower limits. Submit a
   public question, chat during it, create a timer, inspect progress, steer and
   cancel. Check one completion, one partial result, one failure and one restart
4. Browser setup additionally needs user-approved host access, a scoped service
   credential entered through a secure channel, TLS/reverse-proxy configuration
   and the host's sandbox/firewall checks. Those are separate access/security
   changes and are not performed by this code change
5. Only after signed-out rendering and adversarial network tests pass, enable
   the browser flag for a controlled public JavaScript page. Verify real source
   receipts, cleanup, cancellation and resource limits before broader use

To roll back activity, pause first and disable `ENABLE_RESEARCH_JOBS` and
`ENABLE_RESEARCH_BROWSER`, then restart the application. Preserve additive tables
and saved results; do not delete or reset delivery/admission records. Disabling
does not prove an already dispatched provider or Telegram request was undone.
Review queued jobs before re-enabling. No automatic historical task import,
backfill of research requests or account login is included.
