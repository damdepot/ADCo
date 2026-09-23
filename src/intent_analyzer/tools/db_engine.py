"""Database engine detection and path filtering helpers.

These helpers keep the pipeline focused on the engine the user asked for by
classifying source-file paths according to the database engine their names
reference. Detection is purely keyword/filename based, so it stays generic
across arbitrary codebases.
"""
from __future__ import annotations

from collections.abc import Iterable

DB_ENGINE_ALIASES: dict[str, tuple[str, ...]] = {
    "postgres": ("postgres", "postgresql", "psycopg", "pgsql", "pg8000"),
    "mysql": ("mysql", "mariadb", "pymysql", "mysqldb"),
    "sqlite": ("sqlite",),
    "mssql": ("mssql", "sqlserver", "pyodbc", "pymssql", "freetds"),
    "oracle": ("oracle", "cx_oracle", "oracledb"),
    "mongodb": ("mongo", "pymongo"),
    "cassandra": ("cassandra", "pycassa"),
    "redis": ("redis",),
}

_NORMALIZE_MAP: dict[str, str] = {
    "postgresql": "postgres",
    "psql": "postgres",
    "pg": "postgres",
    "mariadb": "mysql",
    "sqlserver": "mssql",
    "mongodb": "mongodb",
}


def normalize_engine(db_type: str) -> str:
    """Normalize a user-provided engine name to its canonical key.

    Lowercases and strips the input, then maps common aliases (for example
    ``postgresql``/``psql``/``pg`` -> ``postgres`` and ``mariadb`` -> ``mysql``).
    Unknown values are returned lowercased and stripped.
    """
    if not db_type:
        return ""
    normalized = db_type.strip().lower()
    return _NORMALIZE_MAP.get(normalized, normalized)


def engine_of_path(path: str) -> str | None:
    """Return the engine whose alias token appears in ``path``, or ``None``.

    Iterates :data:`DB_ENGINE_ALIASES` in insertion order and returns the first
    engine whose any token is a substring of the lowercased path. Engine-agnostic
    paths (for example ``db.py``, ``schema.sql``, ``queries.py``) return ``None``.
    """
    if not path:
        return None
    lowered = path.lower()
    for engine, tokens in DB_ENGINE_ALIASES.items():
        for token in tokens:
            if token in lowered:
                return engine
    return None


def is_foreign_engine_path(path: str, db_type: str) -> bool:
    """Return ``True`` when ``path`` clearly belongs to a different engine.

    Returns ``False`` when ``db_type`` is falsy or normalizes to an empty string.
    Otherwise returns ``True`` only when the path maps to a known engine that
    differs from the normalized ``db_type``.
    """
    if not db_type:
        return False
    normalized = normalize_engine(db_type)
    if not normalized:
        return False
    path_engine = engine_of_path(path)
    return path_engine is not None and path_engine != normalized


def filter_paths_by_db_type(paths: Iterable[str], db_type: str) -> list[str]:
    """Drop foreign-engine paths from ``paths``.

    Returns ``list(paths)`` unchanged when ``db_type`` is falsy. Engine-agnostic
    paths are always kept.
    """
    all_paths = list(paths)
    if not db_type:
        return all_paths
    return [p for p in all_paths if not is_foreign_engine_path(p, db_type)]


def filter_targets_by_db_type(targets: Iterable[dict], db_type: str) -> list[dict]:
    """Drop optimization targets whose ``file`` belongs to a foreign engine.

    Targets without a ``file`` key are always kept. As a safety measure, if
    filtering removes every target the original list is returned unchanged so a
    contradictory request never yields an empty pipeline. No-op when ``db_type``
    is falsy.
    """
    all_targets = list(targets)
    if not db_type:
        return all_targets

    def _keep(target: dict) -> bool:
        file_path = target.get("file") if isinstance(target, dict) else None
        if not file_path:
            return True
        return not is_foreign_engine_path(file_path, db_type)

    filtered = [t for t in all_targets if _keep(t)]
    if not filtered:
        return all_targets
    return filtered


def engine_constraint_note(db_type: str) -> str:
    """Build a human-readable engine constraint instruction for LLM tools.

    Returns an empty string when ``db_type`` is falsy.
    """
    if not db_type:
        return ""
    normalized = normalize_engine(db_type) or db_type.strip().lower()
    tokens = DB_ENGINE_ALIASES.get(normalized, (normalized,))
    others = ", ".join(engine for engine in DB_ENGINE_ALIASES if engine != normalized)
    return (
        f"Target database engine: {normalized}. Optimize ONLY {normalized}-related "
        f"files ({'/'.join(tokens)}); EXCLUDE files for other engines "
        f"({others}). Engine-agnostic files (shared schema/queries) may be included."
    )
