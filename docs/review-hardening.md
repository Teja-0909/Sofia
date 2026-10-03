# Review hardening and operating controls

This change set fixes the review of `269b132d323e881bba15e76ac1a39b217497e1f2`.
It is a code change for review, not a deployment or permission change.

## Runtime changes

- The Telegram entry point exports the real application builder. Startup and all background senders use one shared bot instance. Startup failures and shutdown close the server/database and reset readiness.
- `/health` is liveness. `/ready` becomes ready only after database initialization, Telegram polling and scheduler startup, and checks those dependencies on each request.
- `/image` imports its private handler explicitly; successful consciousness calls unpack the `(text, tool_calls)` result; welcome-back logs use the valid `text` channel.
- Text, code indentation, arithmetic and Markdown are preserved. Telegram output is chunked below the message limit. `/traces` uses the current setting and exposes only concise pipeline diagnostics.
- Normal responses use a direct generation call. `ENABLE_SPECIALISTS=true` optionally adds one selected reviewer and synthesis, with conversation and retrieved evidence supplied. Tool turns and placeholder retries still add calls; this is not a latency benchmark.
- Task/focus context is included in prompt assembly. Webpages, filenames, application events and window titles enter as untrusted data, not privileged instructions.

## Desktop security and migration

Upgrade the server and Windows sidecar together. Old sidecars do not implement the acknowledgement/deadline protocol and must not be used with these endpoints.

1. Set the same strong `WEB_AUTH_TOKEN` in server and sidecar configuration. With it blank, desktop API routes return 503; public liveness/readiness remains available.
2. Set `SOFIA_BASE_URL` explicitly on the sidecar to the server's HTTPS URL. There is no baked-in remote destination.
3. Enable only the capabilities needed, on both server and sidecar:
   - `DESKTOP_ALLOW_SCREEN_CAPTURE`
   - `DESKTOP_ALLOW_CLIPBOARD_READ`
   - `DESKTOP_ALLOW_CLIPBOARD_WRITE` plus `DESKTOP_ALLOW_MUTATIONS` for clipboard writes
   - `DESKTOP_ALLOW_MUTATIONS` for drawing overlays
   - `DESKTOP_WORKSPACE_ROOT` for read-only structured workspace status
4. `DESKTOP_PAUSED=true` or the existence of `DESKTOP_PAUSE_FILE` is a local stop switch. A Telegram resume cannot override that local switch.
5. The local overlay uses its own `SOFIA_OVERLAY_TOKEN`, or a stable, scoped HMAC token derived by the sidecar. Never reuse the cloud API token for overlay IPC.

Raw shell execution is removed from model tools and is denied even if mutations are enabled. Structured operations validate fields, ranges, workspace confinement and permission flags outside the model. General chat/research exposes read-only tools; proactive turns expose no tools. Explicit Telegram controls remain available subject to local policy. Model-generated action tags no longer mutate tasks, memory, mood, focus, sleep or images. Use direct reminder requests or the explicit commands below.

Commands have non-colliding IDs, bounded queues and deadlines. The sidecar must acknowledge a still-live command immediately before execution. Cancellation and timeout remove unclaimed work. Already acknowledged work can have an unknown outcome and is never automatically retried as if it were known not to have run. Screen uploads are paired to an acknowledged capture ID, and the actual image is delivered to the next model turn.

HTTP handling enforces bounded complete headers and bodies, authentication before body reads, timeouts, request/media limits and strict framing. Overlay IPC is authenticated and rechecks expiry before rendering. Public-page fetching validates every DNS result and redirect, pins the connection to the validated IP with the original Host/TLS name, disables implicit proxies, and caps size and time. The unsafe direct fallback and implicit headless-reader route are removed; JavaScript-only sites may no longer yield content.

## Reminders and memory

Delivery now has an expiring atomic claim, retry/backoff state and a durable successful-send receipt. Missing bot, model failure, Telegram failure and expired claims do not permanently strand the item. Reminder text has a deterministic fallback. Counters advance only after accepted delivery. Scheduled sends are one bounded text message; auxiliary model/image effects cannot sit inside the retry boundary.

There is no Telegram idempotency key. Remote acceptance followed by a lost response or a process crash before recording the receipt can still duplicate one message on recovery. This is an explicit at-least-once limitation, not an exactly-once guarantee.

The editable notebook remains in Turso's `app_config` row with key `memory_md_content`. Before rebuilding it, Sofia reconciles its current content with the last published notebook: manually added facts are preserved, replacements update the facts, and removed facts are suppressed instead of silently reappearing. Formatting is normalized into canonical sections. Only an identical normalized fact is reinforced; similar but distinct facts are retained. Explicit deletion/correction adds a durable suppression record, deactivates matching old facts, and invalidates caches. Previously suppressed matching facts are not restored by pasting them back into the notebook. The same suppression is applied at retrieval to historical context. Historical logs/audit data are retained; this is not complete erasure of every semantic paraphrase or a data-export/deletion service.

On first upgrade, existing DB-notebook content is preserved in canonical memory before any rewrite. A disk-only `memory.md` is a fallback only before migration and only when there is no prior suppression/deactivation; after migration the disk file is derived and cannot resurrect deleted facts. Notebook reconciliation uses a durable baseline and compare-and-swap transaction: concurrent edits cause a retry rather than overwrite. A failed initial preservation stops startup before Telegram or scheduled jobs run.

After editing `memory_md_content` in Turso, run `/memory` to check synchronization. Edit that value, not the internal baseline/migration keys or canonical table rows. Matching is deterministic, not semantic: if deleting a short fact would also suppress an overlapping replacement or retained longer fact, synchronization stops and leaves the edited source unchanged. Rephrase the conflicting replacement so it does not contain the removed fact verbatim, or restore the prior text and make an unambiguous edit. Unclosed Markdown fences also stop synchronization instead of discarding text. These unresolved edits can prevent memory-backed replies or startup until corrected; they are not silently accepted.

The Hrana fallback decodes integers, floats, nulls, text and blobs to the same Python types as the regular backend. Schema additions are additive and initialize for new and existing databases. Initialization checks every application table and column against the shipped schema before the service can become ready. Back up the full database and notebook before any production upgrade; the app's Turso backup helper does not itself create an export or verify a recovery point.

## Telegram controls

- `/tasks`: task status, including completed, cancelled and missed items
- `/add <description> at <time>` or a direct `remind me ...` request
- `/done <id>`: complete a pending/missed task
- `/cancel <id>`: cancel future reminders for the task
- `/snooze <id> <minutes>`: 1 minute to 7 days
- `/memory`: inspect active memory IDs and the notebook
- `/forget <id>`: suppress an active fact in future memory context
- `/correct <id> <replacement>`: replace a specific canonical fact
- `/pause on` / `/pause off`: pause/resume background messages and desktop activity; regular chat still works
- `/permissions`: read current permission/paused status; model text cannot change it
- `/screen`, `/watch`, `/overlay`: explicit desktop controls with local permission checks

### Reminder creation and delivery follow-up

Reminders support both `/add 22:13 drink water` and conversational requests such as `Sofia, can you remind me at 22:10 to drink water?`. A successful request is confirmed from the saved task row, with its ID and dated local time. A bare clock time that has already passed means its next occurrence; check the displayed date. Invalid or missing times are rejected rather than silently stored as an undated note or moved four hours ahead.

Supported deterministic forms are local clock times, today/tomorrow, dayparts, minute/hour durations and explicit daily recurrence. Other explicit dates, time zones or recurrence patterns are conservatively rejected instead of silently interpreted as today's local time. If saving cannot be confirmed, check `/tasks` before retrying because the database may have accepted the write.

Repeating a pending reminder description with a different time reschedules that task and resets its reminder count; submitting the same saved time is idempotent. Timed reminders and saved proactive messages use their stored text directly: they never wait for model wording. The scheduler checks for due work every 30 seconds, so normal polling can add up to about 30 seconds, plus database/Telegram latency. Paused messaging, service suspension, network failure or Telegram rejection can still prevent timely delivery; successful chat alone does not validate the scheduler.

This follow-up changes server scheduling only, with no new schema, environment variables or sidecar protocol changes. The earlier paired server/sidecar upgrade instructions still apply when first enabling the desktop features.

No web dashboard, new OS sandbox, arbitrary-command approval UI, production secrets, permission grants, merge or deployment is included. Full semantic memory-erasure workflows, real Windows desktop validation, live Telegram/Turso/model-provider integration and production performance/quality evaluations remain future validation work.

## Desktop presence and safe logging follow-up

Foreground presence is metadata about one focused window, not a screenshot, open-app/tab inventory, verified page contents or proof of physical presence. Native Windows calls now declare pointer-width-correct signatures; missing/failed capture is reported as unavailable instead of fabricated `Desktop`. Idle-read failures remain unknown. Observations commit atomically, replace stale content even when capture fails, and are usable for only 60 seconds. Common standalone PC questions return a fixed, timestamped observation; broader model replies and background consumers receive bounded, untrusted evidence and explicit limitations.

The sidecar now verifies JSON acknowledgements and reports status-only transport failures/recoveries instead of silently swallowing them. A successful startup log is not a successful connection or presence upload. Presence can work with all `DESKTOP_ALLOW_*` flags false; screenshot, clipboard and mutation permissions stay separate. `DESKTOP_PAUSED=true`, a pause file or the server background pause still stops presence.

Update the full server and Windows checkout together, preserving private configuration; no new environment variables, packages or schema migration are required. After the server deployment is Live and the updated sidecar has been restarted, run from the repository root:

```powershell
python scripts/sidecar.py --diagnose
```

This exits after local availability checks and one authenticated GET `/api/desktop/status`. It does not post window content, poll/drain commands, acknowledge work, capture a screenshot or launch the overlay. Output contains only fixed status labels, booleans and observation age, so it can be shared for troubleshooting. It separates local capture availability, transport/authentication, server readiness/pause and stored-sample freshness. A successful status probe does not prove a presence POST has succeeded; check for `presence_state: fresh` after the normal sidecar runs. HTTP 401 means authentication was rejected; 503 can mean missing server auth or storage/service failure; 404 can mean the server predates this endpoint. Keep the matching token private. Do not use `/api/desktop/poll` as a read-only probe because it removes queued commands.

Server logging now redacts configured credentials and common credential-bearing URLs/headers in formatted messages and tracebacks. The sidecar also uses status-only diagnostics. This only protects new application log output; it does not remove old logs, revoke exposed credentials or guarantee redaction in external hosting logs. Rotate any exposed token through its provider and replace it privately before continuing. Code that adds a custom logging handler later must install the same redaction protection.

Real Windows capture and live Render/Telegram/Turso behavior still need the user's rollout check. Mock native APIs cover 64-bit handle preservation and failures; temporary databases, mocked HTTP and adversarial model responses cover server and grounding contracts.

## Offline validation

Run from the repository root:

```sh
python -m pip install -r requirements.txt pytest ruff
ruff check .
PYTHONPATH=. pytest tests
python -m compileall -q app scripts run.py tests
```

Tests use temporary databases/notebooks, fake sidecars, mocked model/network responses and local HTTP servers. They clear provider credentials and disallow live network lookups. The repository has no configured static type-checking gate; compilation is a syntax check, not a substitute for type checking. CI uses Python 3.11; local validation used Python 3.12. No live desktop action or production configuration change is part of this verification.
