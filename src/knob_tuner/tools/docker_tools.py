"""Docker management tools for knob_tuner staging databases."""

import os
import re
import socket
import subprocess
import time
import uuid

from src.knob_tuner.contracts import ResourceBudget

from .db_connector import DBConfig, run_safe_query

# Global registry of active containers tracked across the process lifecycle
ACTIVE_CONTAINERS: set[str] = set()


def get_current_docker_network() -> str | None:
    """Get the current container's Docker network, if running in one."""
    env_net = os.environ.get("ADCO_DOCKER_NETWORK")
    if env_net:
        return env_net.strip()
    if not os.path.exists("/.dockerenv"):
        return None

    try:
        hostname = socket.gethostname()
        proc = subprocess.run(
            ["docker", "inspect", hostname, "-f", "{{range $net, $v := .NetworkSettings.Networks}}{{$net}} {{end}}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if proc.returncode == 0:
            networks = [n.strip() for n in proc.stdout.split() if n.strip()]
            for net in networks:
                if net not in ("bridge", "host", "none"):
                    return net
    except Exception:
        pass
    return None


def resolve_docker_image(db_type: str, db_version: str | None = None) -> str:
    """Resolve the Docker image tag for a given database type and optional version.

    Args:
        db_type: Database type ('postgres', 'postgresql', 'mysql').
        db_version: Optional version string or full version banner.

    Returns:
        Docker image string, e.g. 'postgres:16.3' or 'mysql:8.4'.

    Raises:
        ValueError: If db_type is unsupported.
    """
    raw_db_type = db_type.strip().lower() if db_type else ""
    if raw_db_type in ("postgres", "postgresql"):
        canonical_db = "postgres"
    elif raw_db_type == "mysql":
        canonical_db = "mysql"
    else:
        raise ValueError(
            f"Unsupported db_type '{db_type}'. Supported types: 'postgres', 'mysql'."
        )

    if not db_version or not db_version.strip():
        if canonical_db == "postgres":
            return "postgres:17"
        return "mysql:8.4"

    cleaned = db_version.strip()

    if canonical_db == "postgres":
        if cleaned.lower().startswith("postgresql:"):
            cleaned = cleaned[len("postgresql:"):].strip()
        elif cleaned.lower().startswith("postgres:"):
            cleaned = cleaned[len("postgres:"):].strip()

        # Match numeric version and optional standard docker tag suffix (e.g. -alpine, -bookworm, -bullseye, -slim)
        m = re.search(
            r"(?:postgresql\s+)?([0-9]+(?:\.[0-9]+)*)(-(?:alpine|bookworm|bullseye|slim)[a-zA-Z0-9_.-]*)?",
            cleaned,
            re.IGNORECASE,
        )
        if m:
            base_ver = m.group(1)
            tag_suffix = m.group(2) or ""
            
            parts = base_ver.split('.')
            if len(parts) >= 2:
                major = int(parts[0])
                minor = int(parts[1])
                if major >= 10 and minor > 9:
                    base_ver = str(major)

            version_str = f"{base_ver}{tag_suffix}"
        else:
            version_str = cleaned

        return f"postgres:{version_str}"

    else:  # mysql
        if cleaned.lower().startswith("mysql:"):
            cleaned = cleaned[len("mysql:"):].strip()

        m = re.search(
            r"(?:mysql\s+)?(?:community\s+server\s+)?(?:-\s+gpl\s+)?([0-9]+\.[0-9]+)",
            cleaned,
            re.IGNORECASE,
        )
        if m:
            version_str = m.group(1)
        else:
            m_single = re.search(r"([0-9]+(?:\.[0-9]+)?)", cleaned)
            if m_single:
                version_str = m_single.group(1)
            else:
                version_str = cleaned

        return f"mysql:{version_str}"


def register_active_container(container_name: str) -> None:
    """Register a container name as actively running."""
    if container_name and container_name.strip():
        ACTIVE_CONTAINERS.add(container_name.strip())


def unregister_active_container(container_name: str) -> None:
    """Unregister a container name from active tracking."""
    if container_name and container_name.strip():
        ACTIVE_CONTAINERS.discard(container_name.strip())


def get_active_containers() -> set[str]:
    """Return a copy of currently tracked active container names."""
    return set(ACTIVE_CONTAINERS)


def is_docker_available() -> tuple[bool, str]:
    """Check if Docker daemon is running and responsive.

    Returns:
        Tuple of (is_available: bool, message: str).
    """
    try:
        proc = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if proc.returncode == 0:
            return True, "Docker daemon is running and responsive."
        err_msg = proc.stderr.strip() or proc.stdout.strip()
        return False, f"Docker daemon is not running: {err_msg}"
    except subprocess.TimeoutExpired:
        return False, "Docker info check timed out after 10s"
    except FileNotFoundError:
        return False, "docker command not found in PATH"
    except Exception as e:
        return False, f"Unexpected error checking Docker availability: {e}"


def get_container_host_port(container_name: str, internal_port: int = 5432, timeout: int = 15) -> int:
    """Get the mapped host port for a container's internal port.

    Args:
        container_name: The Docker container name or ID.
        internal_port: The internal container port to resolve.
        timeout: Maximum seconds to wait for docker port command.

    Returns:
        The mapped host port.

    Raises:
        RuntimeError: If port resolution fails or output is malformed.
    """
    try:
        proc = subprocess.run(
            ["docker", "port", container_name, str(internal_port)],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except Exception as e:
        raise RuntimeError(f"Failed to get port mapping for container '{container_name}': {e}") from e

    if proc.returncode != 0:
        err_msg = proc.stderr.strip() or proc.stdout.strip()
        raise RuntimeError(f"Failed to get port mapping for container '{container_name}': {err_msg}")

    for line in proc.stdout.strip().splitlines():
        line = line.strip()
        if ":" in line:
            port_str = line.rsplit(":", 1)[-1]
            if port_str.isdigit():
                return int(port_str)

    raise RuntimeError(
        f"Failed to parse mapped host port from docker port output: '{proc.stdout.strip()}'"
    )


def stop_staging_db(container_name: str, timeout: int = 15) -> tuple[bool, str]:
    """Stop and remove a staging database Docker container.

    Args:
        container_name: Name or ID of the Docker container.
        timeout: Seconds to wait before killing container during stop.

    Returns:
        Tuple of (success: bool, message: str).
    """
    if not container_name or not container_name.strip():
        return False, "Container name cannot be empty"

    container_name = container_name.strip()
    unregister_active_container(container_name)
    try:
        # Gracefully stop the container
        subprocess.run(
            ["docker", "stop", "-t", str(timeout), container_name],
            capture_output=True,
            text=True,
            timeout=timeout + 15,
        )
    except Exception:
        # Ignore stop errors and proceed to forced removal
        pass

    try:
        proc = subprocess.run(
            ["docker", "rm", "-f", "-v", container_name],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if proc.returncode == 0:
            return True, f"Container '{container_name}' stopped and removed successfully"
        err_msg = proc.stderr.strip() or proc.stdout.strip()
        return False, f"Failed to remove container '{container_name}': {err_msg}"
    except subprocess.TimeoutExpired:
        return False, f"Timed out removing container '{container_name}'"
    except FileNotFoundError:
        return False, "docker command not found in PATH"
    except Exception as e:
        return False, f"Unexpected error stopping container '{container_name}': {e}"


def cleanup_orphan_containers() -> int:
    """Find and remove all orphaned staging containers managed by knob_tuner.

    Returns:
        Number of removed containers.
    """
    try:
        proc = subprocess.run(
            ["docker", "ps", "-aq", "--filter", "label=managed-by=adco-knob-tuner"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if proc.returncode != 0:
            return 0

        container_ids = [cid.strip() for cid in proc.stdout.strip().split() if cid.strip()]
        if not container_ids:
            return 0

        rm_proc = subprocess.run(
            ["docker", "rm", "-f", "-v"] + container_ids,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if rm_proc.returncode == 0:
            return len(container_ids)

        removed_count = len([line for line in rm_proc.stdout.strip().splitlines() if line.strip()])
        return removed_count
    except Exception:
        return 0


def verify_container_resources(
    container_name: str, budget: ResourceBudget
) -> tuple[bool, str]:
    """Verify a running container's CPU and memory limits match the budget.

    Args:
        container_name: Name or ID of the Docker container to inspect.
        budget: The resource budget the container was expected to receive.

    Returns:
        Tuple of (matches: bool, message: str). Never raises; any subprocess or
        parse failure is reported as ``(False, <error message>)``.
    """
    expected_nano_cpus = int(budget.cpu_cores * 1_000_000_000)
    expected_memory = int(budget.memory_gb * 1024**3)

    try:
        proc = subprocess.run(
            [
                "docker",
                "inspect",
                "--format",
                "{{.HostConfig.NanoCpus}} {{.HostConfig.Memory}}",
                container_name,
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception as e:
        return False, f"Failed to inspect container '{container_name}': {e}"

    if proc.returncode != 0:
        err_msg = proc.stderr.strip() or proc.stdout.strip()
        return False, f"Failed to inspect container '{container_name}': {err_msg}"

    output = proc.stdout.strip()
    try:
        parts = output.split()
        actual_nano_cpus = int(parts[0])
        actual_memory = int(parts[1])
    except (IndexError, ValueError) as e:
        return False, (
            f"Malformed docker inspect output for container '{container_name}': "
            f"'{output}' ({e})"
        )

    mismatches: list[str] = []
    if actual_nano_cpus != expected_nano_cpus:
        mismatches.append(
            f"cpus actual={actual_nano_cpus} expected={expected_nano_cpus}"
        )
    if actual_memory != expected_memory:
        mismatches.append(
            f"memory actual={actual_memory} expected={expected_memory}"
        )

    if mismatches:
        return False, (
            f"Container '{container_name}' resource mismatch: " + "; ".join(mismatches)
        )

    return True, (
        f"Container '{container_name}' resources verified "
        f"(cpus={budget.to_docker_cpus()}, memory={budget.to_docker_memory()})"
    )


def start_staging_db(
    db_type: str = "postgres",
    db_version: str | None = None,
    budget: ResourceBudget | None = None,
    database: str = "testdb",
    timeout: int = 60,
) -> tuple[str, DBConfig]:
    """Start an ephemeral Docker container for staging database benchmarking.

    Args:
        db_type: Database type ('postgres', 'postgresql', 'mysql').
        db_version: Optional database version string or banner.
        budget: Explicit resource budget for the container. Required; there is
            no hidden default.
        database: Database name to create and initialize.
        timeout: Maximum seconds to wait for database readiness.

    Returns:
        Tuple of (container_name: str, config: DBConfig).

    Raises:
        ValueError: If db_type is unsupported or no budget is provided.
        RuntimeError: If container fails to start, resource limits do not match
            the budget, or port mapping cannot be resolved.
        TimeoutError: If database fails to become ready within timeout.
    """
    raw_db_type = db_type.strip().lower()
    if raw_db_type in ("postgres", "postgresql"):
        engine = "postgres"
    elif raw_db_type == "mysql":
        engine = "mysql"
    else:
        raise ValueError(
            f"Unsupported db_type '{db_type}'. Supported types: 'postgres', 'mysql'."
        )

    if budget is None:
        raise ValueError("ResourceBudget is required to start the staging database")

    image = resolve_docker_image(engine, db_version)
    container_id_suffix = uuid.uuid4().hex[:8]
    container_name = f"adco-staging-{engine}-{container_id_suffix}"

    current_network = get_current_docker_network()

    if engine == "postgres":
        internal_port = 5432
        user = "postgres"
        password = "postgres"
        env_vars = [
            "-e",
            f"POSTGRES_USER={user}",
            "-e",
            f"POSTGRES_PASSWORD={password}",
            "-e",
            f"POSTGRES_DB={database}",
        ]
        extra_args: list[str] = []
        readiness_cmd = [
            "docker",
            "exec",
            container_name,
            "pg_isready",
            "-U",
            user,
            "-d",
            database,
            "-h",
            "127.0.0.1",
        ]
    else:  # mysql
        internal_port = 3306
        user = "root"
        password = "mysql_root_password"
        env_vars = [
            "-e",
            f"MYSQL_ROOT_PASSWORD={password}",
            "-e",
            f"MYSQL_DATABASE={database}",
        ]
        extra_args = ["--mysql-native-password=ON"]
        readiness_cmd = [
            "docker",
            "exec",
            container_name,
            "mysqladmin",
            "ping",
            "-u",
            user,
            f"-p{password}",
            "--silent",
        ]

    cmd = [
        "docker",
        "run",
        "-d",
        "--name",
        container_name,
        "--label",
        "managed-by=adco-knob-tuner",
        f"--cpus={budget.to_docker_cpus()}",
        f"--memory={budget.to_docker_memory()}",
        f"--memory-swap={budget.to_docker_memory()}",
        "-p",
        f"127.0.0.1::{internal_port}",
    ]
    if current_network:
        cmd.extend(["--network", current_network])
    cmd.extend(env_vars)

    cmd.append(image)
    cmd.extend(extra_args)

    try:
        run_proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except Exception as e:
        raise RuntimeError(f"Failed to execute docker run for '{container_name}': {e}") from e

    if run_proc.returncode != 0:
        err_msg = run_proc.stderr.strip() or run_proc.stdout.strip()
        if "manifest unknown" in err_msg.lower() or "pull" in err_msg.lower() or "not found" in err_msg.lower():
            fallback_image = "postgres:17" if engine == "postgres" else "mysql:8.4"
            idx = cmd.index(image)
            cmd[idx] = fallback_image
            try:
                run_proc = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                if run_proc.returncode != 0:
                    err_msg = run_proc.stderr.strip() or run_proc.stdout.strip()
                    raise RuntimeError(f"Failed to start staging DB container '{container_name}' even with fallback {fallback_image}: {err_msg}")
            except Exception as e:
                raise RuntimeError(f"Failed to execute fallback docker run for '{container_name}': {e}") from e
        else:
            raise RuntimeError(f"Failed to start staging DB container '{container_name}': {err_msg}")

    register_active_container(container_name)

    # Verify the container actually received the requested resource limits
    resources_ok, resources_msg = verify_container_resources(container_name, budget)
    if not resources_ok:
        stop_staging_db(container_name)
        raise RuntimeError(resources_msg)

    # Inspect assigned host port
    try:
        host_port = get_container_host_port(container_name, internal_port, timeout=15)
    except Exception as e:
        stop_staging_db(container_name)
        raise RuntimeError(str(e)) from e

    if current_network and current_network not in ("bridge", "host", "none"):
        conn_host = container_name
        conn_port = internal_port
    else:
        conn_host = "127.0.0.1"
        conn_port = host_port

    cfg = DBConfig(
        host=conn_host,
        port=conn_port,
        user=user,
        password=password,
        database=database,
        db_type=engine,
        env="staging",
        restart_type="docker",
        restart_target=container_name,
    )

    # Inspect container IP for fallback connection
    container_ip = None
    try:
        ip_proc = subprocess.run(
            ["docker", "inspect", "-f", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}", container_name],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if ip_proc.returncode == 0:
            container_ip = ip_proc.stdout.strip()
    except Exception:
        pass

    # Readiness polling loop
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            exec_proc = subprocess.run(
                readiness_cmd,
                capture_output=True,
                text=True,
                timeout=10,
            )
            if exec_proc.returncode == 0:
                # Also verify connection via database driver / query
                try:
                    run_safe_query(cfg, "SELECT 1")
                    return container_name, cfg
                except Exception:
                    fallback_host = container_ip or (container_name if cfg.host != container_name else None)
                    if fallback_host:
                        fallback_cfg = DBConfig(
                            host=fallback_host,
                            port=internal_port,
                            user=user,
                            password=password,
                            database=database,
                            db_type=engine,
                            env="staging",
                            restart_type="docker",
                            restart_target=container_name,
                        )
                        try:
                            run_safe_query(fallback_cfg, "SELECT 1")
                            return container_name, fallback_cfg
                        except Exception:
                            pass
        except Exception:
            pass
        time.sleep(1)

    # Capture container logs on timeout failure before stopping container
    container_logs = ""
    try:
        logs_proc = subprocess.run(
            ["docker", "logs", "--tail", "50", container_name],
            capture_output=True,
            text=True,
            timeout=10,
        )
        out = logs_proc.stdout.strip()
        err = logs_proc.stderr.strip()
        if out and err:
            container_logs = f"{out}\n{err}"
        elif out:
            container_logs = out
        elif err:
            container_logs = err
    except Exception as log_err:
        container_logs = f"(Failed to retrieve container logs: {log_err})"

    # Clean up container on timeout failure
    stop_staging_db(container_name)

    err_msg = f"Staging DB container '{container_name}' did not become ready within {timeout}s"
    if container_logs:
        err_msg += f"\nContainer logs (tail 50):\n{container_logs}"

    raise TimeoutError(err_msg)

def restart_docker_db(
    container_name: str,
    timeout: int = 60,
    db_type: str = "postgres",
    readiness_timeout: int = 60,
) -> tuple[bool, str]:
    """Restart a database running inside a Docker container.

    Args:
        container_name: Docker container name or ID.
        timeout: Maximum seconds to wait for the docker restart command.
        db_type: Type of database ('postgres' or 'mysql') used to select
            the readiness probe command.
        readiness_timeout: Maximum seconds to wait for the database inside
            the container to accept connections after restart.

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
        if proc.returncode != 0:
            err_msg = proc.stderr.strip() or proc.stdout.strip()
            return False, f"Failed to restart container '{container_name}': {err_msg}"
    except subprocess.TimeoutExpired:
        return False, f"Timed out restarting container '{container_name}' after {timeout}s"
    except FileNotFoundError:
        return False, "docker command not found in PATH"
    except Exception as e:
        return False, f"Unexpected error restarting container '{container_name}': {e}"

    # Build the readiness probe command based on db_type
    if db_type == "mysql":
        probe_cmd = [
            "docker", "exec", container_name,
            "mysqladmin", "ping", "-uroot", "--silent",
        ]
    else:
        # Default to postgres
        probe_cmd = [
            "docker", "exec", container_name,
            "pg_isready", "-U", "postgres", "-h", "127.0.0.1",
        ]

    # Poll until the database is ready or readiness_timeout elapses
    elapsed = 0
    while elapsed < readiness_timeout:
        try:
            result = subprocess.run(
                probe_cmd,
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                return (
                    True,
                    f"Docker container '{container_name}' restarted and ready",
                )
        except Exception:
            pass
        time.sleep(1)
        elapsed += 1

    return (
        False,
        f"Docker container '{container_name}' restarted but database did not become"
        f" ready within {readiness_timeout}s",
    )


def recreate_docker_db(
    container_name: str,
    db_type: str = "postgres",
    db_version: str | None = None,
    database: str = "testdb",
    budget: ResourceBudget | None = None,
    timeout: int = 60,
) -> tuple[bool, str, object]:
    """Stop, remove, and recreate a staging Docker container, returning the new config.

    When Docker restarts a container with dynamic port mapping (``-p 127.0.0.1::<port>``),
    the host port is reassigned on every restart. Recreating the container ensures the
    new port is captured and stored in state so subsequent tools connect to the right address.

    Args:
        container_name: Name of the existing staging container to replace.
        db_type: Database type ('postgres' or 'mysql').
        db_version: Optional database version string.
        database: Database name to recreate.
        budget: Explicit resource budget for the new container. Required; there
            is no hidden default.
        timeout: Maximum seconds to wait for the new container to become ready.

    Returns:
        Tuple of (success: bool, new_container_name_or_error: str, new_cfg_or_None).
    """
    if budget is None:
        raise ValueError("ResourceBudget is required to recreate the staging database")

    stop_staging_db(container_name)

    try:
        new_container_name, new_cfg = start_staging_db(
            db_type=db_type,
            db_version=db_version,
            database=database,
            budget=budget,
            timeout=timeout,
        )
        return True, new_container_name, new_cfg
    except Exception as e:
        return False, str(e), None
