import shlex

import pytest

from agentbox.exceptions import ConfigError, RemoteError
from agentbox.remote_registry import RemoteHost
from agentbox.remote_ssh import RemoteShell
from agentbox.remote_tasks import TaskStatus, list_tasks, start_task, stop_task, task_logs
from tests.remote_fakes import FakeRunner, fail, out

HOST = RemoteHost(
    ssh="vm",
    repository="git@github.com:o/r.git",
    base_dir="/srv/ab/r",
    agent="claude",
    runtime="docker",
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
    assert tmux[:7] == [
        "tmux",
        "new-session",
        "-d",
        "-s",
        "agentbox-dmd-1",
        "-c",
        "/srv/ab/r/wt/dmd-1",
    ]
    assert shlex.split(tmux[-1]) == [
        "agentbox",
        "run",
        "/srv/ab/r/wt/dmd-1",
        "--agent",
        "claude",
        "--name",
        "agentbox-task-dmd-1",
        "--",
        "do it",
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
        {
            "tmux has-session": fail(""),
            "test -e": fail(""),
            "fetch": fail("couldn't find remote ref x"),
        }
    )
    with pytest.raises(RemoteError, match="couldn't find remote ref"):
        start_task(_shell(runner), HOST, "dmd-1", "x", "claude", [])


def test_list_combines_sessions_and_worktrees() -> None:
    runner = FakeRunner({"list-sessions": out("agentbox-a\nother\n"), "ls -1": out("a\nb\n")})
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
