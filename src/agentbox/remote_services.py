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
            "agentbox",
            "service",
            "start",
            host.repo_dir,
            "--name",
            name,
            "--restart-policy",
            restart_policy,
            *env_args,
        ],
        error=f"Cannot start service {name}",
    )


def service_status(shell: RemoteShell, name: str) -> str:
    validate_name(name)
    return shell.check(["agentbox", "service", "status", name], error=f"Cannot inspect {name}")


def service_logs(shell: RemoteShell, name: str, tail: int) -> str:
    validate_name(name)
    return shell.check(
        ["agentbox", "service", "logs", name, "--tail", str(tail)],
        error=f"Cannot read logs of {name}",
    )


def stop_service(shell: RemoteShell, name: str) -> str:
    validate_name(name)
    return shell.check(["agentbox", "service", "stop", name], error=f"Cannot stop {name}")
