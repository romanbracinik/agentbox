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
from pydantic import ValidationError

from .exceptions import RemoteError
from .remote_registry import RemoteHost, load_registry, save_host
from .remote_ssh import RemoteShell

Status = Literal["ok", "fixed", "action", "info"]
WheelBuilder = Callable[[], "Path | None"]
# Mirrors agentbox.config.get_config_paths(): a repo-tracked file always wins (it is
# checked out into every worktree), then the remote user's first existing global file.
REPO_CONFIG_NAMES = (".agentbox.yaml", ".agentbox.yml")
GLOBAL_CONFIG_CANDIDATES = (
    ".config/agentbox/config.yaml",
    ".config/agentbox/config.yml",
    ".agentbox.yaml",
)
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
            "Install Podman (e.g. `sudo apt-get install -y podman`) or give the SSH user "
            "Docker access (`sudo usermod -aG docker $USER`, or `gpasswd -a` for OS Login "
            "users), then re-run",
        ),
        None,
    )


def _tools(shell: RemoteShell) -> StepResult:
    missing = [tool for tool in TOOLS if shell.run(["command", "-v", tool]).returncode != 0]
    if missing:
        return StepResult(
            "tools",
            "action",
            f"Install on the remote: {' '.join(missing)} (e.g. `sudo apt-get install -y ...`)",
        )
    return StepResult("tools", "ok", ", ".join(TOOLS))


def _remote_version(shell: RemoteShell) -> str | None:
    result = shell.run(["agentbox", "--version"])
    return (
        result.stdout.strip().split()[-1]
        if result.returncode == 0 and result.stdout.strip()
        else None
    )


def _agentbox(
    shell: RemoteShell,
    prompter: Prompter | None,
    local_version: str,
    wheel_builder: WheelBuilder | None,
) -> StepResult:
    remote = _remote_version(shell)
    if remote == local_version:
        return StepResult("agentbox", "ok", local_version)
    found = remote or "not installed"
    if prompter is None or wheel_builder is None:
        return StepResult(
            "agentbox", "action", f"remote {found}, laptop {local_version}; run remote setup"
        )
    if not prompter.confirm(f"Install agentbox {local_version} on the remote (currently {found})?"):
        return StepResult("agentbox", "action", f"remote {found}, laptop {local_version}")
    wheel = wheel_builder()
    if wheel is None:
        shell.check(
            ["pipx", "install", "--force", f"agentbox=={local_version}"],
            error="pipx install failed",
        )
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
        f"Run in your terminal: ssh -t {shell.destination} gh auth login "
        "--hostname github.com --web",
    )


def _repository(shell: RemoteShell, repository: str, base_dir: str, *, write: bool) -> StepResult:
    repo = f"{base_dir}/repo"
    if shell.run(["test", "-d", f"{repo}/.git"]).returncode == 0:
        origin_result = shell.run(["git", "-C", repo, "remote", "get-url", "origin"])
        if origin_result.returncode != 0:
            return StepResult("repository", "action", f"{repo} has no origin remote")
        origin = origin_result.stdout
        if origin.strip() != repository:
            return StepResult(
                "repository", "action", f"{repo} has origin {origin.strip()}, expected {repository}"
            )
        return StepResult("repository", "ok", repo)
    if not write:
        return StepResult("repository", "action", f"{repo} missing; run remote setup")
    shell.check(["mkdir", "-p", f"{base_dir}/wt"], error=f"Cannot create {base_dir}")
    shell.check(["git", "clone", repository, repo], error=f"Cannot clone {repository}")
    return StepResult("repository", "fixed", repo)


def _desired_config(runtime: str) -> dict[str, Any]:
    return {"runtime": runtime, "credentials": {"github": True}, "state_scope": "repository"}


def _merge(
    current: dict[str, Any], desired: dict[str, Any], prefix: str = ""
) -> tuple[dict[str, Any], list[str], list[str]]:
    merged = dict(current)
    missing: list[str] = []
    conflicts: list[str] = []
    for key, value in desired.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict):
            nested, nested_missing, nested_conflicts = _merge(
                dict(current.get(key) or {}), value, f"{path}."
            )
            merged[key] = nested
            missing += nested_missing
            conflicts += nested_conflicts
        elif key not in current:
            merged[key] = value
            missing.append(path)
        elif current[key] != value:
            conflicts.append(f"{path} is {current[key]!r}, needs {value!r}")
    return merged, missing, conflicts


def _remote_config(
    shell: RemoteShell, prompter: Prompter | None, runtime: str, repo_dir: str
) -> StepResult:
    for name in REPO_CONFIG_NAMES:
        repo_file = f"{repo_dir}/{name}"
        if shell.run(["test", "-f", repo_file]).returncode == 0:
            return StepResult(
                "remote-config",
                "action",
                f"{repo_file} is tracked in the repository and takes precedence; "
                "add runtime, credentials.github and state_scope there",
            )

    target = GLOBAL_CONFIG_CANDIDATES[0]
    current: dict[str, Any] = {}
    for candidate in GLOBAL_CONFIG_CANDIDATES:
        result = shell.run(["cat", candidate])
        if result.returncode != 0:
            continue
        try:
            loaded = yaml.safe_load(result.stdout)
        except yaml.YAMLError:
            return StepResult("remote-config", "action", f"{candidate} is not valid YAML")
        loaded = loaded or {}
        if not isinstance(loaded, dict):
            return StepResult("remote-config", "action", f"{candidate} does not contain a mapping")
        if not isinstance(loaded.get("credentials", {}), dict):
            return StepResult(
                "remote-config", "action", f"{candidate}: 'credentials' must be a mapping"
            )
        target, current = candidate, loaded
        break

    merged, missing, conflicts = _merge(current, _desired_config(runtime))
    if conflicts:
        return StepResult(
            "remote-config",
            "action",
            f"Edit ~/{target} on the remote: " + "; ".join(conflicts),
        )
    if not missing:
        return StepResult("remote-config", "ok", f"~/{target}")
    if prompter is None or not prompter.confirm(f"Add {', '.join(missing)} to remote ~/{target}?"):
        return StepResult("remote-config", "action", f"missing {', '.join(missing)}")
    shell.write_file(target, yaml.safe_dump(merged, sort_keys=False))
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
    results.append(_tools(shell))
    if stop():
        return results
    results.append(_agentbox(shell, prompter, local_version, wheel_builder))
    if stop():
        return results
    results.append(_github(shell))
    if stop():
        return results
    repository = (
        existing.repository if existing else prompter.ask("Git repository URL (repository)")
    )
    base_dir = (
        existing.base_dir
        if existing
        else prompter.ask("Absolute directory on the remote (base_dir)")
    )
    agent = existing.agent if existing else prompter.ask("Default agent (agent)")
    try:
        host = RemoteHost(
            ssh=shell.destination,
            repository=repository,
            base_dir=base_dir,
            agent=agent,
            runtime=cast(Literal["podman", "docker"], runtime),
        )
    except ValidationError as err:
        results.append(StepResult("registry", "action", str(err)))
        return results
    results.append(_repository(shell, host.repository, host.base_dir, write=True))
    if stop():
        return results
    results.append(_remote_config(shell, prompter, runtime, host.repo_dir))
    if stop():
        return results
    results.append(_image(shell, host.repo_dir, host.agent))
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
        _remote_config(shell, None, host.runtime, host.repo_dir),
        _login_hint(shell, host.repo_dir, host.agent),
    ]
    return results
