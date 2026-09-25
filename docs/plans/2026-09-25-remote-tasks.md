# Remote Tasks Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a laptop start, list, attach to, inspect and stop agent tasks and Hermes services on an SSH host, with a re-runnable setup wizard and doctor.

**Architecture:** A thin SSH layer (`RemoteShell`) runs existing `agentbox run` / `agentbox service` commands on the remote inside `bash -lc`. Tasks are git worktrees plus tmux sessions under a per-repository `base_dir`. A laptop-side registry (`~/.config/agentbox/remotes.yaml`) stores host settings written only by the wizard. Core gains one opt-in config key, `state_scope: repository`, so all worktrees of one repository share an agent HOME (one login).

**Tech Stack:** Python >= 3.10, click, pydantic v2, PyYAML, pytest, ruff, mypy (strict).

**Spec:** `docs/specs/2026-09-25-remote-tasks-design.md`

## Global Constraints

- Every SSH call: `ssh -o BatchMode=yes`; stderr of a failed call is surfaced verbatim.
- The wizard never runs sudo, never edits `~/.ssh/config`, never reads, prints or transfers tokens.
- The wizard stops at the first step with status `action`; re-running skips satisfied steps.
- Registry keys `ssh`, `repository`, `base_dir`, `agent`, `runtime` are all required; values come from the user (wizard prompts) or detection (`runtime`), never guessed. `base_dir` must be absolute.
- Remote config writes add missing keys only; existing keys with a different value are reported, never overwritten; every write is confirmed first.
- `state_scope` default is `workspace` (current behaviour). `repository` outside Git is a `ConfigError`, not a fallback.
- No command prints environment values or file contents from the remote HOME.
- Task and service names match `[a-zA-Z0-9][a-zA-Z0-9_.-]*`; branch names match `[A-Za-z0-9._/-]+` and must not start with `-`.
- Checks before each commit: `.venv/bin/ruff format src/agentbox tests` (line length is 100 and `E501` is enforced; code blocks in this plan are not pre-wrapped), then `.venv/bin/pytest -q`, `.venv/bin/ruff check src/agentbox tests`, `.venv/bin/mypy src/agentbox`. Wherever a task's commit step shows `ruff format --check`, run `ruff format` first.
- Commit messages: conventional prefix, no `Co-Authored-By` trailer.
- Code comments: one short line, non-obvious WHY only.

## Review Focus

1. Task name or branch containing shell metacharacters or a leading `-` → rejected before any SSH call (Task 4 test `test_start_rejects_unsafe_names`).
2. `remote list` on a host with no tmux server running → empty list, exit 0 (Task 4 test `test_list_without_tmux_server`).
3. `remote setup` re-run when everything is already configured → no prompts, no writes (Task 6 test `test_setup_is_idempotent`).
4. Remote config already has `runtime: podman` while Docker was detected → not overwritten, reported as `action` (Task 6 test `test_remote_config_conflict_not_overwritten`).
5. `stop --remove-worktree` on a worktree with uncommitted changes → refused, worktree kept (Task 4 test `test_stop_refuses_dirty_worktree`).

---

## File Structure

| Path | Responsibility |
| --- | --- |
| `src/agentbox/config.py` | add `state_scope` field |
| `src/agentbox/state.py` | `state_home(..., scope=...)` keys HOME by Git common dir for `repository` |
| `src/agentbox/execution.py`, `src/agentbox/state_cli.py` | pass `config.state_scope` |
| `src/agentbox/exceptions.py` | add `RemoteError` |
| `src/agentbox/remote_registry.py` | `RemoteHost` model, load/save/get of `remotes.yaml` |
| `src/agentbox/remote_ssh.py` | `CommandResult`, `CommandRunner`, `subprocess_runner`, `RemoteShell` |
| `src/agentbox/remote_tasks.py` | start/list/attach/logs/stop tasks |
| `src/agentbox/remote_services.py` | Hermes service passthrough |
| `src/agentbox/remote_setup.py` | wizard steps, `run_setup`, `run_doctor` |
| `src/agentbox/remote_cli.py` | click `remote` group |
| `src/agentbox/cli.py` | register `remote` |
| `tests/remote_fakes.py` | `FakeRunner` shared by remote tests |
| `tests/test_state_scope.py`, `tests/test_remote_registry.py`, `tests/test_remote_ssh.py`, `tests/test_remote_tasks.py`, `tests/test_remote_services.py`, `tests/test_remote_setup.py`, `tests/test_remote_cli.py` | tests |
| `README.md`, `ROADMAP.md`, `CHANGELOG.md`, `AGENTS.md`, spec | docs |

---

### Task 1: Development environment and `state_scope`

**Files:**
- Modify: `src/agentbox/config.py` (class `Config`)
- Modify: `src/agentbox/state.py:19-25`
- Modify: `src/agentbox/execution.py:103`
- Modify: `src/agentbox/state_cli.py` (`show`, `reset`)
- Test: `tests/test_state_scope.py`

**Interfaces:**
- Produces: `Config.state_scope: Literal["workspace", "repository"]`; `state_home(root: Path, workspace: Path, agent: str, scope: Literal["workspace", "repository"] = "workspace") -> Path`.

- [ ] **Step 1: Create the dev environment**

```bash
cd /Users/romanbracinik/keboola/agentbox
python3 -m venv .venv
.venv/bin/pip install -q -e '.[dev]'
.venv/bin/pytest -q 2>&1 | tail -3
```
Expected: the existing suite passes (record the count in the report).

- [ ] **Step 2: Write the failing tests** — `tests/test_state_scope.py`

```python
import subprocess
from pathlib import Path

import pytest

from agentbox.config import Config
from agentbox.exceptions import ConfigError
from agentbox.state import state_home


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo_with_worktree(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", cwd=repo)
    _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "i", cwd=repo)
    worktree = tmp_path / "wt" / "task"
    _git("worktree", "add", "-q", str(worktree), "-b", "task", cwd=repo)
    return repo, worktree


def test_default_scope_is_workspace() -> None:
    assert Config().state_scope == "workspace"


def test_workspace_scope_separates_worktrees(tmp_path: Path, repo_with_worktree: tuple[Path, Path]) -> None:
    repo, worktree = repo_with_worktree
    assert state_home(tmp_path / "s", repo, "claude") != state_home(tmp_path / "s", worktree, "claude")


def test_repository_scope_shares_home_across_worktrees(
    tmp_path: Path, repo_with_worktree: tuple[Path, Path]
) -> None:
    repo, worktree = repo_with_worktree
    root = tmp_path / "s"
    assert state_home(root, repo, "claude", "repository") == state_home(root, worktree, "claude", "repository")


def test_repository_scope_separates_repositories(tmp_path: Path) -> None:
    homes = set()
    for name in ("a", "b"):
        repo = tmp_path / name
        repo.mkdir()
        _git("init", "-q", cwd=repo)
        homes.add(state_home(tmp_path / "s", repo, "claude", "repository"))
    assert len(homes) == 2


def test_repository_scope_outside_git_is_error(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(ConfigError, match="requires a Git repository"):
        state_home(tmp_path / "s", plain, "claude", "repository")
```

- [ ] **Step 3: Run to verify failure**

Run: `.venv/bin/pytest tests/test_state_scope.py -q`
Expected: FAIL (`state_scope` attribute missing / `state_home` takes 3 positional arguments).

- [ ] **Step 4: Implement**

`src/agentbox/config.py`, add to `class Config` after `state_dir`:
```python
    state_scope: Literal["workspace", "repository"] = "workspace"
```

`src/agentbox/state.py`, add import `from typing import Literal` and `from .git import detect_worktree`, replace `state_home`:
```python
def state_home(
    root: Path,
    workspace: Path,
    agent: str,
    scope: Literal["workspace", "repository"] = "workspace",
) -> Path:
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", agent):
        raise ConfigError("Invalid agent state name")
    key = workspace.resolve()
    if scope == "repository":
        info = detect_worktree(workspace)
        if info is None:
            raise ConfigError(f"state_scope 'repository' requires a Git repository: {workspace}")
        key = info.git_common_dir.resolve()
    digest = hashlib.sha256(str(key).encode()).hexdigest()[:16]
    home = root.expanduser().resolve() / digest / agent / "home"
    validate_home(home)
    return home
```

`src/agentbox/execution.py:103`:
```python
    home = state_home(
        config.state_dir,
        workspace,
        agent.name if agent is not None else "shell",
        config.state_scope,
    )
```

`src/agentbox/state_cli.py`: in `show` and `reset` replace `state_home(config.state_dir, workspace, agent)` with `state_home(config.state_dir, workspace, agent, config.state_scope)`; in `show` change the non-path output to:
```python
    click.echo(
        str(home) if path_only else f"{home}\nScope: {config.state_scope}\nBytes: {state_size(home)}"
    )
```

- [ ] **Step 5: Run tests**

Run: `.venv/bin/pytest -q`
Expected: all pass (new 5 + existing). If an existing `state show` output assertion breaks because of the added `Scope:` line, report it as a concern instead of editing the existing test.

- [ ] **Step 6: Lint, type-check, commit**

```bash
.venv/bin/ruff check src/agentbox tests && .venv/bin/ruff format --check src/agentbox tests && .venv/bin/mypy src/agentbox
git add src/agentbox/config.py src/agentbox/state.py src/agentbox/execution.py src/agentbox/state_cli.py tests/test_state_scope.py
git commit -m "feat: add repository-scoped agent state"
```

---

### Task 2: Remote host registry

**Files:**
- Create: `src/agentbox/remote_registry.py`
- Modify: `src/agentbox/exceptions.py` (add `RemoteError`)
- Test: `tests/test_remote_registry.py`

**Interfaces:**
- Produces: `class RemoteError(AgentboxError)`; `RemoteHost(ssh: str, repository: str, base_dir: str, agent: str, runtime: Literal["podman", "docker"])` with properties `repo_dir -> str` (`f"{base_dir}/repo"`) and `worktrees_dir -> str` (`f"{base_dir}/wt"`); `registry_path() -> Path`; `load_registry(path: Path | None = None) -> dict[str, RemoteHost]`; `get_host(name: str, path: Path | None = None) -> RemoteHost`; `save_host(name: str, host: RemoteHost, path: Path | None = None) -> Path`; `validate_remote_name(name: str) -> None`.

- [ ] **Step 1: Write the failing tests** — `tests/test_remote_registry.py`

```python
from pathlib import Path

import pytest

from agentbox.exceptions import ConfigError, RemoteError
from agentbox.remote_registry import RemoteHost, get_host, load_registry, save_host


def _host(**overrides: str) -> RemoteHost:
    values = {
        "ssh": "agent-runner",
        "repository": "git@github.com:org/repo.git",
        "base_dir": "/home/u/agentbox-remote/repo",
        "agent": "claude",
        "runtime": "docker",
    }
    values.update(overrides)
    return RemoteHost.model_validate(values)


def test_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "remotes.yaml"
    save_host("vm", _host(), path)
    assert get_host("vm", path) == _host()
    assert get_host("vm", path).repo_dir == "/home/u/agentbox-remote/repo/repo"
    assert get_host("vm", path).worktrees_dir == "/home/u/agentbox-remote/repo/wt"


def test_save_preserves_other_hosts(tmp_path: Path) -> None:
    path = tmp_path / "remotes.yaml"
    save_host("a", _host(), path)
    save_host("b", _host(ssh="other"), path)
    assert set(load_registry(path)) == {"a", "b"}


def test_unknown_host_points_to_setup(tmp_path: Path) -> None:
    with pytest.raises(RemoteError, match="agentbox remote setup vm"):
        get_host("vm", tmp_path / "missing.yaml")


def test_relative_base_dir_rejected() -> None:
    with pytest.raises(ValueError, match="absolute"):
        _host(base_dir="relative/path")


def test_missing_key_rejected(tmp_path: Path) -> None:
    path = tmp_path / "remotes.yaml"
    path.write_text("remotes:\n  vm:\n    ssh: x\n")
    with pytest.raises(ConfigError, match="vm"):
        load_registry(path)


def test_invalid_remote_name_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        save_host("-bad", _host(), tmp_path / "remotes.yaml")
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_remote_registry.py -q`
Expected: FAIL with `ModuleNotFoundError: agentbox.remote_registry`.

- [ ] **Step 3: Implement**

Append to `src/agentbox/exceptions.py`:
```python
class RemoteError(AgentboxError):
    """A remote host command failed or the remote is not configured."""
```

`src/agentbox/remote_registry.py`:
```python
"""Laptop-side registry of remote task hosts."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from .exceptions import ConfigError, RemoteError

_NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*")


class RemoteHost(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ssh: str
    repository: str
    base_dir: str
    agent: str
    runtime: Literal["podman", "docker"]

    @field_validator("ssh", "repository", "agent")
    @classmethod
    def _not_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be empty")
        return value

    @field_validator("base_dir")
    @classmethod
    def _absolute(cls, value: str) -> str:
        if not value.startswith("/"):
            raise ValueError("base_dir must be an absolute path on the remote")
        return value.rstrip("/")

    @property
    def repo_dir(self) -> str:
        return f"{self.base_dir}/repo"

    @property
    def worktrees_dir(self) -> str:
        return f"{self.base_dir}/wt"


def validate_remote_name(name: str) -> None:
    if not _NAME.fullmatch(name):
        raise ConfigError(f"Invalid remote name: {name}")


def registry_path() -> Path:
    return Path.home() / ".config" / "agentbox" / "remotes.yaml"


def load_registry(path: Path | None = None) -> dict[str, RemoteHost]:
    path = path or registry_path()
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text()) or {}
    hosts: dict[str, RemoteHost] = {}
    for name, values in (data.get("remotes") or {}).items():
        try:
            hosts[name] = RemoteHost.model_validate(values)
        except ValidationError as err:
            raise ConfigError(f"Invalid remote '{name}' in {path}: {err}") from None
    return hosts


def get_host(name: str, path: Path | None = None) -> RemoteHost:
    hosts = load_registry(path)
    if name not in hosts:
        raise RemoteError(f"Unknown remote '{name}'. Run: agentbox remote setup {name}")
    return hosts[name]


def save_host(name: str, host: RemoteHost, path: Path | None = None) -> Path:
    validate_remote_name(name)
    path = path or registry_path()
    hosts = load_registry(path)
    hosts[name] = host
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"remotes": {key: value.model_dump() for key, value in sorted(hosts.items())}}
    path.write_text(yaml.safe_dump(payload, sort_keys=False))
    return path
```

- [ ] **Step 4: Run tests** — `.venv/bin/pytest tests/test_remote_registry.py -q` → PASS.

- [ ] **Step 5: Lint, type-check, commit**

```bash
.venv/bin/ruff check src/agentbox tests && .venv/bin/ruff format --check src/agentbox tests && .venv/bin/mypy src/agentbox
git add src/agentbox/exceptions.py src/agentbox/remote_registry.py tests/test_remote_registry.py
git commit -m "feat: add remote host registry"
```

---

### Task 3: SSH transport

**Files:**
- Create: `src/agentbox/remote_ssh.py`
- Create: `tests/remote_fakes.py`
- Test: `tests/test_remote_ssh.py`

**Interfaces:**
- Consumes: `RemoteError` (Task 2).
- Produces: `CommandResult(returncode: int, stdout: str, stderr: str)`; `CommandRunner` protocol `(argv: Sequence[str], *, input: str | None = None, tty: bool = False) -> CommandResult`; `subprocess_runner`; `RemoteShell(destination: str, runner: CommandRunner = subprocess_runner)` with `argv(command, *, tty=False) -> list[str]`, `run(command, *, input=None) -> CommandResult`, `check(command, *, error: str, input=None) -> str`, `interactive(command) -> int`, `write_file(relative_path: str, content: str) -> None`. `FakeRunner` in `tests/remote_fakes.py` with `.calls: list[FakeCall]`, `.remote_commands() -> list[str]`, `responses: dict[str, CommandResult]` matched by substring of the remote command string.

- [ ] **Step 1: Write the test helper** — `tests/remote_fakes.py`

```python
from __future__ import annotations

import shlex
from collections.abc import Sequence
from dataclasses import dataclass, field

from agentbox.remote_ssh import CommandResult

OK = CommandResult(0, "", "")


def fail(stderr: str = "boom", code: int = 1) -> CommandResult:
    return CommandResult(code, "", stderr)


def out(stdout: str) -> CommandResult:
    return CommandResult(0, stdout, "")


@dataclass
class FakeCall:
    argv: list[str]
    input: str | None
    tty: bool

    @property
    def remote(self) -> str:
        # ssh argv ends with "bash -lc '<command>'"; unwrap to the inner command.
        return shlex.split(self.argv[-1])[-1]


@dataclass
class FakeRunner:
    responses: dict[str, CommandResult] = field(default_factory=dict)
    calls: list[FakeCall] = field(default_factory=list)

    def __call__(
        self, argv: Sequence[str], *, input: str | None = None, tty: bool = False
    ) -> CommandResult:
        call = FakeCall(list(argv), input, tty)
        self.calls.append(call)
        for needle, result in self.responses.items():
            if needle in call.remote:
                return result
        return OK

    def remote_commands(self) -> list[str]:
        return [call.remote for call in self.calls]
```

- [ ] **Step 2: Write the failing tests** — `tests/test_remote_ssh.py`

```python
import shlex

import pytest

from agentbox.exceptions import RemoteError
from agentbox.remote_ssh import RemoteShell
from tests.remote_fakes import FakeRunner, fail, out


def test_argv_uses_batch_mode_and_login_shell() -> None:
    argv = RemoteShell("host").argv(["echo", "a b"])
    assert argv[:5] == ["ssh", "-T", "-o", "BatchMode=yes", "host"]
    assert shlex.split(argv[-1]) == ["bash", "-lc", "echo 'a b'"]


def test_metacharacters_stay_quoted() -> None:
    argv = RemoteShell("host").argv(["echo", "$(rm -rf ~); x"])
    assert shlex.split(shlex.split(argv[-1])[-1]) == ["echo", "$(rm -rf ~); x"]


def test_tty_flag() -> None:
    assert RemoteShell("host").argv(["true"], tty=True)[1] == "-t"


def test_check_surfaces_stderr() -> None:
    shell = RemoteShell("host", FakeRunner({"git": fail("fatal: nope")}))
    with pytest.raises(RemoteError, match="Cannot fetch: fatal: nope"):
        shell.check(["git", "fetch"], error="Cannot fetch")


def test_check_returns_stdout() -> None:
    shell = RemoteShell("host", FakeRunner({"echo": out("hi\n")}))
    assert shell.check(["echo", "hi"], error="x") == "hi\n"


def test_write_file_streams_content_via_stdin() -> None:
    runner = FakeRunner()
    RemoteShell("host", runner).write_file(".config/agentbox/config.yaml", "a: 1\n")
    call = runner.calls[-1]
    assert call.input == "a: 1\n"
    assert ".config/agentbox/config.yaml" in call.remote
```

- [ ] **Step 3: Run to verify failure** — `.venv/bin/pytest tests/test_remote_ssh.py -q` → FAIL (`ModuleNotFoundError`).

- [ ] **Step 4: Implement** — `src/agentbox/remote_ssh.py`

```python
"""SSH transport for remote commands."""

from __future__ import annotations

import shlex
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from .exceptions import RemoteError


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


class CommandRunner(Protocol):
    def __call__(
        self, argv: Sequence[str], *, input: str | None = None, tty: bool = False
    ) -> CommandResult: ...


def subprocess_runner(
    argv: Sequence[str], *, input: str | None = None, tty: bool = False
) -> CommandResult:
    if tty:
        return CommandResult(subprocess.run(list(argv)).returncode, "", "")
    completed = subprocess.run(list(argv), input=input, capture_output=True, text=True)
    return CommandResult(completed.returncode, completed.stdout, completed.stderr)


class RemoteShell:
    def __init__(self, destination: str, runner: CommandRunner = subprocess_runner) -> None:
        self.destination = destination
        self._runner = runner

    def argv(self, command: Sequence[str], *, tty: bool = False) -> list[str]:
        # A login shell puts pipx/user tools on PATH for non-interactive ssh sessions.
        remote = shlex.join(["bash", "-lc", shlex.join(command)])
        return ["ssh", "-t" if tty else "-T", "-o", "BatchMode=yes", self.destination, remote]

    def run(self, command: Sequence[str], *, input: str | None = None) -> CommandResult:
        return self._runner(self.argv(command), input=input)

    def check(self, command: Sequence[str], *, error: str, input: str | None = None) -> str:
        result = self.run(command, input=input)
        if result.returncode != 0:
            detail = result.stderr.strip() or f"exit {result.returncode}"
            raise RemoteError(f"{error}: {detail}")
        return result.stdout

    def interactive(self, command: Sequence[str]) -> int:
        return self._runner(self.argv(command, tty=True), tty=True).returncode

    def write_file(self, relative_path: str, content: str) -> None:
        self.check(
            ["bash", "-c", 'mkdir -p "$(dirname "$1")" && cat > "$1"', "_", relative_path],
            error=f"Cannot write {relative_path}",
            input=content,
        )
```

- [ ] **Step 5: Run tests** — `.venv/bin/pytest tests/test_remote_ssh.py -q` → PASS.

- [ ] **Step 6: Lint, type-check, commit**

```bash
.venv/bin/ruff check src/agentbox tests && .venv/bin/ruff format --check src/agentbox tests && .venv/bin/mypy src/agentbox
git add src/agentbox/remote_ssh.py tests/remote_fakes.py tests/test_remote_ssh.py
git commit -m "feat: add ssh transport for remote commands"
```

---

### Task 4: Remote tasks

**Files:**
- Create: `src/agentbox/remote_tasks.py`
- Test: `tests/test_remote_tasks.py`

**Interfaces:**
- Consumes: `RemoteShell` (Task 3), `RemoteHost` (Task 2), `RemoteError`, `validate_name` from `agentbox.service`.
- Produces: `session_name(task: str) -> str` (`agentbox-<task>`); `container_name(task: str) -> str` (`agentbox-task-<task>`); `TaskStatus(name: str, running: bool)`; `start_task(shell, host, task, branch, agent, agent_args: Sequence[str]) -> None`; `list_tasks(shell, host) -> list[TaskStatus]`; `attach_task(shell, task) -> int`; `task_logs(shell, task, tail: int) -> str`; `stop_task(shell, host, task, *, remove_worktree: bool) -> None`.

- [ ] **Step 1: Write the failing tests** — `tests/test_remote_tasks.py`

```python
import shlex

import pytest

from agentbox.exceptions import ConfigError, RemoteError
from agentbox.remote_registry import RemoteHost
from agentbox.remote_ssh import RemoteShell
from agentbox.remote_tasks import TaskStatus, list_tasks, start_task, stop_task, task_logs
from tests.remote_fakes import FakeRunner, fail, out

HOST = RemoteHost(
    ssh="vm", repository="git@github.com:o/r.git", base_dir="/srv/ab/r", agent="claude", runtime="docker"
)


def _shell(runner: FakeRunner) -> RemoteShell:
    return RemoteShell("vm", runner)


def test_start_creates_worktree_and_session() -> None:
    runner = FakeRunner({"tmux has-session": fail("no session"), "test -e": fail("")})
    start_task(_shell(runner), HOST, "dmd-1", "roman/DMD-1", "claude", ["do it"])
    cmds = runner.remote_commands()
    assert "git -C /srv/ab/r/repo fetch origin roman/DMD-1" in cmds
    assert "git -C /srv/ab/r/repo worktree add /srv/ab/r/wt/dmd-1 roman/DMD-1" in cmds
    tmux = shlex.split(cmds[-1])
    assert tmux[:7] == ["tmux", "new-session", "-d", "-s", "agentbox-dmd-1", "-c", "/srv/ab/r/wt/dmd-1"]
    assert shlex.split(tmux[-1]) == [
        "agentbox", "run", "/srv/ab/r/wt/dmd-1", "--agent", "claude",
        "--name", "agentbox-task-dmd-1", "--", "do it",
    ]


@pytest.mark.parametrize(
    ("task", "branch"),
    [("-x", "main"), ("a;b", "main"), ("ok", "-main"), ("ok", "a;rm -rf ~"), ("ok", "a b")],
)
def test_start_rejects_unsafe_names(task: str, branch: str) -> None:
    runner = FakeRunner()
    with pytest.raises(ConfigError):
        start_task(_shell(runner), HOST, task, branch, "claude", [])
    assert runner.calls == []


def test_start_refuses_running_task() -> None:
    runner = FakeRunner()  # has-session succeeds by default
    with pytest.raises(RemoteError, match="already running"):
        start_task(_shell(runner), HOST, "dmd-1", "main", "claude", [])


def test_start_refuses_existing_worktree() -> None:
    runner = FakeRunner({"tmux has-session": fail("")})  # test -e succeeds by default
    with pytest.raises(RemoteError, match="already exists"):
        start_task(_shell(runner), HOST, "dmd-1", "main", "claude", [])


def test_start_reports_missing_branch() -> None:
    runner = FakeRunner(
        {"tmux has-session": fail(""), "test -e": fail(""), "fetch": fail("couldn't find remote ref x")}
    )
    with pytest.raises(RemoteError, match="couldn't find remote ref"):
        start_task(_shell(runner), HOST, "dmd-1", "x", "claude", [])


def test_list_combines_sessions_and_worktrees() -> None:
    runner = FakeRunner(
        {"list-sessions": out("agentbox-a\nother\n"), "ls -1": out("a\nb\n")}
    )
    assert list_tasks(_shell(runner), HOST) == [TaskStatus("a", True), TaskStatus("b", False)]


def test_list_without_tmux_server() -> None:
    runner = FakeRunner(
        {"list-sessions": fail("no server running on /tmp/tmux-1000/default"), "ls -1": fail("")}
    )
    assert list_tasks(_shell(runner), HOST) == []


def test_logs_capture_pane() -> None:
    runner = FakeRunner({"capture-pane": out("line\n")})
    assert task_logs(_shell(runner), "a", 50) == "line\n"
    assert "tmux capture-pane -p -t agentbox-a -S -50" in runner.remote_commands()


def test_stop_keeps_worktree_by_default() -> None:
    runner = FakeRunner()
    stop_task(_shell(runner), HOST, "a", remove_worktree=False)
    cmds = runner.remote_commands()
    assert "tmux kill-session -t agentbox-a" in cmds
    assert "docker rm -f agentbox-task-a" in cmds
    assert not any("worktree remove" in c for c in cmds)


def test_stop_refuses_dirty_worktree() -> None:
    runner = FakeRunner({"status --porcelain": out(" M file.py\n")})
    with pytest.raises(RemoteError, match="uncommitted"):
        stop_task(_shell(runner), HOST, "a", remove_worktree=True)
    assert not any("worktree remove" in c for c in runner.remote_commands())


def test_stop_removes_clean_worktree() -> None:
    runner = FakeRunner({"status --porcelain": out("")})
    stop_task(_shell(runner), HOST, "a", remove_worktree=True)
    assert "git -C /srv/ab/r/repo worktree remove /srv/ab/r/wt/a" in runner.remote_commands()
```

- [ ] **Step 2: Run to verify failure** — `.venv/bin/pytest tests/test_remote_tasks.py -q` → FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Implement** — `src/agentbox/remote_tasks.py`

```python
"""Agent tasks on a remote host: one git worktree and one tmux session per task."""

from __future__ import annotations

import re
import shlex
from collections.abc import Sequence
from dataclasses import dataclass

from .exceptions import ConfigError, RemoteError
from .remote_registry import RemoteHost
from .remote_ssh import RemoteShell
from .service import validate_name

SESSION_PREFIX = "agentbox-"
CONTAINER_PREFIX = "agentbox-task-"
_BRANCH = re.compile(r"[A-Za-z0-9._/-]+")


@dataclass(frozen=True)
class TaskStatus:
    name: str
    running: bool


def session_name(task: str) -> str:
    return f"{SESSION_PREFIX}{task}"


def container_name(task: str) -> str:
    return f"{CONTAINER_PREFIX}{task}"


def _validate_task(task: str) -> None:
    try:
        validate_name(task)
    except ConfigError:
        raise ConfigError(f"Invalid task name: {task}") from None


def _validate_branch(branch: str) -> None:
    if branch.startswith("-") or not _BRANCH.fullmatch(branch):
        raise ConfigError(f"Invalid branch name: {branch}")


def _worktree(host: RemoteHost, task: str) -> str:
    return f"{host.worktrees_dir}/{task}"


def start_task(
    shell: RemoteShell,
    host: RemoteHost,
    task: str,
    branch: str,
    agent: str,
    agent_args: Sequence[str],
) -> None:
    _validate_task(task)
    _validate_branch(branch)
    worktree = _worktree(host, task)
    if shell.run(["tmux", "has-session", "-t", session_name(task)]).returncode == 0:
        raise RemoteError(f"Task {task} is already running")
    if shell.run(["test", "-e", worktree]).returncode == 0:
        raise RemoteError(
            f"Worktree {worktree} already exists; stop it with --remove-worktree or pick another name"
        )
    shell.check(["git", "-C", host.repo_dir, "fetch", "origin", branch], error=f"Cannot fetch {branch}")
    shell.check(
        ["git", "-C", host.repo_dir, "worktree", "add", worktree, branch],
        error=f"Cannot create worktree for {branch}",
    )
    inner = shlex.join(
        ["agentbox", "run", worktree, "--agent", agent, "--name", container_name(task), "--", *agent_args]
    )
    shell.check(
        ["tmux", "new-session", "-d", "-s", session_name(task), "-c", worktree, "bash", "-lc", inner],
        error=f"Cannot start task {task}",
    )


def list_tasks(shell: RemoteShell, host: RemoteHost) -> list[TaskStatus]:
    sessions = shell.run(["tmux", "list-sessions", "-F", "#{session_name}"])
    running = {
        line[len(SESSION_PREFIX) :]
        for line in sessions.stdout.splitlines()
        if sessions.returncode == 0 and line.startswith(SESSION_PREFIX)
    }
    worktrees = shell.run(["ls", "-1", host.worktrees_dir])
    names = set(worktrees.stdout.split()) if worktrees.returncode == 0 else set()
    return [TaskStatus(name, name in running) for name in sorted(names | running)]


def attach_task(shell: RemoteShell, task: str) -> int:
    _validate_task(task)
    return shell.interactive(["tmux", "attach", "-t", session_name(task)])


def task_logs(shell: RemoteShell, task: str, tail: int) -> str:
    _validate_task(task)
    return shell.check(
        ["tmux", "capture-pane", "-p", "-t", session_name(task), "-S", f"-{tail}"],
        error=f"Cannot read output of task {task}",
    )


def stop_task(shell: RemoteShell, host: RemoteHost, task: str, *, remove_worktree: bool) -> None:
    _validate_task(task)
    worktree = _worktree(host, task)
    if remove_worktree:
        dirty = shell.check(
            ["git", "-C", worktree, "status", "--porcelain"], error=f"Cannot inspect {worktree}"
        )
        if dirty.strip():
            raise RemoteError(f"Worktree {worktree} has uncommitted changes; commit or push them first")
    shell.run(["tmux", "kill-session", "-t", session_name(task)])
    # Killing tmux may leave the foreground container running.
    shell.run([host.runtime, "rm", "-f", container_name(task)])
    if remove_worktree:
        shell.check(
            ["git", "-C", host.repo_dir, "worktree", "remove", worktree],
            error=f"Cannot remove {worktree}",
        )
```

- [ ] **Step 4: Run tests** — `.venv/bin/pytest tests/test_remote_tasks.py -q` → PASS.

- [ ] **Step 5: Lint, type-check, commit**

```bash
.venv/bin/ruff check src/agentbox tests && .venv/bin/ruff format --check src/agentbox tests && .venv/bin/mypy src/agentbox
git add src/agentbox/remote_tasks.py tests/test_remote_tasks.py
git commit -m "feat: add remote task lifecycle"
```

---

### Task 5: Remote Hermes services

**Files:**
- Create: `src/agentbox/remote_services.py`
- Test: `tests/test_remote_services.py`

**Interfaces:**
- Consumes: `RemoteShell`, `RemoteHost`, `validate_name`.
- Produces: `RESTART_POLICIES = ("no", "on-failure:3", "unless-stopped")`; `setup_service(shell, host) -> int`; `start_service(shell, host, name, restart_policy, forwarded_env: Sequence[str]) -> str`; `service_status(shell, name) -> str`; `service_logs(shell, name, tail: int) -> str`; `stop_service(shell, name) -> str`.

- [ ] **Step 1: Write the failing tests** — `tests/test_remote_services.py`

```python
import pytest

from agentbox.exceptions import ConfigError
from agentbox.remote_registry import RemoteHost
from agentbox.remote_services import (
    service_logs,
    service_status,
    setup_service,
    start_service,
    stop_service,
)
from agentbox.remote_ssh import RemoteShell
from tests.remote_fakes import FakeRunner, out

HOST = RemoteHost(
    ssh="vm", repository="git@github.com:o/r.git", base_dir="/srv/ab/r", agent="claude", runtime="docker"
)


def test_setup_runs_gateway_setup_with_tty() -> None:
    runner = FakeRunner()
    setup_service(RemoteShell("vm", runner), HOST)
    call = runner.calls[-1]
    assert call.tty is True
    assert call.remote == "agentbox run /srv/ab/r/repo --agent hermes -- gateway setup"


def test_start_passes_policy_and_env_names() -> None:
    runner = FakeRunner({"service start": out("Created bot: abc\n")})
    result = start_service(RemoteShell("vm", runner), HOST, "bot", "unless-stopped", ["OPENAI_API_KEY"])
    assert result == "Created bot: abc\n"
    assert runner.remote_commands()[-1] == (
        "agentbox service start /srv/ab/r/repo --name bot --restart-policy unless-stopped"
        " --env OPENAI_API_KEY"
    )


def test_status_logs_stop_pass_through() -> None:
    runner = FakeRunner({"service status": out("bot: running\n")})
    shell = RemoteShell("vm", runner)
    assert service_status(shell, "bot") == "bot: running\n"
    service_logs(shell, "bot", 20)
    stop_service(shell, "bot")
    assert runner.remote_commands()[1:] == [
        "agentbox service logs bot --tail 20",
        "agentbox service stop bot",
    ]


def test_invalid_service_name_rejected_before_ssh() -> None:
    runner = FakeRunner()
    with pytest.raises(ConfigError):
        service_status(RemoteShell("vm", runner), "--all")
    assert runner.calls == []


def test_invalid_restart_policy_rejected() -> None:
    with pytest.raises(ConfigError):
        start_service(RemoteShell("vm", FakeRunner()), HOST, "bot", "always", [])
```

- [ ] **Step 2: Run to verify failure** — `.venv/bin/pytest tests/test_remote_services.py -q` → FAIL.

- [ ] **Step 3: Implement** — `src/agentbox/remote_services.py`

```python
"""Hermes gateway services on a remote host, via the existing `agentbox service`."""

from __future__ import annotations

from collections.abc import Sequence

from .exceptions import ConfigError
from .remote_registry import RemoteHost
from .remote_ssh import RemoteShell
from .service import validate_name

RESTART_POLICIES = ("no", "on-failure:3", "unless-stopped")


def setup_service(shell: RemoteShell, host: RemoteHost) -> int:
    return shell.interactive(
        ["agentbox", "run", host.repo_dir, "--agent", "hermes", "--", "gateway", "setup"]
    )


def start_service(
    shell: RemoteShell,
    host: RemoteHost,
    name: str,
    restart_policy: str,
    forwarded_env: Sequence[str],
) -> str:
    validate_name(name)
    if restart_policy not in RESTART_POLICIES:
        raise ConfigError(f"Invalid restart policy: {restart_policy}")
    env_args = [arg for key in forwarded_env for arg in ("--env", key)]
    return shell.check(
        [
            "agentbox", "service", "start", host.repo_dir,
            "--name", name, "--restart-policy", restart_policy, *env_args,
        ],
        error=f"Cannot start service {name}",
    )


def service_status(shell: RemoteShell, name: str) -> str:
    validate_name(name)
    return shell.check(["agentbox", "service", "status", name], error=f"Cannot inspect {name}")


def service_logs(shell: RemoteShell, name: str, tail: int) -> str:
    validate_name(name)
    return shell.check(
        ["agentbox", "service", "logs", name, "--tail", str(tail)], error=f"Cannot read logs of {name}"
    )


def stop_service(shell: RemoteShell, name: str) -> str:
    validate_name(name)
    return shell.check(["agentbox", "service", "stop", name], error=f"Cannot stop {name}")
```

- [ ] **Step 4: Run tests** — `.venv/bin/pytest tests/test_remote_services.py -q` → PASS.

- [ ] **Step 5: Lint, type-check, commit**

```bash
.venv/bin/ruff check src/agentbox tests && .venv/bin/ruff format --check src/agentbox tests && .venv/bin/mypy src/agentbox
git add src/agentbox/remote_services.py tests/test_remote_services.py
git commit -m "feat: add remote hermes service commands"
```

---

### Task 6: Setup wizard and doctor

**Files:**
- Create: `src/agentbox/remote_setup.py`
- Test: `tests/test_remote_setup.py`

**Interfaces:**
- Consumes: `RemoteShell`, `RemoteHost`, `save_host`, `load_registry`, `RemoteError`.
- Produces: `StepResult(name: str, status: Literal["ok", "fixed", "action", "info"], detail: str)`; `Prompter` protocol (`ask(text: str, default: str | None = None) -> str`, `confirm(text: str) -> bool`); `WheelBuilder = Callable[[], Path | None]`; `local_wheel() -> Path | None`; `run_setup(name: str, shell: RemoteShell, prompter: Prompter, *, local_version: str, wheel_builder: WheelBuilder, registry: Path | None = None) -> list[StepResult]`; `run_doctor(host: RemoteHost, shell: RemoteShell, *, local_version: str) -> list[StepResult]`.

- [ ] **Step 1: Write the failing tests** — `tests/test_remote_setup.py`

```python
from pathlib import Path

import pytest
import yaml

from agentbox.exceptions import RemoteError
from agentbox.remote_registry import RemoteHost, get_host, save_host
from agentbox.remote_setup import StepResult, run_doctor, run_setup
from agentbox.remote_ssh import RemoteShell
from tests.remote_fakes import FakeRunner, fail, out

VERSION = "0.5.0a1"


class ScriptedPrompter:
    def __init__(self, answers: dict[str, str] | None = None, confirm: bool = True) -> None:
        self.answers = answers or {}
        self.confirm_value = confirm
        self.asked: list[str] = []
        self.confirmed: list[str] = []

    def ask(self, text: str, default: str | None = None) -> str:
        self.asked.append(text)
        for key, value in self.answers.items():
            if key in text:
                return value
        assert default is not None, f"unexpected prompt: {text}"
        return default

    def confirm(self, text: str) -> bool:
        self.confirmed.append(text)
        return self.confirm_value


ANSWERS = {"repository": "git@github.com:o/r.git", "base_dir": "/srv/ab/r", "agent": "claude"}


def _healthy(**overrides: object) -> FakeRunner:
    responses = {
        "podman info": fail("no podman"),
        "agentbox --version": out(f"agentbox, version {VERSION}\n"),
        "remote get-url origin": out("git@github.com:o/r.git\n"),
        "cat .config/agentbox/config.yaml": out(
            yaml.safe_dump({"runtime": "docker", "credentials": {"github": True}, "state_scope": "repository"})
        ),
    }
    responses.update(overrides)  # type: ignore[arg-type]
    return FakeRunner(responses)


def _statuses(results: list[StepResult]) -> dict[str, str]:
    return {r.name: r.status for r in results}


def test_ssh_failure_stops_immediately(tmp_path: Path) -> None:
    runner = FakeRunner({"true": fail("Permission denied (publickey)")})
    results = run_setup("vm", RemoteShell("vm", runner), ScriptedPrompter(ANSWERS),
                        local_version=VERSION, wheel_builder=lambda: None, registry=tmp_path / "r.yaml")
    assert results[-1].status == "action" and "Permission denied" in results[-1].detail
    assert len(runner.calls) == 1


def test_missing_runtime_is_action_without_sudo(tmp_path: Path) -> None:
    runner = _healthy(**{"docker info": fail("permission denied")})
    results = run_setup("vm", RemoteShell("vm", runner), ScriptedPrompter(ANSWERS),
                        local_version=VERSION, wheel_builder=lambda: None, registry=tmp_path / "r.yaml")
    assert results[-1].name == "runtime" and results[-1].status == "action"
    assert not any("sudo" in c for c in runner.remote_commands())


def test_setup_is_idempotent(tmp_path: Path) -> None:
    registry = tmp_path / "r.yaml"
    save_host("vm", RemoteHost(ssh="vm", repository="git@github.com:o/r.git", base_dir="/srv/ab/r",
                               agent="claude", runtime="docker"), registry)
    prompter = ScriptedPrompter()
    runner = _healthy()
    results = run_setup("vm", RemoteShell("vm", runner), prompter,
                        local_version=VERSION, wheel_builder=lambda: None, registry=registry)
    assert prompter.confirmed == []
    assert all(r.status in {"ok", "info"} for r in results)
    assert not any(c.input is not None for c in runner.calls)
    assert not any("git clone" in c for c in runner.remote_commands())


def test_version_mismatch_installs_then_verifies(tmp_path: Path) -> None:
    # The fake keeps reporting the old version, so post-install verification must fail loudly.
    runner = _healthy(**{"agentbox --version": out("agentbox, version 0.4.0\n")})
    with pytest.raises(RemoteError, match="still differs"):
        run_setup("vm", RemoteShell("vm", runner), ScriptedPrompter(ANSWERS),
                  local_version=VERSION, wheel_builder=lambda: None, registry=tmp_path / "r.yaml")
    assert f"pipx install --force agentbox=={VERSION}" in runner.remote_commands()


def test_wheel_is_streamed_not_copied_with_scp(tmp_path: Path) -> None:
    wheel = tmp_path / "agentbox-0.5.0a1-py3-none-any.whl"
    wheel.write_bytes(b"wheel-bytes")
    runner = _healthy(**{"agentbox --version": fail("command not found")})
    with pytest.raises(RemoteError):
        run_setup("vm", RemoteShell("vm", runner), ScriptedPrompter(ANSWERS),
                  local_version=VERSION, wheel_builder=lambda: wheel, registry=tmp_path / "r.yaml")
    uploads = [c for c in runner.calls if c.input is not None]
    assert uploads and "base64 -d" in uploads[0].remote
    assert f"pipx install --force .cache/agentbox/{wheel.name}" in runner.remote_commands()
    assert not any(c.argv[0] == "scp" for c in runner.calls)


def test_declined_install_is_action(tmp_path: Path) -> None:
    runner = _healthy(**{"agentbox --version": fail("command not found")})
    results = run_setup("vm", RemoteShell("vm", runner), ScriptedPrompter(ANSWERS, confirm=False),
                        local_version=VERSION, wheel_builder=lambda: None, registry=tmp_path / "r.yaml")
    assert results[-1].name == "agentbox" and results[-1].status == "action"


def test_gh_not_logged_in_prints_command_without_token(tmp_path: Path) -> None:
    runner = _healthy(**{"gh auth status": fail("not logged in")})
    results = run_setup("vm", RemoteShell("vm", runner), ScriptedPrompter(ANSWERS),
                        local_version=VERSION, wheel_builder=lambda: None, registry=tmp_path / "r.yaml")
    assert results[-1].name == "github" and results[-1].status == "action"
    assert "ssh -t vm gh auth login --hostname github.com --web" in results[-1].detail


def test_clones_missing_repository(tmp_path: Path) -> None:
    runner = _healthy(**{"test -d /srv/ab/r/repo/.git": fail("")})
    run_setup("vm", RemoteShell("vm", runner), ScriptedPrompter(ANSWERS),
              local_version=VERSION, wheel_builder=lambda: None, registry=tmp_path / "r.yaml")
    assert "git clone git@github.com:o/r.git /srv/ab/r/repo" in runner.remote_commands()


def test_remote_config_conflict_not_overwritten(tmp_path: Path) -> None:
    runner = _healthy(**{"cat .config/agentbox/config.yaml": out("runtime: podman\n")})
    results = run_setup("vm", RemoteShell("vm", runner), ScriptedPrompter(ANSWERS),
                        local_version=VERSION, wheel_builder=lambda: None, registry=tmp_path / "r.yaml")
    assert results[-1].name == "remote-config" and results[-1].status == "action"
    assert "runtime" in results[-1].detail
    assert not any(c.input is not None for c in runner.calls)


def test_remote_config_adds_missing_keys_after_confirmation(tmp_path: Path) -> None:
    runner = _healthy(**{"cat .config/agentbox/config.yaml": fail("No such file")})
    run_setup("vm", RemoteShell("vm", runner), ScriptedPrompter(ANSWERS),
              local_version=VERSION, wheel_builder=lambda: None, registry=tmp_path / "r.yaml")
    written = [c.input for c in runner.calls if c.input is not None]
    assert yaml.safe_load(written[-1]) == {
        "runtime": "docker", "credentials": {"github": True}, "state_scope": "repository"
    }


def test_successful_setup_saves_registry(tmp_path: Path) -> None:
    registry = tmp_path / "r.yaml"
    results = run_setup("vm", RemoteShell("vm", _healthy()), ScriptedPrompter(ANSWERS),
                        local_version=VERSION, wheel_builder=lambda: None, registry=registry)
    assert _statuses(results)["save"] == "fixed"
    assert get_host("vm", registry).runtime == "docker"


def test_doctor_never_writes(tmp_path: Path) -> None:
    host = RemoteHost(ssh="vm", repository="git@github.com:o/r.git", base_dir="/srv/ab/r",
                      agent="claude", runtime="docker")
    runner = _healthy(**{"agentbox --version": out("agentbox, version 0.4.0\n"),
                         "cat .config/agentbox/config.yaml": fail("No such file")})
    results = run_doctor(host, RemoteShell("vm", runner), local_version=VERSION)
    assert _statuses(results)["agentbox"] == "action"
    assert not any(c.input is not None for c in runner.calls)
    assert not any(word in c for c in runner.remote_commands() for word in ("pipx", "clone", "build"))
```

- [ ] **Step 2: Run to verify failure** — `.venv/bin/pytest tests/test_remote_setup.py -q` → FAIL.

- [ ] **Step 3: Implement** — `src/agentbox/remote_setup.py`

```python
"""Remote host setup wizard and read-only doctor."""

from __future__ import annotations

import base64
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol, cast

import yaml

from .exceptions import RemoteError
from .remote_registry import RemoteHost, load_registry, save_host
from .remote_ssh import RemoteShell

Status = Literal["ok", "fixed", "action", "info"]
WheelBuilder = Callable[[], "Path | None"]
REMOTE_CONFIG = ".config/agentbox/config.yaml"
WHEEL_DIR = ".cache/agentbox"
TOOLS = ("git", "tmux", "pipx")


@dataclass(frozen=True)
class StepResult:
    name: str
    status: Status
    detail: str


class Prompter(Protocol):
    def ask(self, text: str, default: str | None = None) -> str: ...

    def confirm(self, text: str) -> bool: ...


def local_wheel() -> Path | None:
    root = Path(__file__).resolve().parents[2]
    if not (root / "pyproject.toml").exists():
        return None
    target = Path(tempfile.mkdtemp(prefix="agentbox-wheel-"))
    subprocess.run(
        [sys.executable, "-m", "pip", "wheel", "--no-deps", "-q", "-w", str(target), str(root)],
        check=True,
        capture_output=True,
    )
    return next(target.glob("agentbox-*.whl"))


def _ssh(shell: RemoteShell) -> StepResult:
    result = shell.run(["true"])
    if result.returncode != 0:
        return StepResult("ssh", "action", result.stderr.strip() or f"exit {result.returncode}")
    return StepResult("ssh", "ok", shell.destination)


def _runtime(shell: RemoteShell, expected: str | None) -> tuple[StepResult, str | None]:
    candidates = [expected] if expected else ["podman", "docker"]
    for runtime in candidates:
        if runtime and shell.run([runtime, "info"]).returncode == 0:
            return StepResult("runtime", "ok", runtime), runtime
    return (
        StepResult(
            "runtime",
            "action",
            "Install Podman (e.g. `sudo apt-get install -y podman`) or give the SSH user Docker "
            "access (`sudo usermod -aG docker $USER`, or `gpasswd -a` for OS Login users), then re-run",
        ),
        None,
    )


def _tools(shell: RemoteShell) -> StepResult:
    missing = [tool for tool in TOOLS if shell.run(["command", "-v", tool]).returncode != 0]
    if missing:
        return StepResult(
            "tools", "action", f"Install on the remote: {' '.join(missing)} (e.g. `sudo apt-get install -y ...`)"
        )
    return StepResult("tools", "ok", ", ".join(TOOLS))


def _remote_version(shell: RemoteShell) -> str | None:
    result = shell.run(["agentbox", "--version"])
    return result.stdout.strip().split()[-1] if result.returncode == 0 and result.stdout.strip() else None


def _agentbox(
    shell: RemoteShell, prompter: Prompter | None, local_version: str, wheel_builder: WheelBuilder | None
) -> StepResult:
    remote = _remote_version(shell)
    if remote == local_version:
        return StepResult("agentbox", "ok", local_version)
    found = remote or "not installed"
    if prompter is None or wheel_builder is None:
        return StepResult("agentbox", "action", f"remote {found}, laptop {local_version}; run remote setup")
    if not prompter.confirm(f"Install agentbox {local_version} on the remote (currently {found})?"):
        return StepResult("agentbox", "action", f"remote {found}, laptop {local_version}")
    wheel = wheel_builder()
    if wheel is None:
        shell.check(["pipx", "install", "--force", f"agentbox=={local_version}"], error="pipx install failed")
    else:
        remote_wheel = f"{WHEEL_DIR}/{wheel.name}"
        shell.check(
            ["bash", "-c", 'mkdir -p "$(dirname "$1")" && base64 -d > "$1"', "_", remote_wheel],
            error="Cannot upload wheel",
            input=base64.b64encode(wheel.read_bytes()).decode(),
        )
        shell.check(["pipx", "install", "--force", remote_wheel], error="pipx install failed")
    if _remote_version(shell) != local_version:
        raise RemoteError("agentbox version on the remote still differs after install")
    return StepResult("agentbox", "fixed", local_version)


def _github(shell: RemoteShell) -> StepResult:
    if shell.run(["gh", "auth", "status"]).returncode == 0:
        return StepResult("github", "ok", "gh logged in")
    return StepResult(
        "github",
        "action",
        f"Run in your terminal: ssh -t {shell.destination} gh auth login --hostname github.com --web",
    )


def _repository(shell: RemoteShell, repository: str, base_dir: str, *, write: bool) -> StepResult:
    repo = f"{base_dir}/repo"
    if shell.run(["test", "-d", f"{repo}/.git"]).returncode == 0:
        origin = shell.check(["git", "-C", repo, "remote", "get-url", "origin"], error="Cannot read origin")
        if origin.strip() != repository:
            return StepResult("repository", "action", f"{repo} has origin {origin.strip()}, expected {repository}")
        return StepResult("repository", "ok", repo)
    if not write:
        return StepResult("repository", "action", f"{repo} missing; run remote setup")
    shell.check(["mkdir", "-p", f"{base_dir}/wt"], error=f"Cannot create {base_dir}")
    shell.check(["git", "clone", repository, repo], error=f"Cannot clone {repository}")
    return StepResult("repository", "fixed", repo)


def _desired_config(runtime: str) -> dict[str, Any]:
    return {"runtime": runtime, "credentials": {"github": True}, "state_scope": "repository"}


def _merge(current: dict[str, Any], desired: dict[str, Any], prefix: str = "") -> tuple[dict[str, Any], list[str], list[str]]:
    merged = dict(current)
    missing: list[str] = []
    conflicts: list[str] = []
    for key, value in desired.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict):
            nested, nested_missing, nested_conflicts = _merge(dict(current.get(key) or {}), value, f"{path}.")
            merged[key] = nested
            missing += nested_missing
            conflicts += nested_conflicts
        elif key not in current:
            merged[key] = value
            missing.append(path)
        elif current[key] != value:
            conflicts.append(f"{path} is {current[key]!r}, needs {value!r}")
    return merged, missing, conflicts


def _remote_config(shell: RemoteShell, prompter: Prompter | None, runtime: str) -> StepResult:
    result = shell.run(["cat", REMOTE_CONFIG])
    current = (yaml.safe_load(result.stdout) or {}) if result.returncode == 0 else {}
    merged, missing, conflicts = _merge(current, _desired_config(runtime))
    if conflicts:
        return StepResult("remote-config", "action", f"Edit ~/{REMOTE_CONFIG} on the remote: " + "; ".join(conflicts))
    if not missing:
        return StepResult("remote-config", "ok", f"~/{REMOTE_CONFIG}")
    if prompter is None or not prompter.confirm(f"Add {', '.join(missing)} to remote ~/{REMOTE_CONFIG}?"):
        return StepResult("remote-config", "action", f"missing {', '.join(missing)}")
    shell.write_file(REMOTE_CONFIG, yaml.safe_dump(merged, sort_keys=False))
    return StepResult("remote-config", "fixed", ", ".join(missing))


def _image(shell: RemoteShell, repo: str, agent: str) -> StepResult:
    shell.check(
        ["bash", "-c", 'cd "$1" && agentbox build --agent "$2"', "_", repo, agent],
        error=f"Cannot build the {agent} image",
    )
    return StepResult("image", "ok", agent)


def _login_hint(shell: RemoteShell, repo: str, agent: str) -> StepResult:
    return StepResult(
        "login",
        "info",
        f"Log the agent in once: ssh -t {shell.destination} agentbox run {repo} --agent {agent}",
    )


def run_setup(
    name: str,
    shell: RemoteShell,
    prompter: Prompter,
    *,
    local_version: str,
    wheel_builder: WheelBuilder,
    registry: Path | None = None,
) -> list[StepResult]:
    existing = load_registry(registry).get(name)
    results: list[StepResult] = []

    def stop() -> bool:
        return results[-1].status == "action"

    results.append(_ssh(shell))
    if stop():
        return results
    runtime_result, runtime = _runtime(shell, existing.runtime if existing else None)
    results.append(runtime_result)
    if stop() or runtime is None:
        return results
    for step in (lambda: _tools(shell), lambda: _agentbox(shell, prompter, local_version, wheel_builder), lambda: _github(shell)):
        results.append(step())
        if stop():
            return results
    repository = existing.repository if existing else prompter.ask("Git repository URL (repository)")
    base_dir = existing.base_dir if existing else prompter.ask("Absolute directory on the remote (base_dir)")
    agent = existing.agent if existing else prompter.ask("Default agent (agent)")
    host = RemoteHost(
        ssh=shell.destination,
        repository=repository,
        base_dir=base_dir,
        agent=agent,
        runtime=cast(Literal["podman", "docker"], runtime),
    )
    for step in (
        lambda: _repository(shell, host.repository, host.base_dir, write=True),
        lambda: _remote_config(shell, prompter, runtime),
        lambda: _image(shell, host.repo_dir, host.agent),
    ):
        results.append(step())
        if stop():
            return results
    if existing != host:
        save_host(name, host, registry)
        results.append(StepResult("save", "fixed", name))
    else:
        results.append(StepResult("save", "ok", name))
    results.append(_login_hint(shell, host.repo_dir, host.agent))
    return results


def run_doctor(host: RemoteHost, shell: RemoteShell, *, local_version: str) -> list[StepResult]:
    results = [_ssh(shell)]
    if results[-1].status == "action":
        return results
    runtime_result, _ = _runtime(shell, host.runtime)
    results += [
        runtime_result,
        _tools(shell),
        _agentbox(shell, None, local_version, None),
        _github(shell),
        _repository(shell, host.repository, host.base_dir, write=False),
        _remote_config(shell, None, host.runtime),
        _login_hint(shell, host.repo_dir, host.agent),
    ]
    return results
```

Note for the implementer: `_image` returns `ok` after a successful build (the build itself is cached by the engine); the idempotency test accepts `ok` and `info`.

- [ ] **Step 4: Run tests** — `.venv/bin/pytest tests/test_remote_setup.py -q` → PASS. Fix implementation (not tests) until green; if a test contradicts the spec, report it.

- [ ] **Step 5: Lint, type-check, commit**

```bash
.venv/bin/ruff check src/agentbox tests && .venv/bin/ruff format src/agentbox tests && .venv/bin/mypy src/agentbox
git add src/agentbox/remote_setup.py tests/test_remote_setup.py
git commit -m "feat: add remote setup wizard and doctor"
```

---

### Task 7: CLI wiring

**Files:**
- Create: `src/agentbox/remote_cli.py`
- Modify: `src/agentbox/cli.py` (imports; skip-set in `main`; `main.add_command(remote)`)
- Test: `tests/test_remote_cli.py`

**Interfaces:**
- Consumes: everything from Tasks 2-6.
- Produces: click group `remote` with commands `setup`, `doctor`, `run`, `list`, `attach`, `logs`, `stop`, and group `service` (`setup`, `start`, `status`, `logs`, `stop`). `remote_cli.make_shell(host: RemoteHost) -> RemoteShell` is the single place that builds shells (tests patch it).

- [ ] **Step 1: Write the failing tests** — `tests/test_remote_cli.py`

```python
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from agentbox.cli import main
from agentbox.remote_registry import RemoteHost, save_host
from agentbox.remote_ssh import RemoteShell
from tests.remote_fakes import FakeRunner, fail, out


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    save_host("vm", RemoteHost(ssh="vm", repository="git@github.com:o/r.git", base_dir="/srv/ab/r",
                               agent="claude", runtime="docker"))
    return home


def _invoke(runner: FakeRunner, args: list[str]):  # type: ignore[no-untyped-def]
    with patch("agentbox.remote_cli.make_shell", lambda host: RemoteShell(host.ssh, runner)):
        return CliRunner().invoke(main, ["remote", *args])


def test_run_uses_host_default_agent() -> None:
    runner = FakeRunner({"tmux has-session": fail(""), "test -e": fail("")})
    result = _invoke(runner, ["run", "vm", "--name", "t1", "--branch", "main", "--", "do it"])
    assert result.exit_code == 0, result.output
    assert "--agent claude" in runner.remote_commands()[-1]


def test_list_prints_status() -> None:
    runner = FakeRunner({"list-sessions": out("agentbox-t1\n"), "ls -1": out("t1\nt2\n")})
    result = _invoke(runner, ["list", "vm"])
    assert result.exit_code == 0
    assert "t1\trunning" in result.output and "t2\tstopped" in result.output


def test_unknown_host_is_clear_error() -> None:
    result = _invoke(FakeRunner(), ["list", "other"])
    assert result.exit_code != 0
    # `main` re-raises AgentboxError; the console entry point `cli` prints it.
    assert "agentbox remote setup other" in result.output + str(result.exception)


def test_doctor_exit_code_reflects_actions() -> None:
    runner = FakeRunner({"gh auth status": fail("no"), "agentbox --version": out("agentbox, version 0\n")})
    result = _invoke(runner, ["doctor", "vm"])
    assert result.exit_code == 1
    assert "ACTION" in result.output


def test_service_start_passes_policy() -> None:
    runner = FakeRunner()
    result = _invoke(runner, ["service", "start", "vm", "bot", "--restart-policy", "unless-stopped"])
    assert result.exit_code == 0, result.output
    assert "--restart-policy unless-stopped" in runner.remote_commands()[-1]
```

- [ ] **Step 2: Run to verify failure** — `.venv/bin/pytest tests/test_remote_cli.py -q` → FAIL (`No such command 'remote'`).

- [ ] **Step 3: Implement** — `src/agentbox/remote_cli.py`

```python
"""`agentbox remote`: run tasks and services on an SSH host."""

from __future__ import annotations

import sys

import click

from . import __version__
from .remote_registry import RemoteHost, get_host, load_registry
from .remote_services import (
    RESTART_POLICIES,
    service_logs,
    service_status,
    setup_service,
    start_service,
    stop_service,
)
from .remote_setup import StepResult, local_wheel, run_doctor, run_setup
from .remote_ssh import RemoteShell
from .remote_tasks import attach_task, list_tasks, start_task, stop_task, task_logs


class ClickPrompter:
    def ask(self, text: str, default: str | None = None) -> str:
        return str(click.prompt(text, default=default))

    def confirm(self, text: str) -> bool:
        return click.confirm(text, default=False)


def make_shell(host: RemoteHost) -> RemoteShell:
    return RemoteShell(host.ssh)


def _report(results: list[StepResult]) -> None:
    for result in results:
        click.echo(f"{result.status.upper():6} {result.name}: {result.detail}")
    if any(result.status == "action" for result in results):
        sys.exit(1)


@click.group()
def remote() -> None:
    """Run agent tasks and services on an SSH host."""


@remote.command()
@click.argument("host")
@click.option("--ssh", "ssh_destination", help="ssh destination; defaults to HOST for a new remote")
def setup(host: str, ssh_destination: str | None) -> None:
    """Prepare and validate a remote host (re-runnable)."""
    existing = load_registry().get(host)
    destination = ssh_destination or (existing.ssh if existing else host)
    results = run_setup(
        host,
        RemoteShell(destination),
        ClickPrompter(),
        local_version=__version__,
        wheel_builder=local_wheel,
    )
    _report(results)


@remote.command()
@click.argument("host")
def doctor(host: str) -> None:
    """Validate a remote host without changing anything."""
    remote_host = get_host(host)
    _report(run_doctor(remote_host, make_shell(remote_host), local_version=__version__))


@remote.command(name="run", context_settings={"ignore_unknown_options": True})
@click.argument("host")
@click.option("--name", "task", required=True)
@click.option("--branch", required=True)
@click.option("--agent")
@click.argument("agent_args", nargs=-1, type=click.UNPROCESSED)
def run_task(host: str, task: str, branch: str, agent: str | None, agent_args: tuple[str, ...]) -> None:
    """Start a task from BRANCH in its own worktree and tmux session."""
    remote_host = get_host(host)
    start_task(make_shell(remote_host), remote_host, task, branch, agent or remote_host.agent, agent_args)
    click.echo(f"Started {task}. Attach: agentbox remote attach {host} {task}")


@remote.command(name="list")
@click.argument("host")
def list_command(host: str) -> None:
    """List tasks and whether they are running."""
    remote_host = get_host(host)
    for status in list_tasks(make_shell(remote_host), remote_host):
        click.echo(f"{status.name}\t{'running' if status.running else 'stopped'}")


@remote.command()
@click.argument("host")
@click.argument("task")
def attach(host: str, task: str) -> None:
    """Attach to a task's tmux session (detach with Ctrl+b d)."""
    sys.exit(attach_task(make_shell(get_host(host)), task))


@remote.command()
@click.argument("host")
@click.argument("task")
@click.option("--tail", type=click.IntRange(min=1), default=200)
def logs(host: str, task: str, tail: int) -> None:
    """Print recent task output."""
    click.echo(task_logs(make_shell(get_host(host)), task, tail), nl=False)


@remote.command()
@click.argument("host")
@click.argument("task")
@click.option("--remove-worktree", is_flag=True)
def stop(host: str, task: str, remove_worktree: bool) -> None:
    """Stop a task; keeps its worktree unless --remove-worktree."""
    remote_host = get_host(host)
    stop_task(make_shell(remote_host), remote_host, task, remove_worktree=remove_worktree)
    click.echo(f"Stopped {task}")


@remote.group()
def service() -> None:
    """Manage Hermes gateway services on a remote host."""


@service.command(name="setup")
@click.argument("host")
def service_setup(host: str) -> None:
    """Interactive Hermes gateway setup on the remote."""
    remote_host = get_host(host)
    sys.exit(setup_service(make_shell(remote_host), remote_host))


@service.command(name="start")
@click.argument("host")
@click.argument("name")
@click.option("--restart-policy", type=click.Choice(RESTART_POLICIES), default="on-failure:3")
@click.option("--env", "forwarded_env", multiple=True, help="Remote environment variable name to forward")
def service_start(host: str, name: str, restart_policy: str, forwarded_env: tuple[str, ...]) -> None:
    """Start a Hermes gateway service."""
    remote_host = get_host(host)
    click.echo(start_service(make_shell(remote_host), remote_host, name, restart_policy, forwarded_env), nl=False)


@service.command(name="status")
@click.argument("host")
@click.argument("name")
def service_status_command(host: str, name: str) -> None:
    """Show service status."""
    click.echo(service_status(make_shell(get_host(host)), name), nl=False)


@service.command(name="logs")
@click.argument("host")
@click.argument("name")
@click.option("--tail", type=click.IntRange(min=0), default=100)
def service_logs_command(host: str, name: str, tail: int) -> None:
    """Show service logs."""
    click.echo(service_logs(make_shell(get_host(host)), name, tail), nl=False)


@service.command(name="stop")
@click.argument("host")
@click.argument("name")
def service_stop(host: str, name: str) -> None:
    """Stop a service."""
    click.echo(stop_service(make_shell(get_host(host)), name), nl=False)
```

`src/agentbox/cli.py`:
- add `from .remote_cli import remote` next to `from .service_cli import service`;
- in `main`, change the skip set to `{"run", "state", "service", "doctor", "server", "remote"}`;
- add `main.add_command(remote)` after `main.add_command(server)`.

The console entry point `agentbox.cli:cli` catches `AgentboxError` and prints `Error: <message>` with exit 1; `RemoteError` subclasses `AgentboxError`, so remote failures follow the same path without extra handling.

- [ ] **Step 4: Run all tests** — `.venv/bin/pytest -q` → PASS.

- [ ] **Step 5: Lint, type-check, commit**

```bash
.venv/bin/ruff check src/agentbox tests && .venv/bin/ruff format src/agentbox tests && .venv/bin/mypy src/agentbox
git add src/agentbox/remote_cli.py src/agentbox/cli.py tests/test_remote_cli.py
git commit -m "feat: add agentbox remote commands"
```

---

### Task 8: Documentation

**Files:**
- Modify: `README.md` (CLI reference block + new section "Remote tasks" after "Hermes gateway service")
- Modify: `ROADMAP.md`, `CHANGELOG.md` (unreleased section), `AGENTS.md` (architecture list)
- Modify: `docs/specs/2026-09-25-remote-tasks-design.md`

- [ ] **Step 1: README** — add to the CLI reference block:

```
agentbox remote setup|doctor HOST          Prepare/validate an SSH host for remote tasks
agentbox remote run HOST --name T --branch B [-- ARGS]   Start a task in a worktree + tmux session
agentbox remote list|attach|logs|stop HOST [TASK]        Manage remote tasks
agentbox remote service setup|start|status|logs|stop HOST [NAME]   Hermes gateway on the remote
```

Then a "Remote tasks" section covering: purpose (start from laptop, runs on SSH host, laptop may close); prerequisites (Podman/Docker without sudo, git, tmux, pipx, `gh` login, outbound network; `deploy/gcp` is not a target); `agentbox remote setup HOST [--ssh DEST]` walkthrough with the step names `ssh, runtime, tools, agentbox, github, repository, remote-config, image, save, login`; the registry file and its five keys; task commands with an example; Hermes service commands; `state_scope: repository`; limits (no uncommitted-change transfer, no handoff, no nested Docker, no queue).

- [ ] **Step 2: ROADMAP, CHANGELOG, AGENTS.md**

- `ROADMAP.md`: add an item "Remote tasks and services over SSH (preview)" with status implemented, real-host validation pending.
- `CHANGELOG.md`: under an "Unreleased" heading: "Added `agentbox remote` (setup wizard, doctor, tasks, Hermes services) and `state_scope: repository`."
- `AGENTS.md` architecture list: add `remote_*.py: SSH transport, registry, tasks, services and setup wizard for remote hosts.`

- [ ] **Step 3: Align the spec with the implementation**

- Registry: five required keys (add `runtime`, detected by the wizard).
- Wizard step 4: the wheel is streamed over ssh (base64 on stdin), not `scp`.
- `remote service` commands take no `--agent` flag; the existing `agentbox service` is Hermes-only.
- `remote setup` accepts `--ssh DEST`; without it the remote name is used as the ssh destination for a new remote.
- Wizard step 9 is informational (`login`), not a blocking check; login is not verified automatically because agent credentials are not inspected.

- [ ] **Step 4: Verify and commit**

```bash
.venv/bin/pytest -q && .venv/bin/ruff check src/agentbox tests && .venv/bin/ruff format --check src/agentbox tests && .venv/bin/mypy src/agentbox
git add README.md ROADMAP.md CHANGELOG.md AGENTS.md docs/specs/2026-09-25-remote-tasks-design.md
git commit -m "docs: document remote tasks and services"
```

---

## Manual acceptance (after all tasks, on a real host, by the user)

1. `agentbox remote setup agent-runner` until all steps are OK/INFO; log Claude in once with the printed command.
2. `agentbox remote run agent-runner --name smoke --branch <existing-branch> -- "print the repo README title"`; `remote attach`, detach; close the laptop lid; reopen; `remote logs`.
3. `agentbox remote stop agent-runner smoke --remove-worktree`.
4. `agentbox remote service setup agent-runner hermes` then `service start ... --restart-policy unless-stopped`, `service status`, `service logs`.
