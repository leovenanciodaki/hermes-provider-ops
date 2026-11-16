# Hermes provider operations 1.1.0

Provider quota hook and transactional rollout source for Hermes v0.23.1,
core `3b421578588243f920886e6fe79a6faa87ea026e`. This revision has not been
deployed or verified on fleet hosts. Tests use fake transports; they prove local
logic, not compatibility with every live installation.

The quota hook uses native credential pools read-only, with per-profile throttling,
threshold deduplication and bounded notifications. Refresh uses native auth reset;
it does not call a model. Explicit test notifications have a `TESTE` prefix.

## Rollout

Publish the intended repository commit first. `REF` must be its exact lowercase
40-hex SHA, present in the local Git repository. Working-tree edits are not the
plugin release. Private artifacts must be outside this repository.

```sh
python scripts/rollout.py --repo-ref REF --artifacts /private/provider-results --detach
python scripts/rollout.py --repo-ref REF --target mafe --artifacts /private/provider-results --status /tmp/hermes-provider-ops-mafe-JOB
python scripts/rollout.py --repo-ref REF --target mafe --artifacts /private/provider-results --rollback /tmp/hermes-provider-ops-mafe-JOB --detach
```

Default targets: manu, mafe, luna, aquario. Tanque is excluded unless explicitly
selected with `--target tanque`; its adapter covers hermes-carla and hermes-ana.
`--image sha256:…` overrides the Docker derivative. Mafe/Aquario default to the
specified staged images, checked against native version/core identity and both
patched file hashes. Without a staged image, the adapter builds a derivative of
the installation's old image using the pinned core archive, preserving runtime
directories and writing an install stamp. Docker HTTP requests use v1.45.

A local Git bundle carries the pinned plugin release. Native plugin install and
legacy disable run before stopping gateways. Only provider-ops is installed; only
named legacy artifacts are removed. Docker Config, HostConfig and network creation
options are preserved, including User, resources and unrelated mounts. Configs,
provider plugin directories, mounted boot file and selected legacy files have a
private tar backup; sessions and general backups are excluded. The old container
remains available for rollback. Manu and Luna patch idempotently and restart their
native service when code changes. Host jobs detach, writing job.log, result.json
and exit-code. Local polling does not own job lifetime.

Acceptance always requires one authenticated local API turn plus a live gateway
PID reporting version 0.23.1 from `/health/detailed`. It does **not** require a
new PID, a fresh status file, or every messaging platform to reconnect. The old
`--accept-turns` flag remains compatible but is no longer needed. Probe candidates
come from configured homes even when their status is absent or stale; the first
successful turn completes installation acceptance. The returned text must be
nonempty, not necessarily the literal prompt marker. Startup/probe commands are
bounded (up to 900 seconds for Aquario, 300 for other installations); a readiness
or platform deadline alone never triggers rollback after a turn has responded.

Platform observations older than job start or from a dead/replaced writer are
ignored, including port hints. Preexisting disconnected/fatal platforms do not
have to recover. Fresh platform regressions, plugin readback, cleanup or evidence
write failures retain the new version, record the error and return **2** without
retrying or restarting. Automatic rollback is reserved for failure to start the
new gateway at all, or failure of every local API candidate to return a real turn.
A start-command timeout first checks the API instead of assuming an outage.

Each execution attempts the update once and, only for a total gateway failure,
one rollback. A host-wide advisory lock prevents overlapping jobs even with
`--force`; its persisted timestamp refuses another execution within **6 hours**
unless `--force` is supplied. Status polling does not acquire this lock. Explicit
rollback is also a new execution and needs `--force` during the cooldown. Workers
use exit codes **0** (accepted), **1** (total failure; automatic rollback attempted),
**2** (incomplete; no automatic rollback), **3** (cooldown/concurrent execution
refused). The foreground operator preserves these codes; detached jobs expose
`exit_code` in `result.json`. Foreground polling waits for the terminal result;
a polling timeout returns 2 and leaves the detached worker alone, never success
or an automatic retry. Explicit rollback uses a fresh execution identifier and
`result-<execution-id>.json`, preserving the original job result. To inspect it,
add the returned `--execution-id` to `--status JOB`. Only jobs staged with this
worker revision support these execution-specific rollback result files.

Legacy scans and executable inventory prune `uv-cache`, `.cache`, pip/uv and
other tool caches before descending. Recursive ownership/permission changes are
restricted to the pinned release/staged source, bounded below 50 MiB, not runtime
caches. Docker uses `stop -t 60` with a 120-second command timeout, independent of
readiness budgets. Host commands, service permissions, API routing and timing
still require an operator-reviewed live trial; **no rollout was run for this PR**.

## Refresh bot

```sh
python scripts/local_ops.py refresh anthropic --cli /absolute/native/hermes
python scripts/local_ops.py refresh openai --cli /absolute/native/hermes
```

Dispatcher preserves the existing sender/private-chat, duplicate and Kanban guards.
Refresh forwards through the authenticated bridge, then sends one reply containing
one line for each of manu, mafe, luna, aquario and tanque. Only the literal boolean
`provider_ops_test: true` adds `TESTE`. Missing/unreachable installations report a
bounded failure; subprocess diagnostics and credentials are not relayed.

Provision `/etc/hermes-provider-ops-routes.json` privately with `operator_argv`
and `installations: [{"name":"manu","argv":["/absolute/fixed/launcher"]}, …]`.
The operator runs five installations concurrently, each with a 40-second timeout.
No token is passed to those subprocesses. Existing bridge functions remain intact.
`python scripts/deploy_bot.py SOURCE DEST --preimage-sha256 HASH` is an optional
local atomic-copy helper with a preimage guard and backup. It was not run.

## Tests

```sh
python -m unittest discover -s tests -p 'test_*.py'
python -m py_compile scripts/*.py bot/*.py bin/provider-operator
```

Hermetic tests cover quota behavior, strict bot guards/replies, Docker recipe
preservation, baseline readiness, legacy scan exclusions, exact restore, local
native install ordering, worker routes, failed-cut rollback and safe diagnostics.
