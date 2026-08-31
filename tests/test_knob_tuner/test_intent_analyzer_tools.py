"""Tests for knob_tuner intent_analyzer sub-agent models, agent, and tools."""

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.knob_tuner.sub_agents.intent_analyzer.agent import (
    create_intent_analyzer_agent,
)
from src.knob_tuner.sub_agents.intent_analyzer.models import (
    IntentAnalyzerOutput,
    KnobInfo,
    TableInfo,
    WorkloadPattern,
)
from src.knob_tuner.sub_agents.intent_analyzer.tools import (
    _get_db_config,
    check_schema,
    extract_knobs,
    scan_codebase_workload,
    write_knobs_file,
)
from src.knob_tuner.tools.db_connector import DBConfig


class MockToolContext:
    def __init__(self, state: dict | None = None):
        self.state = state if state is not None else {}


# ===========================================================================
# 1. Pydantic Models Validation Tests
# ===========================================================================

def test_table_info_model_validates():
    table = TableInfo(
        name="users",
        columns=["id bigint NOT NULL", "email text NOT NULL"],
        indexes=["users_pkey: CREATE UNIQUE INDEX users_pkey ON users(id)"],
        approximate_row_count=5000,
    )
    assert table.name == "users"
    assert len(table.columns) == 2
    assert table.approximate_row_count == 5000
    dump = table.model_dump()
    assert dump["name"] == "users"
    assert TableInfo.model_validate(dump).name == "users"


def test_knob_info_model_validates():
    knob = KnobInfo(
        name="shared_buffers",
        current_value="128MB",
        unit="MB",
        category="Resource Usage / Memory",
        description="Sets the amount of memory the database server uses for shared memory buffers",
        min_val="16",
        max_val="1073741823",
        context="postmaster",
    )
    assert knob.name == "shared_buffers"
    assert knob.current_value == "128MB"
    assert knob.category == "Resource Usage / Memory"
    dump = knob.model_dump()
    assert KnobInfo.model_validate(dump).name == "shared_buffers"


def test_workload_pattern_model_validates():
    workload = WorkloadPattern(
        query_types=["SELECT", "INSERT", "UPDATE"],
        orm_detected="SQLAlchemy",
        transaction_pattern="Explicit commit/rollback",
        estimated_read_write_ratio="80% Read / 20% Write (Read-Heavy)",
        notable_patterns=["Bulk operations detected", "Connection pooling configured"],
    )
    assert workload.orm_detected == "SQLAlchemy"
    assert "SELECT" in workload.query_types
    assert len(workload.notable_patterns) == 2


def test_intent_analyzer_output_model_validates():
    data = {
        "db_type": "postgres",
        "db_version": "PostgreSQL 16.1",
        "cpu_cores": 8,
        "memory_gb": 32.0,
        "tables": [
            {
                "name": "orders",
                "columns": ["id int", "amount numeric"],
                "indexes": ["orders_pkey"],
                "approximate_row_count": 100000,
            }
        ],
        "available_knobs": [
            {
                "name": "work_mem",
                "current_value": "4MB",
                "unit": "MB",
                "category": "Resource Usage / Memory",
                "description": "Sets the maximum memory to be used for query workspaces",
            }
        ],
        "workload": {
            "query_types": ["SELECT", "INSERT"],
            "orm_detected": "Raw SQL",
            "transaction_pattern": "Auto-commit",
            "estimated_read_write_ratio=":"90% Read / 10% Write (Read-Heavy)",
            "notable_patterns": [],
        },
        "summary_for_recommender": "High read workload on 32GB RAM. Focus on shared_buffers and effective_cache_size.",
    }
    output = IntentAnalyzerOutput.model_validate(data)
    assert output.db_type == "postgres"
    assert output.cpu_cores == 8
    assert output.memory_gb == 32.0
    assert len(output.tables) == 1
    assert len(output.available_knobs) == 1
    assert output.tables[0].name == "orders"


# ===========================================================================
# 2. Agent Factory Test
# ===========================================================================

def test_create_intent_analyzer_agent():
    agent = create_intent_analyzer_agent()
    assert agent.name == "intent_analyzer"
    assert agent.output_key == "intent_analyzer_output"
    assert agent.output_schema == IntentAnalyzerOutput
    assert len(agent.tools) == 4
    tool_names = [t.__name__ for t in agent.tools]
    assert "check_schema" in tool_names
    assert "extract_knobs" in tool_names
    assert "scan_codebase_workload" in tool_names
    assert "write_knobs_file" in tool_names


# ===========================================================================
# 3. DBConfig Helper Tests
# ===========================================================================

def test_get_db_config_from_object():
    cfg = DBConfig(
        host="127.0.0.1",
        port=5432,
        user="test_user",
        password="pwd",
        database="test_db",
        db_type="postgres",
        env="dev",
    )
    tc = MockToolContext({"db_config": cfg})
    assert _get_db_config(tc) is cfg


def test_get_db_config_from_dict():
    cfg_dict = {
        "host": "localhost",
        "port": 3306,
        "user": "root",
        "password": "secret",
        "database": "app_db",
        "db_type": "mysql",
        "env": "production",
    }
    tc = MockToolContext({"db_config": cfg_dict})
    parsed = _get_db_config(tc)
    assert parsed is not None
    assert parsed.host == "localhost"
    assert parsed.port == 3306
    assert parsed.db_type == "mysql"


def test_get_db_config_from_top_level_state():
    tc = MockToolContext({
        "db_type": "postgres",
        "database": "my_db",
        "host": "db.internal",
        "port": 5432,
        "user": "admin",
        "password": "pass",
    })
    parsed = _get_db_config(tc)
    assert parsed is not None
    assert parsed.database == "my_db"
    assert parsed.host == "db.internal"


def test_get_db_config_missing():
    tc = MockToolContext({})
    assert _get_db_config(tc) is None


# ===========================================================================
# 4. check_schema Tool Tests
# ===========================================================================

@patch("src.knob_tuner.sub_agents.intent_analyzer.tools.run_safe_query")
def test_check_schema_postgres_success(mock_query):
    def side_effect(cfg, sql, params=None):
        if "version()" in sql:
            return [{"version": "PostgreSQL 16.0"}]
        if "pg_class" in sql:
            return [
                {"table_name": "accounts", "approx_row_count": 1000},
                {"table_name": "transactions", "approx_row_count": 50000},
            ]
        if "information_schema.columns" in sql:
            return [
                {"table_name": "accounts", "column_name": "id", "data_type": "integer", "is_nullable": "NO"},
                {"table_name": "accounts", "column_name": "balance", "data_type": "numeric", "is_nullable": "YES"},
                {"table_name": "transactions", "column_name": "id", "data_type": "integer", "is_nullable": "NO"},
            ]
        if "pg_indexes" in sql:
            return [
                {"table_name": "accounts", "index_name": "accounts_pkey", "index_def": "CREATE UNIQUE INDEX accounts_pkey ON accounts(id)"},
                {"table_name": "transactions", "index_name": "tx_pkey", "index_def": "CREATE UNIQUE INDEX tx_pkey ON transactions(id)"},
            ]
        return []

    mock_query.side_effect = side_effect
    cfg = DBConfig(host="localhost", port=5432, user="pg", password="", database="test", db_type="postgres", env="test")
    tc = MockToolContext({"db_config": cfg})

    result = check_schema(tc)

    assert "PostgreSQL 16.0" in result
    assert "accounts" in result
    assert "transactions" in result
    assert "schema_info" in tc.state
    tables = tc.state["schema_info"]
    assert len(tables) == 2
    assert tables[0]["name"] == "accounts"
    assert tables[0]["approximate_row_count"] == 1000
    assert len(tables[0]["columns"]) == 2
    assert len(tables[0]["indexes"]) == 1


@patch("src.knob_tuner.sub_agents.intent_analyzer.tools.run_safe_query")
def test_check_schema_mysql_success(mock_query):
    def side_effect(cfg, sql, params=None):
        if "VERSION()" in sql:
            return [{"version": "8.0.35-MySQL"}]
        if "information_schema.tables" in sql:
            return [{"table_name": "items", "approx_row_count": 2500}]
        if "information_schema.columns" in sql:
            return [
                {"table_name": "items", "column_name": "i_id", "data_type": "int", "is_nullable": "NO"},
                {"table_name": "items", "column_name": "i_name", "data_type": "varchar(50)", "is_nullable": "YES"},
            ]
        if "information_schema.statistics" in sql:
            return [
                {"table_name": "items", "index_name": "PRIMARY", "index_def": "i_id"}
            ]
        return []

    mock_query.side_effect = side_effect
    cfg = DBConfig(host="localhost", port=3306, user="root", password="", database="store", db_type="mysql", env="test")
    tc = MockToolContext({"db_config": cfg})

    result = check_schema(tc)

    assert "8.0.35-MySQL" in result
    assert "items" in result
    assert "schema_info" in tc.state
    tables = tc.state["schema_info"]
    assert len(tables) == 1
    assert tables[0]["name"] == "items"
    assert tables[0]["approximate_row_count"] == 2500


def test_check_schema_missing_db_config():
    tc = MockToolContext({})
    result = check_schema(tc)
    assert "ERROR: DBConfig not found in state" in result


def test_check_schema_unsupported_db_type():
    cfg = DBConfig(host="localhost", port=1521, user="system", password="", database="orcl", db_type="oracle", env="test")
    tc = MockToolContext({"db_config": cfg})
    result = check_schema(tc)
    assert "ERROR: Unsupported db_type 'oracle'" in result


@patch("src.knob_tuner.sub_agents.intent_analyzer.tools.run_safe_query")
def test_check_schema_handles_query_exception(mock_query):
    mock_query.side_effect = Exception("Connection refused on port 5432")
    cfg = DBConfig(host="localhost", port=5432, user="pg", password="", database="test", db_type="postgres", env="test")
    tc = MockToolContext({"db_config": cfg})

    result = check_schema(tc)
    assert "ERROR: failed to check schema" in result
    assert "Connection refused" in result


# ===========================================================================
# 5. extract_knobs Tool Tests
# ===========================================================================

@patch("src.knob_tuner.sub_agents.intent_analyzer.tools.run_safe_query")
def test_extract_knobs_postgres_success(mock_query):
    mock_query.return_value = [
        {
            "name": "shared_buffers",
            "current_value": "16384",
            "unit": "8kB",
            "category": "Resource Usage / Memory",
            "description": "Sets the amount of memory the database server uses for shared memory buffers.",
            "min_val": "16",
            "max_val": "1073741823",
            "context": "postmaster",
        },
        {
            "name": "work_mem",
            "current_value": "4096",
            "unit": "kB",
            "category": "Resource Usage / Memory",
            "description": "Sets the maximum memory to be used for query workspaces.",
            "min_val": "64",
            "max_val": "2147483647",
            "context": "user",
        },
    ]
    cfg = DBConfig(host="localhost", port=5432, user="pg", password="", database="test", db_type="postgres", env="test")
    tc = MockToolContext({"db_config": cfg})

    result = extract_knobs(tc)

    assert "Extracted 2 knobs from POSTGRES" in result
    assert "shared_buffers" in result
    assert "knobs_info" in tc.state
    knobs = tc.state["knobs_info"]
    assert len(knobs) == 2
    assert knobs[0]["name"] == "shared_buffers"
    assert knobs[0]["current_value"] == "16384"
    assert knobs[0]["unit"] == "8kB"


@patch("src.knob_tuner.sub_agents.intent_analyzer.tools.run_safe_query")
def test_extract_knobs_mysql_performance_schema(mock_query):
    mock_query.return_value = [
        {"name": "innodb_buffer_pool_size", "current_value": "134217728"},
        {"name": "max_connections", "current_value": "151"},
    ]
    cfg = DBConfig(host="localhost", port=3306, user="root", password="", database="test", db_type="mysql", env="test")
    tc = MockToolContext({"db_config": cfg})

    result = extract_knobs(tc)

    assert "Extracted 2 knobs from MYSQL" in result
    assert "innodb_buffer_pool_size" in result
    assert "knobs_info" in tc.state
    knobs = tc.state["knobs_info"]
    assert len(knobs) == 2
    assert knobs[0]["name"] == "innodb_buffer_pool_size"
    assert knobs[0]["category"] == "InnoDB / Buffer Pool & Memory"


@patch("src.knob_tuner.sub_agents.intent_analyzer.tools.run_safe_query")
def test_extract_knobs_mysql_fallback_to_show_variables(mock_query):
    def side_effect(cfg, sql, params=None):
        if "performance_schema" in sql:
            raise Exception("Access denied for performance_schema")
        return [
            {"Variable_name": "innodb_log_file_size", "Value": "50331648"},
            {"Variable_name": "query_cache_size", "Value": "1048576"},
        ]

    mock_query.side_effect = side_effect
    cfg = DBConfig(host="localhost", port=3306, user="root", password="", database="test", db_type="mysql", env="test")
    tc = MockToolContext({"db_config": cfg})

    result = extract_knobs(tc)

    assert "Extracted 2 knobs from MYSQL" in result
    assert "innodb_log_file_size" in result
    assert "knobs_info" in tc.state
    assert len(tc.state["knobs_info"]) == 2


def test_extract_knobs_missing_config():
    tc = MockToolContext({})
    result = extract_knobs(tc)
    assert "ERROR: DBConfig not found in state" in result


# ===========================================================================
# 6. scan_codebase_workload Tool Tests
# ===========================================================================

def test_scan_codebase_workload_python_sqlalchemy():
    with tempfile.TemporaryDirectory() as target_dir:
        app_py = Path(target_dir) / "app.py"
        app_py.write_text(
            """
            from sqlalchemy import create_engine
            from sqlalchemy.orm import sessionmaker

            engine = create_engine('postgresql://localhost/mydb')
            Session = sessionmaker(bind=engine)
            session = Session()

            def get_user(uid):
                return session.query(User).filter_by(id=uid).first()

            def create_users(user_list):
                session.bulk_insert_mappings(User, user_list)
                session.commit()

            async def fetch_stats():
                result = await session.execute("SELECT COUNT(*), AVG(amount) FROM orders GROUP BY user_id")
                return result
            """
        )

        tc = MockToolContext({"target": target_dir})
        result = scan_codebase_workload(tc)

        assert "Workload Pattern Analysis" in result
        assert "SQLAlchemy" in result
        assert "workload_info" in tc.state

        workload = tc.state["workload_info"]
        assert "SQLAlchemy" in workload["orm_detected"]
        assert "SELECT" in workload["query_types"]
        assert any("Bulk" in p for p in workload["notable_patterns"])
        assert any("Async" in p or "concurrent" in p for p in workload["notable_patterns"])


def test_scan_codebase_workload_java_jpa():
    with tempfile.TemporaryDirectory() as target_dir:
        repo_java = Path(target_dir) / "OrderRepository.java"
        repo_java.write_text(
            """
            package com.example.repo;

            import org.hibernate.annotations.Query;
            import javax.persistence.Entity;
            import org.springframework.transaction.annotation.Transactional;

            @Entity
            public class OrderRepository {
                @Transactional
                public void saveOrder(Order o) {
                    entityManager.persist(o);
                }

                @Query("SELECT o FROM Order o JOIN o.items WHERE o.status = 'COMPLETED'")
                public List<Order> findCompleted();
            }
            """
        )

        tc = MockToolContext({"target": target_dir})
        result = scan_codebase_workload(tc)

        assert "Hibernate / JPA" in result
        assert "Declarative (@Transactional)" in result
        assert "workload_info" in tc.state


def test_scan_codebase_workload_go_gorm():
    with tempfile.TemporaryDirectory() as target_dir:
        main_go = Path(target_dir) / "main.go"
        main_go.write_text(
            """
            package main

            import "gorm.io/gorm"

            func GetUser(db *gorm.DB, id uint) User {
                var user User
                db.Where("id = ?", id).Find(&user)
                return user
            }

            func UpdateUser(db *gorm.DB, user *User) {
                db.Save(user)
            }
            """
        )

        tc = MockToolContext({"target": target_dir})
        result = scan_codebase_workload(tc)

        assert "GORM" in result
        assert "workload_info" in tc.state


def test_scan_codebase_workload_missing_target():
    tc = MockToolContext({})
    result = scan_codebase_workload(tc)
    assert "ERROR: target path not set in state" in result


def test_scan_codebase_workload_nonexistent_directory():
    tc = MockToolContext({"target": "/nonexistent/path/for/sure/12345"})
    result = scan_codebase_workload(tc)
    assert "ERROR: target path not set in state or directory does not exist" in result


# ===========================================================================
# 7. write_knobs_file Tool Tests
# ===========================================================================

def test_write_knobs_file_success():
    with tempfile.TemporaryDirectory() as target_dir:
        knobs_data = [
            {"name": "shared_buffers", "current_value": "128MB", "unit": "MB", "category": "Memory"},
            {"name": "work_mem", "current_value": "4MB", "unit": "MB", "category": "Memory"},
        ]
        tc = MockToolContext({"knob_path": target_dir, "knobs_info": knobs_data})

        result = write_knobs_file(tc)

        assert "OK: wrote 2 knobs" in result
        expected_path = os.path.join(target_dir, "knobs.json")
        assert os.path.isfile(expected_path)

        with open(expected_path, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        assert len(loaded) == 2
        assert loaded[0]["name"] == "shared_buffers"


def test_write_knobs_file_missing_knobs_info():
    tc = MockToolContext({"knob_path": "/tmp"})
    result = write_knobs_file(tc)
    assert "ERROR: knobs_info not found in state or empty" in result


def test_write_knobs_file_uses_target_as_fallback():
    with tempfile.TemporaryDirectory() as target_dir:
        knobs_data = [{"name": "max_connections", "current_value": "100"}]
        tc = MockToolContext({"target": target_dir, "knobs_info": knobs_data})

        result = write_knobs_file(tc)

        assert "OK: wrote 1 knobs" in result
        assert os.path.isfile(os.path.join(target_dir, "knobs.json"))
