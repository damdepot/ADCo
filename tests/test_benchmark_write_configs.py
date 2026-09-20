"""Tests for benchmark write_configs.py and helper scripts."""

import configparser
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT_DIR = Path(__file__).resolve().parent.parent
WRITE_CONFIGS_SCRIPT = ROOT_DIR / "benchmarks" / "helpers" / "write_configs.py"
SETUP_BASELINE_SH = ROOT_DIR / "benchmarks" / "helpers" / "setup_baseline.sh"
SETUP_PRODUCTION_SH = ROOT_DIR / "benchmarks" / "helpers" / "setup_production.sh"


def _create_mock_project(tmp_path: Path) -> Path:
    """Create a mock project structure with benchmark tools."""
    (tmp_path / "benchmarks" / "tools" / "smallbank").mkdir(parents=True, exist_ok=True)
    (tmp_path / "benchmarks" / "tools" / "tpcc").mkdir(parents=True, exist_ok=True)
    return tmp_path


def test_write_configs_baseline(tmp_path: Path):
    """Test write_configs.py with baseline.config mapping databases to tool names."""
    project_root = _create_mock_project(tmp_path)
    baseline_cfg = project_root / "benchmarks" / "baseline.config"
    baseline_cfg.write_text(
        "[mysql]\n"
        "host = 127.0.0.1\n"
        "port = 3308\n"
        "user = root\n"
        "password = mysql_root_password\n"
        "database = smallbank\n\n"
        "[postgres]\n"
        "host = 127.0.0.1\n"
        "port = 5434\n"
        "user = postgres\n"
        "password = postgres\n"
        "database = smallbank\n"
    )

    result = subprocess.run(
        [sys.executable, str(WRITE_CONFIGS_SCRIPT), str(project_root), "", str(baseline_cfg)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.returncode == 0

    # Verify smallbank tool db.config
    sb_cfg_path = project_root / "benchmarks" / "tools" / "smallbank" / "db.config"
    assert sb_cfg_path.exists()
    sb_cfg = configparser.ConfigParser()
    sb_cfg.read(sb_cfg_path)
    assert sb_cfg["mysql"]["database"] == "smallbank"
    assert sb_cfg["mysql"]["port"] == "3308"
    assert sb_cfg["postgres"]["database"] == "smallbank"
    assert sb_cfg["postgres"]["port"] == "5434"

    # Verify tpcc tool db.config
    tpcc_cfg_path = project_root / "benchmarks" / "tools" / "tpcc" / "db.config"
    assert tpcc_cfg_path.exists()
    tpcc_cfg = configparser.ConfigParser()
    tpcc_cfg.read(tpcc_cfg_path)
    assert tpcc_cfg["mysql"]["database"] == "tpcc"
    assert tpcc_cfg["mysql"]["port"] == "3308"
    assert tpcc_cfg["postgres"]["database"] == "tpcc"
    assert tpcc_cfg["postgres"]["port"] == "5434"


def test_write_configs_production_prefix(tmp_path: Path):
    """Test write_configs.py with prefix='production' and root db.config."""
    project_root = _create_mock_project(tmp_path)
    db_cfg = project_root / "db.config"
    db_cfg.write_text(
        "[production.mysql]\n"
        "host = 10.0.0.1\n"
        "port = 3306\n"
        "user = prod_root\n"
        "password = prod_secret\n\n"
        "[production.postgres]\n"
        "host = 10.0.0.2\n"
        "port = 5432\n"
        "user = prod_postgres\n"
        "password = prod_pgsecret\n"
    )

    result = subprocess.run(
        [sys.executable, str(WRITE_CONFIGS_SCRIPT), str(project_root), "production", str(db_cfg)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.returncode == 0

    # Verify smallbank
    sb_cfg = configparser.ConfigParser()
    sb_cfg.read(project_root / "benchmarks" / "tools" / "smallbank" / "db.config")
    assert sb_cfg["mysql"]["host"] == "10.0.0.1"
    assert sb_cfg["mysql"]["port"] == "3306"
    assert sb_cfg["mysql"]["user"] == "prod_root"
    assert sb_cfg["mysql"]["database"] == "smallbank"
    assert sb_cfg["postgres"]["host"] == "10.0.0.2"
    assert sb_cfg["postgres"]["port"] == "5432"
    assert sb_cfg["postgres"]["user"] == "prod_postgres"
    assert sb_cfg["postgres"]["database"] == "smallbank"

    # Verify tpcc
    tpcc_cfg = configparser.ConfigParser()
    tpcc_cfg.read(project_root / "benchmarks" / "tools" / "tpcc" / "db.config")
    assert tpcc_cfg["mysql"]["host"] == "10.0.0.1"
    assert tpcc_cfg["mysql"]["port"] == "3306"
    assert tpcc_cfg["mysql"]["user"] == "prod_root"
    assert tpcc_cfg["mysql"]["database"] == "tpcc"
    assert tpcc_cfg["postgres"]["host"] == "10.0.0.2"
    assert tpcc_cfg["postgres"]["port"] == "5432"
    assert tpcc_cfg["postgres"]["user"] == "prod_postgres"
    assert tpcc_cfg["postgres"]["database"] == "tpcc"


def test_write_configs_postgres_only(tmp_path: Path):
    """Test write_configs.py with only postgres in config (no mysql)."""
    project_root = _create_mock_project(tmp_path)
    pg_only_cfg = project_root / "pg_only.config"
    pg_only_cfg.write_text(
        "[postgres]\n"
        "host = 127.0.0.1\n"
        "port = 5432\n"
        "user = postgres\n"
        "password = postgres\n"
    )

    result = subprocess.run(
        [sys.executable, str(WRITE_CONFIGS_SCRIPT), str(project_root), "", str(pg_only_cfg)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.returncode == 0

    # Verify smallbank has postgres section and no mysql section
    sb_cfg = configparser.ConfigParser()
    sb_cfg.read(project_root / "benchmarks" / "tools" / "smallbank" / "db.config")
    assert "postgres" in sb_cfg
    assert sb_cfg["postgres"]["database"] == "smallbank"
    assert "mysql" not in sb_cfg

    # Verify tpcc has postgres section and no mysql section
    tpcc_cfg = configparser.ConfigParser()
    tpcc_cfg.read(project_root / "benchmarks" / "tools" / "tpcc" / "db.config")
    assert "postgres" in tpcc_cfg
    assert tpcc_cfg["postgres"]["database"] == "tpcc"
    assert "mysql" not in tpcc_cfg


def test_setup_baseline_sh_execution():
    """Test execution of setup_baseline.sh."""
    res = subprocess.run(
        ["bash", str(SETUP_BASELINE_SH), "smallbank"],
        capture_output=True,
        text=True,
        cwd=str(ROOT_DIR),
    )
    assert res.returncode == 0
    assert "Wrote" in res.stdout


def test_setup_production_sh_execution():
    """Test execution of setup_production.sh."""
    res = subprocess.run(
        ["bash", str(SETUP_PRODUCTION_SH), "tpcc"],
        capture_output=True,
        text=True,
        cwd=str(ROOT_DIR),
    )
    assert res.returncode == 0
    assert "Wrote" in res.stdout
