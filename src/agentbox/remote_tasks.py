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
            f"Worktree {worktree} already exists; "
            "stop it with --remove-worktree or pick another name"
        )
    shell.check(
        ["git", "-C", host.repo_dir, "fetch", "origin", branch], error=f"Cannot fetch {branch}"
    )
    shell.check(
        ["git", "-C", host.repo_dir, "worktree", "add", worktree, branch],
        error=f"Cannot create worktree for {branch}",
    )
    inner = shlex.join(
        [
            "agentbox",
            "run",
            worktree,
            "--agent",
            agent,
            "--name",
            container_name(task),
            "--",
            *agent_args,
        ]
    )
    shell.check(
        [
            "tmux",
            "new-session",
            "-d",
            "-s",
            session_name(task),
            "-c",
            worktree,
            "bash",
            "-lc",
            inner,
        ],
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
            raise RemoteError(
                f"Worktree {worktree} has uncommitted changes; commit or push them first"
            )
    shell.run(["tmux", "kill-session", "-t", session_name(task)])
    # Killing tmux may leave the foreground container running.
    shell.run([host.runtime, "rm", "-f", container_name(task)])
    if remove_worktree:
        shell.check(
            ["git", "-C", host.repo_dir, "worktree", "remove", worktree],
            error=f"Cannot remove {worktree}",
        )
