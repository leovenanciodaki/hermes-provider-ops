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

Readiness requires fresh gateway state, a new process identity, version 0.23.1,
and the baseline connected platforms for every previously active home. Preexisting
fatal platforms are not required to recover. Docker readiness has a 200-second
budget, leaving time for an optional API turn and rollback. `--accept-turns`
explicitly authorizes one local OpenAI-compatible API turn per installation; its
output is private. API settings use native gateway configuration and secret scope.
Failure restores the tar and restarts the old installation. Host commands, service
permissions, API routing and timing still require an operator-reviewed live trial.

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
