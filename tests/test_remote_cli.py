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
    save_host(
        "vm",
        RemoteHost(
            ssh="vm",
            repository="git@github.com:o/r.git",
            base_dir="/srv/ab/r",
            agent="claude",
            runtime="docker",
        ),
    )
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
    runner = FakeRunner(
        {"gh auth status": fail("no"), "agentbox --version": out("agentbox, version 0\n")}
    )
    result = _invoke(runner, ["doctor", "vm"])
    assert result.exit_code == 1
    assert "ACTION" in result.output


def test_service_start_passes_policy() -> None:
    runner = FakeRunner()
    result = _invoke(
        runner, ["service", "start", "vm", "bot", "--restart-policy", "unless-stopped"]
    )
    assert result.exit_code == 0, result.output
    assert "--restart-policy unless-stopped" in runner.remote_commands()[-1]
