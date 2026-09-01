"""Database configuration and connectivity module for knob_tuner."""

import configparser
import os
import re
from dataclasses import dataclass
from typing import Any


@dataclass
class DBConfig:
    host: str
    port: int
    user: str
    password: str
    database: str
    db_type: str
    env: str
    restart_type: str = "docker"
    restart_target: str = ""
    restart_cmd: str = ""
    remote_host: str = ""
    remote_user: str = ""


# Allowed statement prefixes for safe queries
_SAFE_QUERY_PREFIXES = ("select", "show", "explain", "describe", "desc")


def _is_safe_query(sql: str) -> bool:
    """Check if an SQL query is strictly read-only."""
    # Remove single-line comments (-- ...) and multi-line comments (/* ... */)
    cleaned = re.sub(r"--.*?(\n|$)", " ", sql)
    cleaned = re.sub(r"/\*.*?\*/", " ", cleaned, flags=re.DOTALL)
    cleaned = cleaned.strip()

    if not cleaned:
        return False

    # Check for statement chaining with semicolons
    # Allow trailing semicolon, but disallow intermediate semicolons
    parts = [p.strip() for p in cleaned.split(";") if p.strip()]
    if len(parts) > 1:
        # Check every statement if multiple
        for part in parts:
            first_word = part.split()[0].lower() if part.split() else ""
            if first_word not in _SAFE_QUERY_PREFIXES:
                return False
        return True

    first_word = cleaned.split()[0].lower() if cleaned.split() else ""
    return first_word in _SAFE_QUERY_PREFIXES


def load_db_config(
    config_path: str, env: str, db_type: str, db_override: str | None = None
) -> DBConfig:
    """Load database configuration from an INI file for a given env and db_type.

    Args:
        config_path: Path to the INI config file.
        env: Environment name (e.g. 'staging', 'production').
        db_type: Database engine type ('postgres', 'postgresql', 'mysql').
        db_override: Optional database name override.

    Returns:
        DBConfig dataclass instance.

    Raises:
        FileNotFoundError: If config file does not exist.
        KeyError: If the section [env.db_type] or required keys are missing.
        ValueError: If configuration values are invalid.
    """
    if not os.path.isfile(config_path):
        raise FileNotFoundError(f"Database configuration file not found: {config_path}")

    config = configparser.ConfigParser()
    config.read(config_path, encoding="utf-8")

    # Normalize section name lookup (e.g., staging.postgres or staging.mysql)
    section_name = f"{env}.{db_type}"
    matched_section = None
    for section in config.sections():
        if section.lower() == section_name.lower():
            matched_section = section
            break

    if not matched_section:
        raise KeyError(f"Section [{section_name}] not found in {config_path}")

    sec = config[matched_section]
    required_keys = ["host", "port", "user", "password", "database"]
    for key in required_keys:
        if key not in sec:
            raise KeyError(
                f"Missing required key '{key}' in section [{matched_section}] of {config_path}"
            )

    try:
        port = int(sec["port"])
    except ValueError as e:
        raise ValueError(
            f"Invalid port value '{sec['port']}' in [{matched_section}]: {e}"
        ) from e

    database = db_override if db_override else sec["database"]

    return DBConfig(
        host=sec["host"],
        port=port,
        user=sec["user"],
        password=sec["password"],
        database=database,
        db_type=db_type,
        env=env,
        restart_type=sec.get("restart_type", "docker"),
        restart_target=sec.get("restart_target", ""),
        restart_cmd=sec.get("restart_cmd", ""),
        remote_host=sec.get("remote_host", ""),
        remote_user=sec.get("remote_user", ""),
    )


def get_connection(cfg: DBConfig) -> Any:
    """Establish and return a database connection based on DBConfig.

    Supports PostgreSQL (psycopg2 or psycopg) and MySQL (pymysql or MySQLdb).

    Args:
        cfg: DBConfig object with database credentials and type.

    Returns:
        DB connection object.

    Raises:
        ValueError: If db_type is unsupported.
        ImportError: If required driver is not installed.
        Exception: If connection fails.
    """
    db_type = cfg.db_type.lower()

    if db_type in ("postgres", "postgresql"):
        try:
            import psycopg2  # type: ignore

            return psycopg2.connect(
                host=cfg.host,
                port=cfg.port,
                user=cfg.user,
                password=cfg.password,
                dbname=cfg.database,
            )
        except (ImportError, AttributeError):
            pass

        try:
            import psycopg  # type: ignore

            return psycopg.connect(
                host=cfg.host,
                port=cfg.port,
                user=cfg.user,
                password=cfg.password,
                dbname=cfg.database,
            )
        except (ImportError, AttributeError):
            raise ImportError(
                "PostgreSQL driver not found. Please install psycopg2 or psycopg."
            )

    elif db_type == "mysql":
        try:
            import pymysql  # type: ignore

            cursorclass = getattr(getattr(pymysql, "cursors", None), "DictCursor", None)
            kwargs: dict[str, Any] = {
                "host": cfg.host,
                "port": cfg.port,
                "user": cfg.user,
                "password": cfg.password,
                "database": cfg.database,
            }
            if cursorclass:
                kwargs["cursorclass"] = cursorclass
            return pymysql.connect(**kwargs)
        except (ImportError, AttributeError):
            pass

        try:
            import MySQLdb  # type: ignore

            cursorclass = getattr(getattr(MySQLdb, "cursors", None), "DictCursor", None)
            kwargs = {
                "host": cfg.host,
                "port": cfg.port,
                "user": cfg.user,
                "passwd": cfg.password,
                "db": cfg.database,
            }
            if cursorclass:
                kwargs["cursorclass"] = cursorclass
            return MySQLdb.connect(**kwargs)
        except (ImportError, AttributeError):
            raise ImportError(
                "MySQL driver not found. Please install pymysql or mysqlclient."
            )

    else:
        raise ValueError(
            f"Unsupported db_type '{cfg.db_type}'. Supported types: 'postgres', 'mysql'."
        )


def run_safe_query(
    cfg: DBConfig, sql: str, params: tuple | None = None
) -> list[dict[str, Any]]:
    """Execute a safe read-only SQL query and return rows as dictionaries.

    Only permits read operations (SELECT, SHOW, EXPLAIN, DESCRIBE).

    Args:
        cfg: DBConfig instance.
        sql: Query string to execute.
        params: Optional parameters for parameterized queries.

    Returns:
        List of row dictionaries.

    Raises:
        ValueError: If query contains unsafe / mutating statements.
    """
    if not _is_safe_query(sql):
        raise ValueError(
            f"Unsafe query rejected. Only read-only queries (SELECT, SHOW, EXPLAIN, DESCRIBE) are permitted: {sql.strip()[:60]}..."
        )

    conn = get_connection(cfg)
    try:
        cursor = conn.cursor()
        try:
            if params is not None:
                cursor.execute(sql, params)
            else:
                cursor.execute(sql)

            # If the cursor description is None, there is no result set
            if cursor.description is None:
                return []

            rows = cursor.fetchall()
            if not rows:
                return []

            # Check if rows are already dicts (e.g. pymysql DictCursor)
            if isinstance(rows[0], dict):
                return list(rows)

            # Convert tuple rows to dict using column names from cursor.description
            col_names = [d[0] for d in cursor.description]
            return [dict(zip(col_names, row)) for row in rows]
        finally:
            cursor.close()
    finally:
        conn.close()
