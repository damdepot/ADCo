import argparse
import asyncio
import json
import os
from unittest.mock import AsyncMock, patch

import pytest

from src.adco.main import (
    DEFAULT_MODEL,
    build_parser,
    main,
    run_pipeline,
)


def test_build_parser():
    parser = build_parser()
    assert isinstance(parser, argparse.ArgumentParser)

    args = parser.parse_args(["my_target", "--db-name", "test_db"])
    assert args.target == "my_target"
    assert args.db_name == "test_db"
    assert args.model == DEFAULT_MODEL
    assert args.db_type is None
    assert args.db_config == "db.config"
    assert args.cpu_cores is None
    assert args.memory is None
    assert args.sandbox_dir is None
    assert args.log_file == "logs/adco.log"
    assert args.output_path == "out/adco/result.json"
    assert args.intent_output == "out/adco/intent_result.json"
    assert args.rewriter_output == "out/adco/rewriter_result.json"
    assert args.tuner_output == "out/adco/knob_result.json"
    assert args.knob_path == "out/adco/knobs"
    assert args.production_db is False
    assert args.dry_run is False
    assert args.verbose is False
    assert args.mode == "all"
    assert args.apply_mode == "dynamic"
    assert args.buffer_time == 0.0


def test_build_parser_custom_options():
    parser = build_parser()
    args = parser.parse_args([
        "my_target",
        "--db-name", "my_db",
        "--db-type", "mysql",
        "--model", "gemini-test",
        "--cpu-cores", "4",
        "--memory", "8",
        "--production-db",
        "--dry-run",
        "-v",
        "--mode", "tune-only",
        "--apply-mode", "persist-static",
        "--buffer-time", "2.5",
    ])
    assert args.target == "my_target"
    assert args.db_name == "my_db"
    assert args.db_type == "mysql"
    assert args.model == "gemini-test"
    assert args.cpu_cores == "4"
    assert args.memory == "8"
    assert args.production_db is True
    assert args.dry_run is True
    assert args.verbose is True
    assert args.mode == "tune-only"
    assert args.apply_mode == "persist-static"
    assert args.buffer_time == 2.5



@patch("src.adco.main.intent_analyzer_pipeline", new_callable=AsyncMock)
@patch("src.adco.main.rewriter_pipeline", new_callable=AsyncMock)
@patch("src.adco.main.tuner_pipeline", new_callable=AsyncMock)
def test_run_pipeline_success_with_tuning(mock_tuner, mock_rewriter, mock_intent, tmp_path):
    target_dir = tmp_path / "app"
    target_dir.mkdir()
    sandbox_dir = tmp_path / "sandbox"
    sandbox_dir.mkdir()

    log_file = tmp_path / "adco.log"
    out_file = tmp_path / "result.json"
    intent_out = tmp_path / "intent.json"
    rewriter_out = tmp_path / "rewriter.json"
    tuner_out = tmp_path / "tuner.json"

    intent_out.write_text(json.dumps({"intent_output": {"queries": "SELECT * FROM users"}}))
    rewriter_out.write_text(json.dumps({"status": "PASS", "sandbox": str(sandbox_dir)}))
    tuner_out.write_text(json.dumps({"staging_validated": True}))

    mock_intent.return_value = {
        "intent_output": {
            "queries": "SELECT * FROM users JOIN orders",
            "orm": "Peewee",
            "optimization_targets": [{"file": "main.py", "description": "Optimize JOIN query"}],
            "workload": {
                "query_types": ["SELECT", "JOIN"],
                "orm_detected": "Peewee",
            },
        },
        "workload_info": {
            "query_types": ["SELECT", "JOIN"],
            "orm_detected": "Peewee",
        },
    }

    mock_rewriter.return_value = {
        "verifier_output": {"status": "PASS"},
        "sandbox": str(sandbox_dir),
    }
    mock_tuner.return_value = {
        "staging_validated": True,
    }

    res = asyncio.run(
        run_pipeline(
            target=str(target_dir),
            model="gemini-3.5-flash-lite",
            log_file=str(log_file),
            output_path=str(out_file),
            intent_output_path=str(intent_out),
            rewriter_output=str(rewriter_out),
            db_name="testdb",
            db_type="postgres",
            tuner_output=str(tuner_out),
        )
    )

    assert mock_intent.called
    assert mock_rewriter.called
    assert mock_tuner.called

    # Verify tuner received sandbox path and workload_info
    tuner_call_kwargs = mock_tuner.call_args.kwargs
    assert tuner_call_kwargs["target"] == str(sandbox_dir)
    assert "workload_info" in tuner_call_kwargs["extra_initial_state"]
    assert "SELECT" in tuner_call_kwargs["extra_initial_state"]["workload_info"]["query_types"]

    assert os.path.exists(out_file)
    assert res["target"] == str(target_dir.resolve())
    assert res["sandbox"] == str(sandbox_dir)
    assert "intent_analyzer" in res
    assert "rewriter" in res
    assert "knob_tuner" in res


@patch("src.adco.main.intent_analyzer_pipeline", new_callable=AsyncMock)
@patch("src.adco.main.rewriter_pipeline", new_callable=AsyncMock)
@patch("src.adco.main.tuner_pipeline", new_callable=AsyncMock)
def test_run_pipeline_tune_only(mock_tuner, mock_rewriter, mock_intent, tmp_path):
    target_dir = tmp_path / "app"
    target_dir.mkdir()

    log_file = tmp_path / "adco.log"
    out_file = tmp_path / "result.json"

    mock_intent.return_value = {
        "workload_info": {"query_types": ["SELECT"]}
    }
    mock_tuner.return_value = {"staging_validated": True}

    res = asyncio.run(
        run_pipeline(
            target=str(target_dir),
            mode="tune-only",
            log_file=str(log_file),
            output_path=str(out_file),
            db_name="testdb",
        )
    )

    assert mock_intent.called
    assert not mock_rewriter.called
    assert mock_tuner.called

    tuner_call_kwargs = mock_tuner.call_args.kwargs
    assert tuner_call_kwargs["target"] == str(target_dir.resolve())

    assert os.path.exists(out_file)
    assert res["mode"] == "tune-only"
    assert res["sandbox"] == str(target_dir.resolve())


@patch("src.adco.main.intent_analyzer_pipeline", new_callable=AsyncMock)
@patch("src.adco.main.rewriter_pipeline", new_callable=AsyncMock)
@patch("src.adco.main.tuner_pipeline", new_callable=AsyncMock)
def test_run_pipeline_rewrite_only(mock_tuner, mock_rewriter, mock_intent, tmp_path):
    target_dir = tmp_path / "app"
    target_dir.mkdir()
    sandbox_dir = tmp_path / "sandbox"
    sandbox_dir.mkdir()

    log_file = tmp_path / "adco.log"
    out_file = tmp_path / "result.json"
    intent_out = tmp_path / "intent.json"
    rewriter_out = tmp_path / "rewriter.json"

    intent_out.write_text(json.dumps({"intent_output": {"optimization_targets": [{"file": "main.py"}]}}))
    rewriter_out.write_text(json.dumps({"status": "PASS", "sandbox": str(sandbox_dir)}))

    mock_intent.return_value = {
        "intent_output": {
            "optimization_targets": [{"file": "main.py", "description": "Optimize query"}]
        }
    }
    mock_rewriter.return_value = {
        "verifier_output": {"status": "PASS"},
        "sandbox": str(sandbox_dir),
    }

    res = asyncio.run(
        run_pipeline(
            target=str(target_dir),
            model="gemini-3.5-flash-lite",
            mode="rewrite-only",
            log_file=str(log_file),
            output_path=str(out_file),
            intent_output_path=str(intent_out),
            rewriter_output=str(rewriter_out),
            db_name="testdb",
        )
    )

    assert mock_intent.called
    assert mock_rewriter.called
    assert not mock_tuner.called  # Tuning was skipped!

    assert os.path.exists(out_file)
    assert res["mode"] == "rewrite-only"
    assert res["sandbox"] == str(sandbox_dir)
    assert "rewriter" in res


@patch("src.adco.main.intent_analyzer_pipeline", new_callable=AsyncMock)
@patch("src.adco.main.rewriter_pipeline", new_callable=AsyncMock)
def test_run_pipeline_rewriter_fail_fast(mock_rewriter, mock_intent, tmp_path):
    target_dir = tmp_path / "app"
    target_dir.mkdir()

    mock_intent.return_value = {
        "intent_output": {
            "optimization_targets": [{"file": "main.py", "description": "Optimize query"}]
        }
    }
    mock_rewriter.return_value = {
        "verifier_output": {
            "status": "FAIL",
            "category": "syntax_error",
            "reason": "Invalid Python syntax",
        }
    }

    with pytest.raises(RuntimeError) as exc_info:
        asyncio.run(
            run_pipeline(
                target=str(target_dir),
                db_name="testdb",
            )
        )

    assert "Code rewriter FAILED" in str(exc_info.value)


@patch("src.adco.main.intent_analyzer_pipeline", new_callable=AsyncMock)
def test_run_pipeline_intent_fail_fast(mock_intent, tmp_path):
    target_dir = tmp_path / "app"
    target_dir.mkdir()

    mock_intent.return_value = {"intent_output": {}}

    with pytest.raises(RuntimeError) as exc_info:
        asyncio.run(
            run_pipeline(
                target=str(target_dir),
                db_name="testdb",
            )
        )

    assert "Intent analyzer returned no output or no optimization_targets" in str(exc_info.value)


@patch("src.adco.main.intent_analyzer_pipeline", new_callable=AsyncMock)
@patch("src.adco.main.rewriter_pipeline", new_callable=AsyncMock)
@patch("src.adco.main.tuner_pipeline", new_callable=AsyncMock)
def test_run_pipeline_forwards_db_type_to_intent_analyzer(mock_tuner, mock_rewriter, mock_intent, tmp_path):
    target_dir = tmp_path / "app"
    target_dir.mkdir()
    sandbox_dir = tmp_path / "sandbox"
    sandbox_dir.mkdir()

    log_file = tmp_path / "adco.log"
    out_file = tmp_path / "result.json"
    intent_out = tmp_path / "intent.json"
    rewriter_out = tmp_path / "rewriter.json"

    intent_out.write_text(json.dumps({"intent_output": {"optimization_targets": [{"file": "main.py"}]}}))
    rewriter_out.write_text(json.dumps({"status": "PASS", "sandbox": str(sandbox_dir)}))

    mock_intent.return_value = {
        "intent_output": {"optimization_targets": [{"file": "main.py"}]}
    }
    mock_rewriter.return_value = {
        "verifier_output": {"status": "PASS"},
        "sandbox": str(sandbox_dir),
    }

    asyncio.run(
        run_pipeline(
            target=str(target_dir),
            mode="rewrite-only",
            log_file=str(log_file),
            output_path=str(out_file),
            intent_output_path=str(intent_out),
            rewriter_output=str(rewriter_out),
            db_name="testdb",
            db_type="postgres",
        )
    )

    assert mock_intent.called
    assert mock_intent.call_args.kwargs["db_type"] == "postgres"


@patch("src.adco.main.intent_analyzer_pipeline", new_callable=AsyncMock)
@patch("src.adco.main.rewriter_pipeline", new_callable=AsyncMock)
@patch("src.adco.main.tuner_pipeline", new_callable=AsyncMock)
def test_run_pipeline_filters_foreign_engine_targets(mock_tuner, mock_rewriter, mock_intent, tmp_path):
    target_dir = tmp_path / "app"
    target_dir.mkdir()
    sandbox_dir = tmp_path / "sandbox"
    sandbox_dir.mkdir()

    log_file = tmp_path / "adco.log"
    out_file = tmp_path / "result.json"
    intent_out = tmp_path / "intent.json"
    rewriter_out = tmp_path / "rewriter.json"

    intent_out.write_text(json.dumps({"intent_output": {"optimization_targets": [{"file": "main.py"}]}}))
    rewriter_out.write_text(json.dumps({"status": "PASS", "sandbox": str(sandbox_dir)}))

    mock_intent.return_value = {
        "intent_output": {
            "optimization_targets": [
                {"file": "drivers/postgresdriver.py"},
                {"file": "drivers/mysqldriver.py"},
                {"file": "db.py"},
            ]
        }
    }
    mock_rewriter.return_value = {
        "verifier_output": {"status": "PASS"},
        "sandbox": str(sandbox_dir),
    }

    asyncio.run(
        run_pipeline(
            target=str(target_dir),
            mode="rewrite-only",
            log_file=str(log_file),
            output_path=str(out_file),
            intent_output_path=str(intent_out),
            rewriter_output=str(rewriter_out),
            db_name="testdb",
            db_type="postgres",
        )
    )

    targets = mock_rewriter.call_args.kwargs["extra_initial_state"]["intent_output"]["optimization_targets"]
    files = [t["file"] for t in targets]
    assert "drivers/postgresdriver.py" in files
    assert "db.py" in files
    assert "drivers/mysqldriver.py" not in files


@patch("src.adco.main.intent_analyzer_pipeline", new_callable=AsyncMock)
@patch("src.adco.main.rewriter_pipeline", new_callable=AsyncMock)
@patch("src.adco.main.tuner_pipeline", new_callable=AsyncMock)
def test_run_pipeline_fallback_targets_filtered_by_engine(mock_tuner, mock_rewriter, mock_intent, tmp_path):
    target_dir = tmp_path / "app"
    target_dir.mkdir()
    sandbox_dir = tmp_path / "sandbox"
    sandbox_dir.mkdir()

    log_file = tmp_path / "adco.log"
    out_file = tmp_path / "result.json"
    intent_out = tmp_path / "intent.json"
    rewriter_out = tmp_path / "rewriter.json"

    intent_out.write_text(json.dumps({"intent_output": {"queries": "x"}}))
    rewriter_out.write_text(json.dumps({"status": "PASS", "sandbox": str(sandbox_dir)}))

    mock_intent.return_value = {
        "intent_output": {"queries": "x"},
        "file_selector_output": {
            "files": ["drivers/postgresdriver.py", "drivers/mysqldriver.py", "db.py"],
            "entry_point": "main.py",
        },
    }
    mock_rewriter.return_value = {
        "verifier_output": {"status": "PASS"},
        "sandbox": str(sandbox_dir),
    }

    asyncio.run(
        run_pipeline(
            target=str(target_dir),
            mode="rewrite-only",
            log_file=str(log_file),
            output_path=str(out_file),
            intent_output_path=str(intent_out),
            rewriter_output=str(rewriter_out),
            db_name="testdb",
            db_type="postgres",
        )
    )

    targets = mock_rewriter.call_args.kwargs["extra_initial_state"]["intent_output"]["optimization_targets"]
    files = [t["file"] for t in targets]
    assert files == ["drivers/postgresdriver.py", "db.py"]


@patch("src.adco.main.run_pipeline", new_callable=AsyncMock)
def test_main_success(mock_run, monkeypatch, capsys, tmp_path):
    d = tmp_path / "target_app"
    d.mkdir()

    monkeypatch.setattr(
        "sys.argv",
        [
            "adco",
            str(d),
            "--mode",
            "rewrite-only",
            "--sandbox-dir",
            str(tmp_path / "sandbox"),
            "--db-type",
            "postgres",
            "--db-name",
            "testdb",
        ],
    )

    with pytest.raises(SystemExit) as exc_info:
        main()

    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    assert "=== ADCo Pipeline COMPLETED ===" in captured.out


def test_main_invalid_target(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["adco", "nonexistent_dir_123", "--db-name", "mydb"])

    with pytest.raises(SystemExit) as exc_info:
        main()

    assert exc_info.value.code == 2
    captured = capsys.readouterr()
    assert "ERROR: target is not a directory" in captured.err


def test_main_missing_required_all_mode(monkeypatch, capsys, tmp_path):
    d = tmp_path / "target_app"
    d.mkdir()
    monkeypatch.setattr("sys.argv", ["adco", str(d), "--mode", "all"])

    with pytest.raises(SystemExit) as exc_info:
        main()

    assert exc_info.value.code == 2
    captured = capsys.readouterr()
    assert "missing required argument(s) for mode 'all'" in captured.err
    for flag in ("--sandbox-dir", "--db-type", "--db-name", "--cpu-cores", "--memory"):
        assert flag in captured.err


def test_main_missing_required_rewrite_only_mode(monkeypatch, capsys, tmp_path):
    d = tmp_path / "target_app"
    d.mkdir()
    monkeypatch.setattr("sys.argv", ["adco", str(d), "--mode", "rewrite-only"])

    with pytest.raises(SystemExit) as exc_info:
        main()

    assert exc_info.value.code == 2
    captured = capsys.readouterr()
    assert "missing required argument(s) for mode 'rewrite-only'" in captured.err
    for flag in ("--sandbox-dir", "--db-type", "--db-name"):
        assert flag in captured.err
    assert "--cpu-cores" not in captured.err
    assert "--memory" not in captured.err


def test_main_missing_required_tune_only_mode(monkeypatch, capsys, tmp_path):
    d = tmp_path / "target_app"
    d.mkdir()
    monkeypatch.setattr(
        "sys.argv",
        ["adco", str(d), "--mode", "tune-only", "--db-type", "postgres", "--db-name", "testdb"],
    )

    with pytest.raises(SystemExit) as exc_info:
        main()

    assert exc_info.value.code == 2
    captured = capsys.readouterr()
    assert "missing required argument(s) for mode 'tune-only'" in captured.err
    assert "--cpu-cores" in captured.err
    assert "--memory" in captured.err
    assert "--sandbox-dir" not in captured.err


@patch("src.adco.main.run_pipeline", new_callable=AsyncMock)
@patch("src.adco.main.check_auth")
def test_main_all_mode_accepts_required_args(mock_auth, mock_run, monkeypatch, capsys, tmp_path):
    d = tmp_path / "target_app"
    d.mkdir()
    monkeypatch.setattr(
        "sys.argv",
        [
            "adco",
            str(d),
            "--mode",
            "all",
            "--sandbox-dir",
            str(tmp_path / "sandbox"),
            "--db-type",
            "postgres",
            "--db-name",
            "testdb",
            "--cpu-cores",
            "4",
            "--memory",
            "8",
        ],
    )

    with pytest.raises(SystemExit) as exc_info:
        main()

    assert exc_info.value.code == 0
    assert mock_auth.called
    assert mock_run.called


@pytest.mark.parametrize("bad_cpu,bad_mem", [("auto", "8"), ("4", "auto"), ("0", "8"), ("4", "-1")])
def test_main_invalid_budget_exits_2_before_auth(
    monkeypatch, capsys, tmp_path, bad_cpu, bad_mem
):
    d = tmp_path / "target_app"
    d.mkdir()
    monkeypatch.setattr(
        "sys.argv",
        [
            "adco",
            str(d),
            "--mode",
            "tune-only",
            "--db-type",
            "postgres",
            "--db-name",
            "testdb",
            "--cpu-cores",
            bad_cpu,
            "--memory",
            bad_mem,
        ],
    )

    with patch("src.adco.main.check_auth") as mock_auth, patch(
        "src.adco.main.run_pipeline", new_callable=AsyncMock
    ) as mock_run:
        with pytest.raises(SystemExit) as exc_info:
            main()

    assert exc_info.value.code == 2
    assert not mock_auth.called
    assert not mock_run.called
    assert "ERROR:" in capsys.readouterr().err


def test_main_valid_budget_reaches_auth(monkeypatch, capsys, tmp_path):
    d = tmp_path / "target_app"
    d.mkdir()
    monkeypatch.setattr(
        "sys.argv",
        [
            "adco",
            str(d),
            "--mode",
            "tune-only",
            "--db-type",
            "postgres",
            "--db-name",
            "testdb",
            "--cpu-cores",
            "4",
            "--memory",
            "8",
        ],
    )
    with patch("src.adco.main.check_auth") as mock_auth, patch(
        "src.adco.main.run_pipeline", new_callable=AsyncMock
    ) as mock_run:
        mock_run.return_value = {"tuner_run_dir": str(tmp_path), "tuner_status": "PASS"}
        with pytest.raises(SystemExit) as exc_info:
            main()

    assert exc_info.value.code == 0
    assert mock_auth.called
    assert mock_run.called
    assert mock_run.call_args.kwargs["apply_mode"] == "dynamic"
