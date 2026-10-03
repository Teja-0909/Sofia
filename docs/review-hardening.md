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

Canonical active facts drive the notebook. Only an identical normalized fact is reinforced; similar but distinct facts are retained. Explicit deletion/correction adds a durable suppression record, deactivates matching old facts, invalidates caches and reconstructs derived notebook content. The same suppression is applied at retrieval to historical context. Historical logs/audit data are retained; this is not complete erasure of every semantic paraphrase or a data-export/deletion service.

The Hrana fallback decodes integers, floats, nulls, text and blobs to the same Python types as the regular backend. Schema additions are additive and initialize for new and existing databases. Back up the database before any production upgrade.

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

No web dashboard, new OS sandbox, arbitrary-command approval UI, production secrets, permission grants, merge or deployment is included. Full semantic memory-erasure workflows, real Windows desktop validation, live Telegram/Turso/model-provider integration and production performance/quality evaluations remain future validation work.

## Offline validation

Run from the repository root:

```sh
python -m pip install -r requirements.txt pytest ruff
ruff check .
PYTHONPATH=. pytest tests
python -m compileall -q app scripts run.py tests
```

Tests use temporary databases/notebooks, fake sidecars, mocked model/network responses and local HTTP servers. They clear provider credentials and disallow live network lookups. The repository has no configured static type-checking gate; compilation is a syntax check, not a substitute for type checking. CI uses Python 3.11; local validation used Python 3.12. No live desktop action or production configuration change is part of this verification.
