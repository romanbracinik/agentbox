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
            yaml.safe_dump(
                {"runtime": "docker", "credentials": {"github": True}, "state_scope": "repository"}
            )
        ),
    }
    responses.update(overrides)  # type: ignore[arg-type]
    return FakeRunner(responses)


def _statuses(results: list[StepResult]) -> dict[str, str]:
    return {r.name: r.status for r in results}


def test_ssh_failure_stops_immediately(tmp_path: Path) -> None:
    runner = FakeRunner({"true": fail("Permission denied (publickey)")})
    results = run_setup(
        "vm",
        RemoteShell("vm", runner),
        ScriptedPrompter(ANSWERS),
        local_version=VERSION,
        wheel_builder=lambda: None,
        registry=tmp_path / "r.yaml",
    )
    assert results[-1].status == "action" and "Permission denied" in results[-1].detail
    assert len(runner.calls) == 1


def test_missing_runtime_is_action_without_sudo(tmp_path: Path) -> None:
    runner = _healthy(**{"docker info": fail("permission denied")})
    results = run_setup(
        "vm",
        RemoteShell("vm", runner),
        ScriptedPrompter(ANSWERS),
        local_version=VERSION,
        wheel_builder=lambda: None,
        registry=tmp_path / "r.yaml",
    )
    assert results[-1].name == "runtime" and results[-1].status == "action"
    assert not any("sudo" in c for c in runner.remote_commands())


def test_setup_is_idempotent(tmp_path: Path) -> None:
    registry = tmp_path / "r.yaml"
    save_host(
        "vm",
        RemoteHost(
            ssh="vm",
            repository="git@github.com:o/r.git",
            base_dir="/srv/ab/r",
            agent="claude",
            runtime="docker",
        ),
        registry,
    )
    prompter = ScriptedPrompter()
    runner = _healthy()
    results = run_setup(
        "vm",
        RemoteShell("vm", runner),
        prompter,
        local_version=VERSION,
        wheel_builder=lambda: None,
        registry=registry,
    )
    assert prompter.confirmed == []
    assert all(r.status in {"ok", "info"} for r in results)
    assert not any(c.input is not None for c in runner.calls)
    assert not any("git clone" in c for c in runner.remote_commands())


def test_version_mismatch_installs_then_verifies(tmp_path: Path) -> None:
    # The fake keeps reporting the old version, so post-install verification must fail loudly.
    runner = _healthy(**{"agentbox --version": out("agentbox, version 0.4.0\n")})
    with pytest.raises(RemoteError, match="still differs"):
        run_setup(
            "vm",
            RemoteShell("vm", runner),
            ScriptedPrompter(ANSWERS),
            local_version=VERSION,
            wheel_builder=lambda: None,
            registry=tmp_path / "r.yaml",
        )
    assert f"pipx install --force agentbox=={VERSION}" in runner.remote_commands()


def test_wheel_is_streamed_not_copied_with_scp(tmp_path: Path) -> None:
    wheel = tmp_path / "agentbox-0.5.0a1-py3-none-any.whl"
    wheel.write_bytes(b"wheel-bytes")
    runner = _healthy(**{"agentbox --version": fail("command not found")})
    with pytest.raises(RemoteError):
        run_setup(
            "vm",
            RemoteShell("vm", runner),
            ScriptedPrompter(ANSWERS),
            local_version=VERSION,
            wheel_builder=lambda: wheel,
            registry=tmp_path / "r.yaml",
        )
    uploads = [c for c in runner.calls if c.input is not None]
    assert uploads and "base64 -d" in uploads[0].remote
    assert f"pipx install --force .cache/agentbox/{wheel.name}" in runner.remote_commands()
    assert not any(c.argv[0] == "scp" for c in runner.calls)


def test_declined_install_is_action(tmp_path: Path) -> None:
    runner = _healthy(**{"agentbox --version": fail("command not found")})
    results = run_setup(
        "vm",
        RemoteShell("vm", runner),
        ScriptedPrompter(ANSWERS, confirm=False),
        local_version=VERSION,
        wheel_builder=lambda: None,
        registry=tmp_path / "r.yaml",
    )
    assert results[-1].name == "agentbox" and results[-1].status == "action"


def test_gh_not_logged_in_prints_command_without_token(tmp_path: Path) -> None:
    runner = _healthy(**{"gh auth status": fail("not logged in")})
    results = run_setup(
        "vm",
        RemoteShell("vm", runner),
        ScriptedPrompter(ANSWERS),
        local_version=VERSION,
        wheel_builder=lambda: None,
        registry=tmp_path / "r.yaml",
    )
    assert results[-1].name == "github" and results[-1].status == "action"
    assert "ssh -t vm gh auth login --hostname github.com --web" in results[-1].detail


def test_clones_missing_repository(tmp_path: Path) -> None:
    runner = _healthy(**{"test -d /srv/ab/r/repo/.git": fail("")})
    run_setup(
        "vm",
        RemoteShell("vm", runner),
        ScriptedPrompter(ANSWERS),
        local_version=VERSION,
        wheel_builder=lambda: None,
        registry=tmp_path / "r.yaml",
    )
    assert "git clone git@github.com:o/r.git /srv/ab/r/repo" in runner.remote_commands()


def test_remote_config_conflict_not_overwritten(tmp_path: Path) -> None:
    runner = _healthy(**{"cat .config/agentbox/config.yaml": out("runtime: podman\n")})
    results = run_setup(
        "vm",
        RemoteShell("vm", runner),
        ScriptedPrompter(ANSWERS),
        local_version=VERSION,
        wheel_builder=lambda: None,
        registry=tmp_path / "r.yaml",
    )
    assert results[-1].name == "remote-config" and results[-1].status == "action"
    assert "runtime" in results[-1].detail
    assert not any(c.input is not None for c in runner.calls)


def test_remote_config_adds_missing_keys_after_confirmation(tmp_path: Path) -> None:
    runner = _healthy(**{"cat .config/agentbox/config.yaml": fail("No such file")})
    run_setup(
        "vm",
        RemoteShell("vm", runner),
        ScriptedPrompter(ANSWERS),
        local_version=VERSION,
        wheel_builder=lambda: None,
        registry=tmp_path / "r.yaml",
    )
    written = [c.input for c in runner.calls if c.input is not None]
    assert yaml.safe_load(written[-1]) == {
        "runtime": "docker",
        "credentials": {"github": True},
        "state_scope": "repository",
    }


def test_successful_setup_saves_registry(tmp_path: Path) -> None:
    registry = tmp_path / "r.yaml"
    results = run_setup(
        "vm",
        RemoteShell("vm", _healthy()),
        ScriptedPrompter(ANSWERS),
        local_version=VERSION,
        wheel_builder=lambda: None,
        registry=registry,
    )
    assert _statuses(results)["save"] == "fixed"
    assert get_host("vm", registry).runtime == "docker"


def test_doctor_never_writes(tmp_path: Path) -> None:
    host = RemoteHost(
        ssh="vm",
        repository="git@github.com:o/r.git",
        base_dir="/srv/ab/r",
        agent="claude",
        runtime="docker",
    )
    runner = _healthy(
        **{
            "agentbox --version": out("agentbox, version 0.4.0\n"),
            "cat .config/agentbox/config.yaml": fail("No such file"),
        }
    )
    results = run_doctor(host, RemoteShell("vm", runner), local_version=VERSION)
    assert _statuses(results)["agentbox"] == "action"
    assert not any(c.input is not None for c in runner.calls)
    assert not any(
        word in c
        for c in runner.remote_commands()
        for word in ("pipx install", "git clone", "agentbox build")
    )
