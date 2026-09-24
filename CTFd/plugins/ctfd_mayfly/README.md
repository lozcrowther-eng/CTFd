# ctfd_mayfly

A CTFd 3.7+ challenge type whose instances are ephemeral Vercel Sandboxes provisioned by the
mayfly orchestrator (`../` in this repo), with a fresh per-team dynamic flag rather than one
static string every team submits the same answer to.

## Install

Symlink (or copy) this directory into a real CTFd checkout's plugins folder:

```
ln -s /path/to/mayfly/ctfd_mayfly <ctfd-checkout>/CTFd/plugins/ctfd_mayfly
```

CTFd's plugin loader (`CTFd/plugins/__init__.py`'s `init_plugins()`) discovers and imports it
automatically at startup — nothing else to register. Requires `SAFE_MODE` off (CTFd's
default) so plugins actually load.

## Configuration

Two required environment variables on the CTFd process:

- `INSTANCER_URL` — the orchestrator's base URL (e.g. `https://mayfly-nine.vercel.app`).
- `INSTANCER_SECRET` — shared with the orchestrator's own `MAYFLY_SIGNING_SECRET`. Used two
  ways: HMAC-signs this plugin's outbound `launch()` call (client.py), and is compared
  (constant-time) against the `X-Mayfly-Internal-Secret` header on inbound calls from the
  orchestrator (api.py's internal blueprint).

Both are resolved lazily (first real use, not at import time) — importing this plugin never
requires them to already be set.

## Verified

Booted against a real local CTFd 3.7.7 install (SQLite) and driven through its actual HTTP
routes — registration, login, the real `/api/v1/challenges/attempt` flag-submission API, and
a stub matching the orchestrator's real signed contract. All of the following passed:

- Challenge type registers, its three tables (`mayfly_challenges`, `mayfly_instances`,
  `mayfly_flag_share_events`) get created, all routes register.
- `launch()` signs the raw request body exactly per `../fixtures/hmac-vectors.json`'s four
  vectors, returns immediately without state/url (never blocks on provisioning), and is
  idempotent (a second launch for the same owner+challenge returns the same live instance).
- The internal API rejects a wrong shared secret (403) and accepts the right one; `PATCH
  .../instances/<run_id>` and `POST .../instances/<run_id>/reaped` both correctly update
  CTFd's own copy of instance state.
- `attempt()` validates a submission against the per-team `flag_hash` via
  `hmac.compare_digest`, entirely ignoring the static Flags table — a correct flag from one
  team is rejected for another team, and submitting a flag that matches a *different* team's
  instance is reported as "That flag was issued to another team" and logged as a
  `MayflyFlagShareEvent` for admins to see.
- `solve()` calls the orchestrator's `stop()` endpoint (fire-and-forget — the actual reap
  confirmation arrives later via the internal `/reaped` callback, matching the orchestrator's
  own async design).

## A real integration gap this surfaced — needs a decision, not a workaround

`MayflyChallengeModel` carries `image`/`port`/`ttl_seconds`/`vcpus`/`start_command` per
challenge (admin-configured in CTFd), but `client.py`'s `launch()` only ever sends
`{challengeId, teamId, ttlSeconds}` — because that's *all* the orchestrator's current
`LaunchRequestSchema` (`lib/api/schemas.ts`) accepts. The orchestrator resolves `challengeId`
against its own static `lib/fixtures/challenges.ts` array by exact string match; it has no way
today to accept an image/port/vcpus/start_command directly in the launch request.

That means: right now, a challenge configured in CTFd only actually provisions the *correct*
sandbox if its CTFd `challenge_id` happens to coincide with a `challengeId` string the
orchestrator's fixture list already knows about — the image/port/vcpus/start_command fields
this plugin lets an admin set are otherwise inert. Two ways to close this, not done here since
it's a change to the orchestrator, not this plugin:

1. Extend `LaunchRequestSchema` to accept `image`/`port`/`vcpus`/`startCommand` directly and
   have `RealSandboxClient.create()` use them instead of (or as a fallback from) its own
   `lib/fixtures/challenges.ts` lookup.
2. Keep the fixture list as the source of truth and require whoever configures a CTFd
   challenge to set its `image` field to match a `challengeId` the orchestrator already knows
   — coordinated manually, which doesn't scale past a handful of challenges.

(1) is almost certainly the right direction long-term, but it's the orchestrator's schema and
`RealSandboxClient` that would need to change, not this plugin.
