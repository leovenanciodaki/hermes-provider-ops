# Hermes provider quota — 1.0.0

Native `post_llm_call` plugin written from scratch for Hermes v0.23.1
`3b421578588243f920886e6fe79a6faa87ea026e`. Uses stdlib plus native Hermes imports.
No inference calls, cron, credential rotation, model changes, or priority changes.

## Delivery status

**Partial implementation; not deployment ready.** Quota plugin and hermetic tests
work locally. The deterministic rollout entrypoint deliberately exits nonzero and
writes explicit UNVERIFIED results: full installation-specific backup, Docker build,
cleanup, readiness, acceptance, service transaction, and rollback adapters are not
implemented. This includes Tanque. No SSH, deployment, Telegram traffic, API model
calls, commits, or pushes were performed. No real TESTE message ID exists.

The Docker helper refuses contracts it cannot preserve. It is not a complete Docker
recreation engine and must not be used alone to recreate either production container.
Bot source copies preserve the original dispatcher and bridge except the refresh
branch; host integration, fixed route configuration, ownership validation, and
end-to-end transport have not been verified. No configuration containing secrets
is included. Refresh results are returned through the authenticated bridge response;
the dispatcher does not currently send those results as a Telegram reply.

## Native installation

Once this repository has been published at a real commit SHA:

```sh
hermes plugins install https://github.com/leovenanciodaki/hermes-provider-ops --ref SHA --enable --yes-deps --force
```

`SHA` must be replaced with the actual repository commit. This pass did not create
one. The root manifest is installable; installation itself does not apply the core
patch. `scripts/install.py --core CORE --cli NATIVE_HERMES --repo-ref SHA` prechecks
both core file hashes and applies the two-file patch before native plugin installation.
A caller must first create the required backup. Installation is not transactional.
The installer does not yet retire fastrotate from the enabled list or remove obsolete
artifacts. Do not treat it as the requested complete rollout.

## Quota behavior

The hook captures profile home, contextvars, and native gateway scoped Telegram
secret before starting a daemon thread. Per-profile flock and atomic state writes
limit scans to once per 300 seconds across processes. Each pool account uses its
exact runtime key and base URL. Anthropic OAuth usage is fetched directly with native
headers; utilization is already percent, including values below 1. Codex uses native
`fetch_account_usage(read_only=True)` and raw `rate_limit` windows. Unsupported or
invalid windows are skipped. Notifications to Leo `7414371088` include account label,
percent, and reset in America/Sao_Paulo. Deduplication covers provider, account ID,
window, reset, and threshold (85/95). Failed sends remain eligible on the next scan.

`provider_ops.check(..., test=True, thresholds=(...), fetch=..., send=...)` is the
explicit injectable helper. A test invocation sends at most one prefixed `TESTE`
notification and returns its actual Telegram message ID. Tests use only fake senders.
Normal callbacks never select test mode. State is stored under each profile's
`provider-ops/`, with separate test state.

## Refresh and bot

`scripts/local_ops.py refresh anthropic|openai --cli /absolute/native/hermes`
uses native root semantics and invokes native `--profile NAME auth reset PROVIDER`
for root/default and each configured profile. `openai` maps to `openai-codex`.
The launcher needs the native runtime's `hermes_constants` available on PYTHONPATH
or alongside the resolved CLI. It does not import native YAML through system Python.
Diagnostics are bounded and redact common credential shapes; arbitrary provider
error strings have not been exhaustively audited.

`bot/dispatcher.py` preserves original Leo sender/chat/private checks and duplicate
guard before recognizing the two literal refresh commands. It forwards the original
update and ephemeral token to the existing authenticated host bridge route.
`bot/bridge.py` repeats validation; `bot/provider_host.py` persists a replay marker
before invoking fixed configured operator argv, without forwarding `_token`.
`bin/provider-operator` runs installation argv concurrently, with a 40-second process
deadline inside a 45-second host operator deadline. Fixed argv must be provisioned
outside Git in `/etc/hermes-provider-ops-routes.json`, with `operator_argv` and
`installations: [{"name": "...", "argv": ["absolute executable", "fixed args"]}]`.
No SSH keys or tokens belong in that file. Production route configuration is absent.

## Native cooldown patch evidence

`tests/native_reset.py` reproduced the clean-runtime defect using one pre-existing
simulated live agent and real native CredentialPool objects plus native auth-store
writes. Both account cooldown and model-only cooldown remain blocked after reset.
`tests/native_patched.py` executes the generated patched helper in isolation and
checks that the same agent restores after reset using a fresh available native pool.
Only downstream runtime installation is mocked, preventing inference or client calls.

The new patch stamps arm wall time and requires a newer native `status_cleared_at`,
matching primary provider/base URL, a matching reset account ID, and availability
for the primary model. No marker means no early clear. `patches/manifest.json`
archives exact pre/post SHA256. The read-only reference was never changed.

## Hermetic checks

```sh
python -m unittest discover -s tests -p 'test_*.py'
PYTHONPATH=../frota-hermes-upstream ../frota-hermes/.cache/test-env/bin/python tests/native_reset.py
PYTHONPATH=../frota-hermes-upstream ../frota-hermes/.cache/test-env/bin/python tests/native_patched.py
```

No entire Hermes suite is run. Seven standalone tests and two native test methods
cover percent handling, per-profile throttling/deduplication, send retries, explicit
single test alerts, unauthorized/duplicate bot commands, and the native reset gates.

## Rollout command contract

```sh
./scripts/rollout.py --repo-ref SHA --artifacts /private/provider-ops-results
./scripts/rollout.py --repo-ref SHA --target tanque --artifacts /private/tanque-results
```

Default order is Manu, Mafe, Luna, Aquario; `all` excludes Tanque. `--accept-turns`
and `--test-alert` are parsed but cannot execute while transaction adapters are absent.
Every target records missing acceptance and the explicit error; no remote operations
occur. Tanque is **not implemented or verified**, and its explicit command fails closed.
