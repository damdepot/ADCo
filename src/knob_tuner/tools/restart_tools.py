"""Database restart tools supporting Docker, local services, and remote SSH."""

import subprocess
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .db_connector import DBConfig


def restart_docker_db(container_name: str, timeout: int = 60) -> tuple[bool, str]:
    """Restart a database running inside a Docker container.

    Args:
        container_name: Docker container name or ID.
        timeout: Maximum seconds to wait for restart command.

    Returns:
        Tuple of (success: bool, message: str).
    """
    if not container_name or not container_name.strip():
        return False, "Container name cannot be empty"

    container_name = container_name.strip()
    try:
        proc = subprocess.run(
            ["docker", "restart", container_name],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if proc.returncode == 0:
            return True, f"Docker container '{container_name}' restarted successfully"
        err_msg = proc.stderr.strip() or proc.stdout.strip()
        return False, f"Failed to restart container '{container_name}': {err_msg}"
    except subprocess.TimeoutExpired:
        return False, f"Timed out restarting container '{container_name}' after {timeout}s"
    except FileNotFoundError:
        return False, "docker command not found in PATH"
    except Exception as e:
        return False, f"Unexpected error restarting container '{container_name}': {e}"


def restart_local_db(
    service_name: str, method: str = "systemctl", timeout: int = 60
) -> tuple[bool, str]:
    """Restart a local database service via systemctl, service, or brew.

    Args:
        service_name: Name of the service (e.g. 'postgresql', 'mysql').
        method: Init system / command manager ('systemctl', 'service', 'brew').
        timeout: Maximum seconds to wait for restart command.

    Returns:
        Tuple of (success: bool, message: str).
    """
    if not service_name or not service_name.strip():
        return False, "Service name cannot be empty"

    service_name = service_name.strip()
    method = method.strip().lower()

    if method == "systemctl":
        cmd = ["systemctl", "restart", service_name]
    elif method == "service":
        cmd = ["service", service_name, "restart"]
    elif method == "brew":
        cmd = ["brew", "services", "restart", service_name]
    else:
        cmd = [method, "restart", service_name]

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if proc.returncode == 0:
            return (
                True,
                f"Service '{service_name}' restarted successfully via {method}",
            )
        err_msg = proc.stderr.strip() or proc.stdout.strip()
        return (
            False,
            f"Failed to restart service '{service_name}' via {method}: {err_msg}",
        )
    except subprocess.TimeoutExpired:
        return False, f"Timed out restarting service '{service_name}' after {timeout}s"
    except FileNotFoundError:
        return False, f"Command '{cmd[0]}' not found in PATH"
    except Exception as e:
        return False, f"Unexpected error restarting service '{service_name}': {e}"


def restart_remote_db(
    host: str,
    user: str,
    restart_cmd: str,
    key_file: str | None = None,
    timeout: int = 60,
) -> tuple[bool, str]:
    """Restart a remote database service over SSH.

    Args:
        host: Remote hostname or IP address.
        user: SSH user.
        restart_cmd: Remote shell command to execute.
        key_file: Optional path to private SSH key.
        timeout: Maximum seconds to wait for remote command.

    Returns:
        Tuple of (success: bool, message: str).
    """
    if not host or not host.strip():
        return False, "Remote host cannot be empty"
    if not user or not user.strip():
        return False, "Remote user cannot be empty"
    if not restart_cmd or not restart_cmd.strip():
        return False, "Restart command cannot be empty"

    host = host.strip()
    user = user.strip()
    restart_cmd = restart_cmd.strip()

    cmd = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no"]
    if key_file:
        cmd.extend(["-i", key_file])
    cmd.extend([f"{user}@{host}", restart_cmd])

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if proc.returncode == 0:
            return True, f"Remote database at {host} restarted successfully"
        err_msg = proc.stderr.strip() or proc.stdout.strip()
        return False, f"Failed to restart remote database at {host}: {err_msg}"
    except subprocess.TimeoutExpired:
        return False, f"Timed out executing remote restart at {host} after {timeout}s"
    except FileNotFoundError:
        return False, "ssh command not found in PATH"
    except Exception as e:
        return False, f"Unexpected error executing remote restart at {host}: {e}"


def restart_db_by_config(cfg: "DBConfig") -> tuple[bool, str]:
    """Restart database based on the configuration's restart settings.

    Args:
        cfg: DBConfig object containing restart_type and associated parameters.

    Returns:
        Tuple of (success: bool, message: str).
    """
    restart_type = (cfg.restart_type or "docker").strip().lower()

    if restart_type == "docker":
        target = cfg.restart_target or f"{cfg.env}_{cfg.db_type}"
        return restart_docker_db(target)

    elif restart_type in ("local", "systemctl", "service", "brew"):
        method = "systemctl" if restart_type == "local" else restart_type
        target = cfg.restart_target or cfg.db_type
        return restart_local_db(target, method=method)

    elif restart_type in ("remote", "ssh"):
        return restart_remote_db(
            host=cfg.remote_host,
            user=cfg.remote_user,
            restart_cmd=cfg.restart_cmd,
        )

    else:
        return False, f"Unsupported restart_type: '{cfg.restart_type}'"
