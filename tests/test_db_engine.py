"""Tests for the database engine detection and filtering helpers."""
from unittest.mock import MagicMock

from src.intent_analyzer.tools.db_engine import (
    engine_constraint_note,
    engine_of_path,
    filter_paths_by_db_type,
    filter_targets_by_db_type,
    is_foreign_engine_path,
    normalize_engine,
)


def test_engine_of_path():
    assert engine_of_path("drivers/postgresdriver.py") == "postgres"
    assert engine_of_path("drivers/mysqldriver.py") == "mysql"
    assert engine_of_path("drivers/sqlitedriver.py") == "sqlite"
    assert engine_of_path("db.py") is None
    assert engine_of_path("schema.sql") is None
    assert engine_of_path("queries.py") is None


def test_is_foreign_engine_path():
    assert is_foreign_engine_path("drivers/mysqldriver.py", "postgres") is True
    assert is_foreign_engine_path("drivers/postgresdriver.py", "postgres") is False
    assert is_foreign_engine_path("main.py", "postgres") is False
    assert is_foreign_engine_path("drivers/mysqldriver.py", "") is False


def test_filter_paths_by_db_type():
    paths = ["drivers/postgresdriver.py", "drivers/mysqldriver.py", "db.py"]
    assert filter_paths_by_db_type(paths, "postgres") == [
        "drivers/postgresdriver.py",
        "db.py",
    ]
    assert filter_paths_by_db_type(paths, "") == paths


def test_filter_targets_by_db_type():
    targets = [
        {"file": "drivers/mysqldriver.py", "description": "mysql pool"},
        {"file": "drivers/postgresdriver.py", "description": "pg pool"},
        {"description": "no file key"},
    ]
    filtered = filter_targets_by_db_type(targets, "postgres")
    assert {"file": "drivers/postgresdriver.py", "description": "pg pool"} in filtered
    assert all(t.get("file") != "drivers/mysqldriver.py" for t in filtered)
    assert {"description": "no file key"} in filtered

    all_foreign = [
        {"file": "drivers/mysqldriver.py"},
        {"file": "drivers/sqlitedriver.py"},
    ]
    assert filter_targets_by_db_type(all_foreign, "postgres") == all_foreign


def test_normalize_engine():
    assert normalize_engine("postgresql") == "postgres"
    assert normalize_engine("mysql") == "mysql"
    assert normalize_engine("") == ""
    assert normalize_engine("mariadb") == "mysql"


def test_engine_constraint_note():
    assert engine_constraint_note("") == ""
    note = engine_constraint_note("postgres")
    assert "postgres" in note
    assert "postgresql" in note


def test_get_project_files_db_type():
    from src.intent_analyzer.sub_agents.file_selector.tools import get_project_files

    ctx = MagicMock()
    ctx.state = {
        "scan_result": [
            "drivers/postgresdriver.py",
            "drivers/mysqldriver.py",
            "db.py",
        ],
        "db_type": "postgres",
    }
    result = get_project_files(ctx)
    assert isinstance(result, str)
    assert "postgres" in result
    assert "postgresdriver.py" in result
    assert "mysqldriver.py" not in result

    ctx_no_db = MagicMock()
    ctx_no_db.state = {"scan_result": ["a.py", "b.py"]}
    assert get_project_files(ctx_no_db) == ["a.py", "b.py"]


def test_read_selected_files_db_type(tmp_path):
    from src.intent_analyzer.sub_agents.intent_extractor.tools import (
        read_selected_files,
    )

    d = tmp_path / "app"
    d.mkdir()
    (d / "postgres_driver.py").write_text("PG_CONTENT_MARKER")
    (d / "mysql_driver.py").write_text("MYSQL_CONTENT_MARKER")

    ctx = MagicMock()
    ctx.state = {
        "target": str(d),
        "file_selector_output": {
            "files": ["postgres_driver.py", "mysql_driver.py"],
        },
        "db_type": "postgres",
    }
    result = read_selected_files(ctx)
    assert "PG_CONTENT_MARKER" in result
    assert "MYSQL_CONTENT_MARKER" not in result
