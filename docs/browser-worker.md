# Sofia isolated public browser

Status: implementation and offline tests, **disabled by default**. This is deploy
scaffolding, not a deployed or production-verified service. No VPS, Render account,
new credential, production database, or paid API was accessed to build it.

## What it does

Only the central coordinator calls `app.browser_client.read_page(url, max_chars)`.
The API is `POST /read` with JSON containing exactly `url` and `max_chars` (1–20000).
It returns the observed final `url`, bounded `title` and `text`, `truncated`,
`method: "isolated_chromium"`, and a timezone-aware `observed_at`. Errors and disabled
configuration return `None` to Sofia; no fabricated evidence is returned.

This is a signed-out browser with JavaScript rendering. It can load GET-backed
public pages; it cannot click, type, fill forms, log in, upload, run a supplied
script, use a saved profile, or accept model-controlled browser actions. The only
`evaluate` call is a fixed bounded text-extraction program embedded in the source.
No CDP, Playwright server, browser websocket endpoint, screenshots, filesystem API,
or arbitrary navigation command is exposed.

`GET /healthz` reports readiness after the startup checks. It is not a proxy,
browser-control endpoint, or an end-to-end reachability guarantee.

## Boundary and threat model

1. The worker image is built from the **browser_worker directory only**. No Sofia
   code, `.env`, Telegram/LLM/DB credentials, personal profile, host filesystem, or
   Docker socket is mounted into it. The controller receives only a separately
   approved scoped API bearer token. Chromium receives an explicit minimal
   environment without that token; no authentication/storage state is imported.
2. The browser is non-root, uses `chromium_sandbox=True`, and has no effective or
   bounding capabilities. A root entrypoint briefly sets rules in the container's
   private network namespace, writes a root-owned marker, then permanently drops
   capabilities and switches to uid 10001. Never run this entrypoint with host
   networking. Missing firewall tools/rules, capabilities, marker, proxy, or
   Chromium sandbox cause startup to fail. There is no `--no-sandbox` fallback.
3. Docker gives the browser an internal-only network. Its namespace firewall
   permits new outbound TCP only to **172.30.91.2:3128**, the safe egress proxy.
   Other TCP, UDP, direct DNS, loopback TCP, QUIC/WebRTC bypasses, host gateway,
   metadata, and direct Internet connections are denied. IPv6 is disabled and
   separately default-denied. Established replies to the control API are allowed.
4. The proxy is a separate non-root, capability-free service with external
   connectivity and no credentials. It accepts ordinary HTTP GET/HEAD or CONNECT
   to HTTPS port 443. Every destination is validated, all DNS answers must be
   public, and the outbound socket uses a single validated numeric IPv4 address.
   No hostname is resolved again when connecting. Mixed public/private answers,
   local/metadata/reserved IPs, credentials in URLs, non-standard ports, protocol
   upgrades, request bodies, and ambiguous headers fail closed. IPv6-only sites
   are deliberately unsupported. The proxy port is never published.
5. Top-level redirects are followed explicitly, at most five hops, through that
   same proxy. Every hop is checked before fetching. The successful text response
   is rendered at its actual final URL with an additional response CSP that
   forbids forms, frames, objects, workers, and base-URL changes. HTTP attachment
   responses, non-text main documents, authentication challenges, and excessive
   document bodies are rejected. There are no browser-driven document navigations
   after this initial document; extra pages are closed.
6. JavaScript GET/XHR/fetch, scripts, CSS, fonts, and images can load. Request
   routing rejects other methods, non-HTTP(S) URLs, authentication headers and
   other resource types. Service workers, WebSockets, permissions, and downloads
   are separately blocked. The network proxy and firewall remain the SSRF
   boundary for redirects and subresources even when a request is not visible to
   Playwright routing. HTTPS stays encrypted and certificate validation stays on.
7. Each read gets a fresh browser and context, disposed on success, error,
   timeout, or cancellation. Concurrency is one read per worker; excess requests
   get 429. Browser time is capped at 25 seconds, request count at 64, each proxy
   connection at 28 seconds/4 MB combined transfer, and the rendered document at
   2 MB after fetching. Docker also caps memory, CPU, processes and temporary disk.

These controls do **not** make the public Web side-effect-free: GETs and website
JavaScript can record analytics or cause poorly designed services to change state.
Page content and titles are untrusted evidence, never commands or permission. A
page can lie. This is not protection against all browser/kernel vulnerabilities,
a compromised operator, a compromised proxy, or malicious package supply chains.
A full security review and host-level smoke test remain required before production.

Response bodies can decompress before the application size check, so the container
memory/time limits are a necessary second boundary. CSP, blocked POST APIs,
frame/worker restrictions, blocked new document navigation, small limits, and the
1.5-second render window will break some sites. Login-only, CAPTCHA-protected,
IPv6-only, streaming, and interaction-dependent content is intentionally unsupported.

## Configuration in Sofia

The coordinator's configuration must define:

```dotenv
ENABLE_RESEARCH_BROWSER=false
BROWSER_WORKER_URL=
BROWSER_WORKER_TOKEN=
BROWSER_WORKER_TIMEOUT_SECONDS=30
```

No worker dependencies are added to the bot's requirements. The worker dependencies
are in `browser_worker/requirements.txt`. Sofia already uses `httpx`. Only enable
the flag after deployment and verification. Use a loopback service origin for a
same-host coordinator, or HTTPS for a remote service. The client never follows
service redirects and never uses environment proxies, to avoid forwarding its
bearer token elsewhere. Endpoint path/query/userinfo are rejected.

Token creation and configuration are intentionally deferred: an operator must
first obtain the user's approval for this new persistent access, then create a
scoped token of at least 32 printable ASCII characters and store it securely.
Never reuse Telegram, LLM, database, SSH, or cloud-provider credentials. Do not put
real values in this document, commits, shell history, logs, or model prompts.

## Contabo / dedicated Linux VPS: approval-gated deployment checklist

1. Confirm the intended VPS, billing/plan, user access, scope, and permission to
   install Docker/Compose, configure this container networking, configure the
   token, and (if needed) expose a TLS gateway. This document authorizes none of
   those actions. Do not log into the VPS or change host security settings merely
   because this scaffold exists.
2. On the approved Linux host, verify a supported Docker Engine/Compose and
   Chromium user-namespace sandbox support. Review the pinned image/package
   versions for security updates; resolve/pin image digests before production.
   The current image and Playwright package are both 1.63.0. Review the checked-in
   upstream seccomp profile; never replace it with `unconfined` or add SYS_ADMIN.
3. Ensure `172.30.91.0/24` does not overlap existing networks. A subnet change must
   update the Compose addresses, `policy.py`, and entrypoint rules together and
   be retested. Keep the browser separate from the bot and all database networks.
4. Configure only the approved worker token in a protected deployment environment.
   After review, build and start using `docker compose -f docker-compose.browser.yml
   up --build`. No commands in this checklist have been run against a VPS.
5. Check startup logs without secrets, uid/capability state, and the readiness
   endpoint through host loopback port 8088. If startup fails, diagnose the host
   capability/seccomp configuration with the operator. Do not remove sandbox,
   firewall, or marker checks to make it start. The proxy starts first but does not
   have a readiness gate; if the worker starts too early, restart it after the
   proxy is ready. Automatic restart is intentionally off during validation.
6. Run the acceptance checks below in an isolated staging environment. Verify
   Docker's published-port behavior on the selected host and ensure 8088 is not
   externally reachable. Only then point a same-host Sofia client at
   `http://127.0.0.1:8088` and enable its browser flag.
7. If Sofia is on another host, provide an **approved authenticated TLS reverse
   proxy** to this loopback API and restrict ingress/rate-limit at that gateway.
   Keep proxy port 3128 and all CDP/browser ports unpublished. Configure the HTTPS
   origin and the separately scoped token in Sofia. Do not forward bot secrets.

No persistent volume is needed. To roll back, disable the browser flag first,
then stop this Compose project; Sofia should use existing public-page/search
fallbacks or report that browser evidence is unavailable.

## Optional Render arrangement

The safe default is a coordinator on Render calling the dedicated, approved HTTPS
reader above. Render supports Docker-based services and private services, but
those capabilities alone do not establish support for this custom namespace
firewall, capability drop, seccomp profile, or two-network egress boundary.

A **separate browser service on Render is not supplied as a deployable blueprint**.
First obtain platform confirmation that equivalent mandatory isolation can be
implemented and verified, plus user approval for service cost/access. If the
platform cannot enforce it, keep the isolated worker on the VPS or another
approved platform that can. Do not deploy the worker image with unrestricted
network egress, a fake marker, no sandbox, or just Playwright URL interception.

References: [Render Docker](https://render.com/docs/docker),
[Render private services](https://render.com/docs/private-services),
[Docker service controls](https://docs.docker.com/reference/compose-file/services/).

## Verification and remaining gates

Offline tests: `python -m pytest tests/test_browser_worker.py -q`. They cover URL
and mixed DNS rejection, numeric-IP pinning, rebinding between connections,
redirect validation/provenance, subresource method checks, proxy smuggling and byte
limits, browser context cleanup, service authorization, disabled-default behavior,
strict response validation, endpoint/token safety and bounded timeouts.

On this execution host, all 82 browser tests passed. Installed Chromium was attempted
with its sandbox enabled for synthetic QA, but launch failed with `socket() failed:
Operation not permitted` before a page could render. No sandbox weakening,
dependency installation, real website visit, container build, or VPS deployment
was performed. Offline tests are not proof that Chromium/Docker isolation works.

Required staging acceptance before enabling:

- Load a local synthetic public-IP/proxy fixture whose text is created by JS;
  verify the rendered text, final redirect URL, truncation, and timestamp
- Confirm GET and POST form submission, popups, frames, service workers,
  WebSockets, downloads and private-address subresources cannot leave the worker
- Verify a mixed/rebinding DNS fixture cannot reach a private IP, including after
  redirects and from CSS, images, JS and XHR; inspect actual outgoing destinations
- From the browser namespace, attempt direct public/private TCP, loopback TCP,
  metadata, DNS, UDP and IPv6 bypasses; all must fail except the safe proxy socket
- Stop the proxy and verify reads fail closed; break sandbox setup and verify the
  service never becomes ready; never use real metadata services in this test
- Confirm isolation of cookies/storage across requests, cleanup after timeout,
  absence of inherited bot/LLM/DB/token credentials in Chromium, one-job admission,
  and container resource behavior for huge/compressed responses

Use explicit test doubles/fixture transports for public-IP tests. Do not add a
production switch to disable SSRF checks or network isolation for testing.

Implementation references: [Playwright browser launch options](https://playwright.dev/python/docs/api/class-browsertype),
[network routing and service workers](https://playwright.dev/python/docs/network),
[route fetch](https://playwright.dev/python/docs/api/class-route#route-fetch),
[Docker and sandbox guidance](https://playwright.dev/python/docs/docker).

`browser_worker/seccomp_profile.json` is derived from the Microsoft Playwright v1.63.0
profile fetched from its official repository. The unmodified upstream SHA-256 is
`cc3e61cabda6bbc1e53e54d27ba4d55a9d3be829b6dd1a596f4a7b31b1cc7849`.
Source: https://github.com/microsoft/playwright/blob/v1.63.0/utils/docker/seccomp_profile.json
The local profile adds one explicit `clone3` deny rule returning ENOSYS (38),
so modern glibc can fall back to `clone`; it does not allow `clone3`. This follows
the compatibility approach in [Moby's default seccomp profile](https://github.com/moby/profiles/blob/main/seccomp/default.json).
The upstream Apache-2.0 license is included as `browser_worker/LICENSE.playwright`.

Release pins verified at [Playwright 1.63.0 on PyPI](https://pypi.org/project/playwright/1.63.0/),
[aiohttp 3.13.5 on PyPI](https://pypi.org/project/aiohttp/3.13.5/), and the official
Playwright Docker documentation linked above. No packages/images were installed.
