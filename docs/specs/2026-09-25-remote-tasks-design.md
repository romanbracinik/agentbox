# Remote tasks: submit agent runs from a laptop to an SSH host

Status: draft for review (2026-09-25)

## Goal

Let a developer start an agent task from their laptop that runs on an always-on
Linux host (for example a private GCP VM reachable over IAP SSH), then list,
attach to, inspect and stop it from the laptop. The laptop can be closed while the
task runs. A one-time setup wizard prepares and validates the host.

## Non-goals

- Transferring uncommitted or untracked laptop changes. A task starts from a branch
  that already exists on the remote Git origin.
- Handing off a live agent session from the laptop.
- Nested Docker/Compose inside the task container. Project checks that need it run
  in CI on the pull request.
- A queue or concurrency cap. Each `remote run` starts immediately.
- Changes to the `server` profile, broker, scheduled review or `deploy/gcp`.

## User-facing commands

```bash
agentbox remote setup <host> [--ssh DEST]             # wizard, re-runnable
agentbox remote doctor <host>                         # read-only validation
agentbox remote run <host> --name <task> --branch <branch> [--agent claude] [-- AGENT_ARGS...]
agentbox remote list <host>
agentbox remote attach <host> <task>
agentbox remote logs <host> <task> [--tail N]
agentbox remote stop <host> <task> [--remove-worktree]
```

`--ssh` sets the ssh destination for `setup`. Without it, a new (not yet registered)
remote uses `<host>` itself as the ssh destination; a registered remote's stored `ssh`
value is used unless `--ssh` overrides it.

Long-running agent services (the existing `agentbox service` lifecycle, which is
Hermes-only) are managed the same way, but without any `--agent` flag:

```bash
agentbox remote service setup  <host>                                          # interactive: gateway setup
agentbox remote service start  <host> <name> [--restart-policy POLICY] [--env NAME ...]
agentbox remote service status <host> <name>
agentbox remote service logs   <host> <name> [--tail N]
agentbox remote service stop   <host> <name>
```

`service setup` takes only `<host>` — no service/container name. It runs
`agentbox run <base_dir>/repo --agent hermes -- gateway setup` interactively (an
allocated TTY, since the flow is interactive); this configures the per-repository
Hermes HOME, not a container, so it has nothing to name. `service start`/`status`/
`logs`/`stop` take a `<name>` and run the existing `agentbox service` commands on the
remote against `<base_dir>/repo`, with `<name>` passed through as the container name.
agentbox does not capture or store what the user enters during `gateway setup`.

`<host>` is a name registered by `remote setup`. It maps to an SSH destination
that is handed to `ssh` unchanged, so `~/.ssh/config` aliases, `ProxyCommand` (IAP)
and `ProxyJump` keep working.

## Laptop-side registry

`~/.config/agentbox/remotes.yaml`, written only by `remote setup`:

```yaml
remotes:
  agent-runner:
    ssh: agent-runner              # ssh destination, passed through verbatim
    repository: git@github.com:keboola/connection.git
    base_dir: /home/<user>/agentbox-remote/connection   # absolute path on the remote
    agent: claude                  # default agent for `remote run`
    runtime: docker                # podman or docker, detected by the wizard
```

All five keys are required. `remote setup` asks for `ssh`, `repository`, `base_dir`
and `agent`; `runtime` is detected (Podman preferred, falling back to Docker) rather
than asked. Other commands fail with an actionable error when the host is unknown or
a key is missing. The file contains no credentials.

## Remote layout

```
<base_dir>/repo/          # primary clone of `repository`
<base_dir>/wt/<task>/     # one git worktree per task
```

Each task runs in a tmux session named `agentbox-<task>` executing
`agentbox run <base_dir>/wt/<task> --agent <agent> --name agentbox-task-<task> -- AGENT_ARGS`.
Task names are validated with the existing container-name rules.

## Remote host requirements

The remote runs the local `run`/`service` profile, not the `server` profile:

- Linux with Podman or Docker usable by the SSH user without sudo.
- Outbound network access for the agent, Git, package registries and model APIs.
  The `deploy/gcp` module (deny-all egress, broker, baked image) is built for the
  `server` profile and is not a target for remote tasks.
- `git`, `tmux`, `pipx`, and `gh` logged in for the SSH user when tasks push or open PRs.

Membership in the `docker` group is root-equivalent on the host; use a dedicated host.

## Setup wizard (`remote setup <host>`)

Each step reports OK / FIXED / ACTION REQUIRED and the wizard stops at the first
step it cannot complete. Re-running skips satisfied steps.

1. **SSH**: `ssh -o BatchMode=yes <ssh> true`. On failure print the exact error and
   stop; the wizard never edits `~/.ssh/config`.
2. **Runtime**: detect Podman or Docker usable by the SSH user without sudo. If
   missing, print the distribution-specific install command and stop. The wizard
   never runs sudo.
3. **Tools**: require `git`, `tmux` and `pipx`; print install commands when missing.
4. **agentbox parity**: the remote must run the same version as the laptop. When the
   local agentbox runs from a source checkout, build a wheel from it, stream it to the
   remote base64-encoded over the existing SSH connection (stdin, not `scp`), and
   install it with `pipx`; otherwise install `agentbox==<local version>` with `pipx`.
   Verify `agentbox --version` matches. Asks for confirmation before installing or
   upgrading.
5. **GitHub**: run `gh auth status` remotely. When not logged in, print the command
   the user runs in their own terminal (`ssh -t <ssh> gh auth login --web`). The
   wizard never reads, prints or transfers a token.
6. **Repository**: ask for `repository` and `base_dir`, then clone into
   `<base_dir>/repo` if absent; if present, verify its origin matches.
7. **Remote config**: settings needed are `runtime`, `credentials.github: true` and
   `state_scope: repository` (see below). agentbox loads the first config file it
   finds and never merges across files (repository-tracked `.agentbox.yaml`/`.yml`
   first, then the remote user's global `~/.config/agentbox/config.yaml`/`.yml` or
   `~/.agentbox.yaml`). If the repository tracks its own config, the wizard only
   checks that the file exists — it does not read or diff its contents — and always
   reports `action` naming all three keys to add there, since a repo-tracked file
   always wins and the wizard never writes to it (worktrees do not carry untracked
   files from the primary clone). Otherwise it adds any missing keys to the first
   existing global file,
   creating `~/.config/agentbox/config.yaml` only if none exists yet, after asking
   for confirmation. A key already present with a different value is reported as
   `action` and never overwritten. Malformed or non-mapping config (invalid YAML, a
   non-mapping document, a non-mapping `credentials`) is also reported as `action`,
   not a crash.
8. **Image**: run `agentbox build --agent <agent>` remotely so the first task does
   not wait for a build.
9. **Save** the registry entry.
10. **Agent login** (informational, does not block): print the command that logs the
    agent in once on the remote (`ssh -t <ssh> agentbox run <base_dir>/repo --agent
    <agent>`). This is not verified automatically — agentbox does not inspect agent
    credentials — so this step always reports `info`, never `action`. Login state
    lives on the remote, shared by all worktrees of the repo.

`remote doctor` runs the checks of steps 1-7 and the step-10 login hint, without
changing anything and without a save step. Invalid wizard answers (for example a
non-absolute `base_dir`) are reported as `action`, not a crash.

## Shared agent state across worktrees

`state_home()` currently keys private HOME by the resolved workspace path, so each
worktree gets its own login. Add a config key:

```yaml
state_scope: workspace   # default, current behaviour
# state_scope: repository  -> key by the Git common directory
```

With `repository`, the digest input is the resolved Git common directory reported by
`git rev-parse --git-common-dir`, so the primary clone and all its worktrees share one
HOME per agent. Outside a Git repository `repository` is a configuration error, not a
silent fallback. The existing lease and active-container checks keep working because
they operate on the resolved HOME path. `state show` reports the scope in use.

## Error handling

- Every remote step uses `ssh -o BatchMode=yes` and surfaces stderr verbatim.
- `run` rejects unknown options before `--`; everything after `--` is forwarded to
  the agent unchanged, including strings that look like flags.
- `run` refuses an existing task name (tmux session or worktree) instead of reusing it.
- `run` fails when `--branch` does not exist on the remote after `git fetch`.
- `stop` without `--remove-worktree` keeps the worktree so work is not lost; with it,
  refuse when the worktree has uncommitted changes.
- No command prints environment values or file contents from the remote HOME.
- Invalid wizard answers and malformed or non-mapping remote config are reported as
  `action`, never raised as an unhandled error.

## Testing

- Unit tests with an injected command runner that records ssh invocations and
  returns scripted results: argument quoting, registry validation, each wizard step
  (OK / missing / failure), name validation, refusal paths.
- `state_scope` tests: shared HOME for two worktrees of one repository, distinct HOME
  for different repositories, error outside Git, default unchanged.
- Opt-in integration test (documented, not in default CI) against a real SSH host.
- `pytest`, `ruff check`, `ruff format --check`, `mypy` clean.

## Documentation

README section "Remote tasks", ROADMAP entry, CHANGELOG entry, AGENTS.md
architecture line for the new module.

## Manual acceptance

On a real host, after all automated tasks are complete:

1. `agentbox remote setup <host>` until all steps are OK/INFO; log the agent in once
   with the printed command.
2. `agentbox remote run <host> --name smoke --branch <existing-branch> -- "print the
   repo README title"`; `remote attach`, detach; close the laptop lid; reopen;
   `remote logs`.
3. `agentbox remote stop <host> smoke --remove-worktree`.
4. `agentbox remote service setup <host>`, then `remote service start <host> <name>
   --restart-policy unless-stopped`, `service status`, `service logs`.

## Open questions for the maintainer

- Is `state_scope: repository` acceptable in core, or should shared state stay
  opt-in through `claude.share_host_config` on the remote?
- Should `remote` live in core or as a plugin?
