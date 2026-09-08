"""Tools for knob_checker sub-agent — apply knobs to staging, restart staging DB, test staging DB."""

import os
from typing import Any

from google.adk.tools import ToolContext

from src.knob_tuner.tools.benchmark_tools import run_sysbench_benchmark
from src.knob_tuner.tools.db_connector import DBConfig, get_connection
from src.knob_tuner.tools.db_tools import apply_knobs, test_database, verify_active_knobs
from src.knob_tuner.tools.docker_tools import (
    is_docker_available,
    recreate_docker_db,
    restart_docker_db,
    get_container_host_port,
    start_staging_db,
    stop_staging_db,
)
from src.knob_tuner.tools.file_tools import read_json_file
from src.knob_tuner.sub_agents.knob_checker.models import (
    BenchmarkResult,
    KnobCheckIssue,
    SysbenchMetrics,
)


_REGRESSION_THRESHOLD_PCT = 5.0  # FAIL only if TPS drops more than this percentage


def _load_selected_knobs(tool_context: ToolContext) -> list[dict[str, Any]]:
    """Helper to retrieve selected knob recommendations from state or file."""
    # 1. State selected_knobs or recommended_knobs
    for state_key in ("selected_knobs", "recommended_knobs"):
        selected = tool_context.state.get(state_key)
        if isinstance(selected, dict):
            return [{"name": k, "value": v, "restart_required": False} for k, v in selected.items()]
        elif isinstance(selected, list):
            knobs: list[dict[str, Any]] = []
            for item in selected:
                if hasattr(item, "model_dump"):
                    d = item.model_dump()
                elif isinstance(item, dict):
                    d = item
                else:
                    continue
                name = d.get("knob") or d.get("name")
                val = d.get("recommended_value") or d.get("value")
                if name and val is not None:
                    knobs.append({"name": name, "value": val, "restart_required": d.get("restart_required", False)})
            if knobs:
                return knobs

    # 2. State knob_recommender_output
    output = tool_context.state.get("knob_recommender_output")
    if output:
        raw_recs = []
        if hasattr(output, "recommendations"):
            raw_recs = output.recommendations
        elif isinstance(output, dict):
            raw_recs = output.get("recommendations", [])

        knobs = []
        for item in raw_recs:
            if hasattr(item, "model_dump"):
                d = item.model_dump()
            elif isinstance(item, dict):
                d = item
            else:
                continue
            name = d.get("knob") or d.get("name")
            val = d.get("recommended_value") or d.get("value")
            if name and val is not None:
                knobs.append({"name": name, "value": val, "restart_required": d.get("restart_required", False)})
        if knobs:
            return knobs

    # 3. Read from file
    knob_path = (
        tool_context.state.get("knob_path")
        or tool_context.state.get("target")
        or "."
    )
    sel_file = os.path.join(knob_path, "knobs-selected.json")
    if os.path.isfile(sel_file):
        try:
            content = read_json_file(sel_file)
            if isinstance(content, list):
                knobs = []
                for item in content:
                    if isinstance(item, dict):
                        name = item.get("knob") or item.get("name")
                        val = item.get("recommended_value") or item.get("value")
                        if name and val is not None:
                            knobs.append({"name": name, "value": val, "restart_required": item.get("restart_required", False)})
                return knobs
        except Exception:
            pass

    return []


def apply_knobs_staging(tool_context: ToolContext) -> str:
    """Apply recommended database configuration knobs to the staging database.

    Args:
        tool_context: ADK tool execution context.

    Returns:
        Status summary of applied knobs or error message.
    """
    cfg = tool_context.state.get("staging_db_config")
    if not cfg:
        return "ERROR: Staging DBConfig not found in state"

    knobs = _load_selected_knobs(tool_context)
    if not knobs:
        return "ERROR: No selected knob recommendations found to apply to staging"

    try:
        if cfg.db_type.lower() in ("postgres", "postgresql"):
            try:
                conn = get_connection(cfg)
                if conn:
                    try:
                        if hasattr(conn, "autocommit"):
                            try:
                                conn.autocommit = True
                            except Exception:
                                pass
                        with conn.cursor() as cur:
                            cur.execute("ALTER SYSTEM RESET ALL;")
                            cur.execute("SELECT pg_reload_conf();")
                    finally:
                        conn.close()
            except Exception as e:
                pass  # Ignore if we can't reset, let apply_knobs fail if it's broken

        results = apply_knobs(knobs, cfg, dry_run=False)
        tool_context.state["staging_applied_knobs"] = results

        applied_count = sum(1 for r in results if r.get("status") == "applied")
        failed_count = sum(1 for r in results if r.get("status") == "failed")

        lines = [
            f"Staging Knob Application Summary: {applied_count}/{len(results)} applied successfully, {failed_count} failed.",
            "",
            "## Knob Application Details",
        ]
        for r in results:
            status = r.get("status", "unknown")
            kname = r.get("knob", "")
            kval = r.get("value", "")
            err = r.get("error")
            err_str = f" (Error: {err})" if err else ""
            lines.append(f"- **{kname}** -> `{kval}`: **{status.upper()}**{err_str}")

        return "\n".join(lines)
    except Exception as e:
        return f"ERROR: Failed to apply knobs to staging database: {e}"


def restart_database_staging(tool_context: ToolContext) -> str:
    """Restart the staging database to apply static knobs and verify they persist.

    Args:
        tool_context: ADK tool execution context.

    Returns:
        Status message indicating success or failure of restart.
    """
    cfg = tool_context.state.get("staging_db_config")
    if not cfg:
        return "ERROR: Staging DBConfig not found in state"

    container_name = tool_context.state.get("staging_docker_container")
    if not container_name:
        return "ERROR: No active staging Docker container to restart."

    try:
        ok, msg = restart_docker_db(container_name, db_type=cfg.db_type)
        if ok:
            try:
                internal_port = 3306 if "my" in cfg.db_type.lower() else 5432
                new_port = get_container_host_port(container_name, internal_port)
                cfg.port = new_port
                tool_context.state["staging_db_config"] = cfg
            except Exception:
                pass  # Keep previous port if querying fails (e.g. in unit tests)
            return f"OK: Staging database restarted successfully ({msg})"
        else:
            return f"ERROR: Staging database restart failed: {msg}"
    except Exception as e:
        return f"ERROR: Exception while restarting staging database: {e}"


def recreate_database_staging(tool_context: ToolContext) -> str:
    """Reset and recreate the staging Docker container when candidate knobs fail or for retry attempts.

    Args:
        tool_context: ADK tool execution context.

    Returns:
        Status message indicating success or failure of recreation.
    """
    container_name = tool_context.state.get("staging_docker_container")
    cfg = tool_context.state.get("staging_db_config")
    if not container_name or not cfg:
        return setup_staging_docker(tool_context)

    try:
        intent_output = tool_context.state.get("intent_analyzer_output")
        db_version = None
        if intent_output:
            if hasattr(intent_output, "db_version"):
                db_version = getattr(intent_output, "db_version", None) or None
            elif isinstance(intent_output, dict):
                db_version = intent_output.get("db_version") or None

        if not db_version:
            db_version = (
                tool_context.state.get("db_version")
                or tool_context.state.get("target_db_version")
                or None
            )

        db_type = cfg.db_type
        database = cfg.database

        ok, result, new_cfg = recreate_docker_db(
            container_name=container_name,
            db_type=db_type,
            db_version=db_version,
            database=database,
        )
        if ok:
            tool_context.state["staging_docker_container"] = result
            tool_context.state["staging_db_config"] = new_cfg
            
            # Clear previous failed state
            tool_context.state["staging_applied_knobs"] = []
            tool_context.state["staging_test_results"] = None
            tool_context.state["staging_verified_knobs"] = []
            tool_context.state["staging_tuned_benchmark"] = None
            tool_context.state["staging_validated"] = False
            
            return (
                f"OK: Staging container recreated successfully "
                f"(container: {result}, port: {new_cfg.port})"
            )
        else:
            return f"ERROR: Staging container recreation failed: {result}"
    except Exception as e:
        return f"ERROR: Exception while recreating staging database: {e}"


recreate_database_staging.__test__ = False  # type: ignore[attr-defined]


def test_database_staging(tool_context: ToolContext) -> str:
    """Execute Option A database validation tests on the staging database.

    Runs connectivity check, ping, schema discovery, CRUD lifecycle tests,
    and active knob verification.
    Sets ``tool_context.state['staging_validated'] = (status == 'PASS')``.

    Args:
        tool_context: ADK tool execution context.

    Returns:
        Detailed test report string.
    """
    cfg = tool_context.state.get("staging_db_config")
    if not cfg:
        tool_context.state["staging_validated"] = False
        return "ERROR: Staging DBConfig not found in state"

    try:
        report = test_database(cfg)
        is_pass = (report.get("status") == "ok")
        
        # Knob verification
        selected_knobs = _load_selected_knobs(tool_context)
        report["verified_knobs"] = []
        if selected_knobs:
            verification = verify_active_knobs(cfg, selected_knobs)
            report["verified_knobs"] = verification.get("knobs", [])
            tool_context.state["staging_verified_knobs"] = report["verified_knobs"]
            
            for vk in report["verified_knobs"]:
                if vk.get("status") in ("MISMATCH", "PENDING_RESTART"):
                    is_pass = False
                    if not report.get("error"):
                        report["error"] = "Knob verification mismatch or pending restart"
        else:
            tool_context.state["staging_verified_knobs"] = []
            
        tool_context.state["staging_test_results"] = report
        tool_context.state["staging_validated"] = is_pass

        status_str = "PASS" if is_pass else "FAIL"
        checks = report.get("checks", {})
        details = report.get("details", {})
        err = report.get("error")

        lines = [
            f"Staging Database Option A Test Suite Result: **{status_str}**",
            "",
            "## Check Details",
            f"- **Connectivity**: {'OK' if checks.get('connectivity') else 'FAILED'}",
            f"- **Ping**: {'OK' if checks.get('ping') else 'FAILED'}",
            f"- **Table Scan**: {'OK' if checks.get('table_scan') else 'FAILED'}",
            f"- **CRUD Lifecycle**: {'OK' if checks.get('crud') else 'FAILED'}",
            f"- **Tables Discovered**: {', '.join(details.get('tables_found', [])) or 'None'}",
            f"- **CRUD Result**: {details.get('crud_result', 'not_run')}",
            "",
            "## Knob Verification",
        ]
        if report.get("verified_knobs"):
            for vk in report["verified_knobs"]:
                lines.append(f"- **{vk['knob']}**: Expected `{vk['expected_value']}`, Actual `{vk['actual_value']}` -> {vk['status']}")
        else:
            lines.append("- No knobs verified.")

        if err:
            lines.append(f"\n- **Error Details**: {err}")

        return "\n".join(lines)
    except Exception as e:
        tool_context.state["staging_validated"] = False
        return f"ERROR: Option A test suite threw an exception: {e}"


test_database_staging.__test__ = False  # type: ignore[attr-defined]


def benchmark_baseline_staging(tool_context: ToolContext) -> str:
    """Run sysbench stress benchmark on the baseline (pre-tuning) staging database.

    Checks if a baseline benchmark result is already cached in state; if so,
    it reuses the cached result to avoid corrupting baseline measurements across retries.
    Otherwise, runs sysbench benchmark, stores the result in
    ``tool_context.state['staging_baseline_benchmark']``, and returns a formatted report.

    Args:
        tool_context: ADK tool execution context.

    Returns:
        Formatted markdown report of baseline performance (TPS, QPS, latency) or diagnostic info.
    """
    tables = int(tool_context.state.get("benchmark_tables", 10))
    table_size = int(tool_context.state.get("benchmark_table_size", 10000))
    threads = int(tool_context.state.get("benchmark_threads", 4))
    duration = int(tool_context.state.get("benchmark_duration", 120))

    cached = tool_context.state.get("staging_baseline_benchmark")
    if cached and isinstance(cached, dict):
        if cached.get("status") == "ok":
            tps = float(cached.get("tps", 0.0))
            qps = float(cached.get("qps", 0.0))
            cached_threads = cached.get("threads", threads)
            cached_tables = cached.get("tables", tables)
            cached_duration = cached.get("duration", duration)
            log_file = cached.get("log_file", "")
            details = cached.get("details", {})
            lat_avg = details.get("latency_avg_ms", 0.0)
            lat_p95 = details.get("latency_95th_ms", 0.0)

            lines = [
                "## Baseline Sysbench Benchmark Result (Cached): **OK**",
                "",
                "- **Status**: OK (from cache)",
                f"- **Baseline TPS**: {tps:.2f}",
                f"- **Baseline QPS**: {qps:.2f}",
                f"- **Avg Latency**: {lat_avg:.2f} ms",
                f"- **95th Percentile Latency**: {lat_p95:.2f} ms",
                f"- **Threads**: {cached_threads}",
                f"- **Tables**: {cached_tables}",
                f"- **Duration**: {cached_duration}s",
                f"- **Log File**: `{log_file}`",
            ]
            return "\n".join(lines)
        else:
            err = cached.get("error", "Unknown baseline error")
            return (
                f"WARNING: Baseline sysbench benchmark previously failed (status: ERROR/SKIPPED): {err}. "
                f"Reusing cached status. Candidate knobs have not been applied yet; this indicates an "
                f"environmental or tool issue rather than a candidate knob failure."
            )

    cfg = tool_context.state.get("staging_db_config")
    if not cfg:
        return "ERROR: Staging DBConfig not found in state"

    try:
        result = run_sysbench_benchmark(
            cfg,
            tables=tables,
            table_size=table_size,
            threads=threads,
            duration=duration,
        )
        tool_context.state["staging_baseline_benchmark"] = result

        if result.get("status") != "ok":
            err = result.get("error", "Unknown error running sysbench")
            return (
                f"WARNING: Sysbench baseline benchmark encountered an environment/tool issue (status: ERROR/SKIPPED): {err}. "
                f"Baseline marked as ERROR/SKIPPED without crashing. "
                f"Note: Candidate knobs have not been applied yet, so this is an environmental or sysbench issue, "
                f"not a candidate knob failure."
            )

        tps = float(result.get("tps", 0.0))
        qps = float(result.get("qps", 0.0))
        threads_res = result.get("threads", threads)
        tables_res = result.get("tables", tables)
        duration_res = result.get("duration", duration)
        log_file = result.get("log_file", "")
        details = result.get("details", {})
        lat_avg = details.get("latency_avg_ms", 0.0)
        lat_p95 = details.get("latency_95th_ms", 0.0)

        lines = [
            "## Baseline Sysbench Benchmark Result: **OK**",
            "",
            "- **Status**: OK",
            f"- **Baseline TPS**: {tps:.2f}",
            f"- **Baseline QPS**: {qps:.2f}",
            f"- **Avg Latency**: {lat_avg:.2f} ms",
            f"- **95th Percentile Latency**: {lat_p95:.2f} ms",
            f"- **Threads**: {threads_res}",
            f"- **Tables**: {tables_res}",
            f"- **Duration**: {duration_res}s",
            f"- **Log File**: `{log_file}`",
        ]
        return "\n".join(lines)
    except Exception as e:
        tool_context.state["staging_baseline_benchmark"] = {
            "status": "error",
            "error": str(e),
            "tps": 0.0,
            "qps": 0.0,
            "threads": threads,
            "tables": tables,
            "duration": duration,
            "details": {},
        }
        return (
            f"WARNING: Exception while running baseline benchmark (status: ERROR/SKIPPED): {e}. "
            f"Candidate knobs have not been applied yet; this indicates an environment issue."
        )


benchmark_baseline_staging.__test__ = False  # type: ignore[attr-defined]


def benchmark_tuned_staging(tool_context: ToolContext) -> str:
    """Run sysbench stress benchmark on tuned staging database and evaluate performance.

    Compares tuned TPS and QPS against baseline stored in
    ``tool_context.state['staging_baseline_benchmark']``. Calculates percentage delta.
    If benchmark errors or performance regresses (TPS drop exceeds 5%),
    flags regression, sets ``staging_validated = False``, and records an issue.
    Stores ``tool_context.state['staging_tuned_benchmark'] = result`` and
    ``tool_context.state['staging_benchmark_results'] = BenchmarkResult(...)``.

    Args:
        tool_context: ADK tool execution context.

    Returns:
        Formatted markdown comparison report of baseline vs tuned performance.
    """
    cfg = tool_context.state.get("staging_db_config")
    if not cfg:
        tool_context.state["staging_validated"] = False
        return "ERROR: Staging DBConfig not found in state"

    tables = int(tool_context.state.get("benchmark_tables", 10))
    table_size = int(tool_context.state.get("benchmark_table_size", 10000))
    threads = int(tool_context.state.get("benchmark_threads", 4))
    duration = int(tool_context.state.get("benchmark_duration", 120))

    # Extract baseline metrics
    baseline_res = tool_context.state.get("staging_baseline_benchmark") or {}
    baseline_status = baseline_res.get("status") if isinstance(baseline_res, dict) else None
    baseline_tps = float(baseline_res.get("tps", 0.0)) if isinstance(baseline_res, dict) else 0.0
    baseline_qps = float(baseline_res.get("qps", 0.0)) if isinstance(baseline_res, dict) else 0.0
    baseline_details = baseline_res.get("details", {}) if isinstance(baseline_res, dict) else {}
    baseline_lat_avg = float(baseline_details.get("latency_avg_ms", 0.0))
    baseline_lat_p95 = float(baseline_details.get("latency_95th_ms", 0.0))

    try:
        result = run_sysbench_benchmark(
            cfg,
            tables=tables,
            table_size=table_size,
            threads=threads,
            duration=duration,
        )
        tool_context.state["staging_tuned_benchmark"] = result

        tuned_tps = float(result.get("tps", 0.0))
        tuned_qps = float(result.get("qps", 0.0))
        tuned_details = result.get("details", {}) if isinstance(result, dict) else {}
        tuned_lat_avg = float(tuned_details.get("latency_avg_ms", 0.0))
        tuned_lat_p95 = float(tuned_details.get("latency_95th_ms", 0.0))

        if baseline_tps > 0.0:
            delta_pct = ((tuned_tps - baseline_tps) / baseline_tps) * 100.0
        else:
            delta_pct = 0.0

        is_error = (result.get("status") != "ok")
        is_regression = (
            baseline_status == "ok"
            and baseline_tps > 0.0
            and delta_pct < -_REGRESSION_THRESHOLD_PCT
        )

        if is_error:
            status_str = "ERROR"
            regression_detected = True
            tool_context.state["staging_validated"] = False
        elif is_regression:
            status_str = "REGRESSION"
            regression_detected = True
            tool_context.state["staging_validated"] = False
        else:
            status_str = "PASS"
            regression_detected = False
            if tool_context.state.get("staging_validated", True) is not False:
                tool_context.state["staging_validated"] = True

        baseline_metrics = SysbenchMetrics(
            tps=baseline_tps,
            qps=baseline_qps,
            latency_avg_ms=baseline_lat_avg,
            latency_p95_ms=baseline_lat_p95,
            errors=0 if baseline_status == "ok" else 1,
        )
        tuned_metrics = SysbenchMetrics(
            tps=tuned_tps,
            qps=tuned_qps,
            latency_avg_ms=tuned_lat_avg,
            latency_p95_ms=tuned_lat_p95,
            errors=0 if not is_error else 1,
        )
        benchmark_results = BenchmarkResult(
            status=status_str,
            baseline=baseline_metrics,
            tuned=tuned_metrics,
            performance_delta_pct=round(delta_pct, 2),
            regression_detected=regression_detected,
            baseline_tps=baseline_tps,
            tuned_tps=tuned_tps,
            delta_pct=round(delta_pct, 2),
            qps=tuned_qps,
            details=tuned_details,
        )
        tool_context.state["staging_benchmark_results"] = benchmark_results

        if regression_detected:
            err_msg = result.get("error", "")
            desc = (
                f"Sysbench benchmark error on tuned staging database: {err_msg}"
                if is_error
                else (
                    f"Performance regression detected in staging sysbench benchmark: "
                    f"tuned TPS ({tuned_tps:.2f}) vs baseline TPS ({baseline_tps:.2f}), "
                    f"delta: {delta_pct:+.2f}% (threshold: -{_REGRESSION_THRESHOLD_PCT:.1f}%)"
                )
            )
            issue = KnobCheckIssue(
                knob="tuned_configuration",
                severity="high",
                category="sysbench_error" if is_error else "performance_regression",
                description=desc,
                suggestion="Revert or reduce aggressive memory, worker, or buffer parameters to alleviate contention.",
            )
            issues = tool_context.state.setdefault("staging_issues", [])
            issues.append(issue)

        threads_res = result.get("threads", threads)
        tables_res = result.get("tables", tables)
        duration_res = result.get("duration", duration)
        log_file = result.get("log_file", "")

        lines = [
            f"Staging Tuned Sysbench Benchmark Result: **{status_str}**",
            "",
            "## Performance Comparison",
            f"- **Baseline TPS**: {baseline_tps:.2f}",
            f"- **Tuned TPS**: {tuned_tps:.2f}",
            f"- **Delta**: {delta_pct:+.2f}%",
            f"- **Tuned QPS**: {tuned_qps:.2f} (Baseline QPS: {baseline_qps:.2f})",
            f"- **Avg Latency**: {tuned_lat_avg:.2f} ms (Baseline: {baseline_lat_avg:.2f} ms)",
            f"- **95th Percentile Latency**: {tuned_lat_p95:.2f} ms (Baseline: {baseline_lat_p95:.2f} ms)",
            f"- **Status**: **{status_str}**",
            f"- **Threads**: {threads_res}",
            f"- **Tables**: {tables_res}",
            f"- **Duration**: {duration_res}s",
            f"- **Log File**: `{log_file}`",
        ]

        if is_error:
            lines.append(f"\n- **Error Details**: {result.get('error')}")
        elif is_regression:
            lines.append(
                f"\n> **WARNING**: Performance regression detected! Tuned throughput dropped by {abs(delta_pct):.2f}% compared to baseline."
            )

        return "\n".join(lines)
    except Exception as e:
        tool_context.state["staging_validated"] = False
        return f"ERROR: Exception while running tuned benchmark: {e}"


benchmark_tuned_staging.__test__ = False  # type: ignore[attr-defined]


def setup_staging_docker(tool_context: ToolContext) -> str:
    """Spin up an isolated Docker container for database staging validation.

    Args:
        tool_context: ADK tool execution context.

    Returns:
        Formatted status string with container details or error message.
    """
    ok, msg = is_docker_available()
    if not ok:
        return (
            "ERROR: Docker daemon is not running or available. "
            "Exclusive Docker staging validation requires a running Docker daemon."
        )

    # Clean up any existing staging container from a previous attempt
    existing_container = tool_context.state.get("staging_docker_container")
    if existing_container:
        stop_staging_db(existing_container)
        tool_context.state["staging_docker_container"] = None

    intent_output = tool_context.state.get("intent_analyzer_output")
    db_version = None
    db_type = None

    if intent_output:
        if hasattr(intent_output, "db_version"):
            db_version = getattr(intent_output, "db_version", None) or None
        elif isinstance(intent_output, dict):
            db_version = intent_output.get("db_version") or None

        if hasattr(intent_output, "db_type"):
            db_type = getattr(intent_output, "db_type", None) or None
        elif isinstance(intent_output, dict):
            db_type = intent_output.get("db_type") or None

    if not db_version:
        db_version = (
            tool_context.state.get("db_version")
            or tool_context.state.get("target_db_version")
            or None
        )

    if not db_type:
        db_type = tool_context.state.get("db_type", "postgres")

    database = (
        tool_context.state.get("db_name")
        or tool_context.state.get("database")
        or tool_context.state.get("dbname")
        or "testdb"
    )

    try:
        container_name, cfg = start_staging_db(
            db_type=db_type,
            db_version=db_version,
            database=database,
        )
        tool_context.state["staging_db_config"] = cfg
        tool_context.state["staging_docker_container"] = container_name

        lines = [
            "## Staging Docker Container Initialized: **OK**",
            "",
            f"- **Container Name**: `{container_name}`",
            f"- **Host Port**: `{cfg.port}`",
            f"- **Database**: `{database}`",
            f"- **Database Engine**: `{db_type}`",
        ]
        if db_version:
            lines.append(f"- **Database Version**: `{db_version}`")
        lines.extend([
            "- **Resource Limits**: 2 CPU cores, 2GB RAM",
            "- **Status**: Ready and accepting connections",
        ])
        return "\n".join(lines)
    except Exception as e:
        return f"ERROR: Failed to start staging Docker container: {e}"


setup_staging_docker.__test__ = False  # type: ignore[attr-defined]


def cleanup_staging_docker(tool_context: ToolContext) -> str:
    """Tear down and clean up the active staging Docker container.

    Args:
        tool_context: ADK tool execution context.

    Returns:
        Status message string.
    """
    container_name = tool_context.state.get("staging_docker_container")
    if not container_name:
        return "OK: No active staging Docker container to clean up."

    ok, msg = stop_staging_db(container_name)
    tool_context.state["staging_docker_container"] = None
    tool_context.state["staging_db_config"] = None

    if ok:
        return f"OK: Staging Docker container '{container_name}' cleaned up successfully ({msg})."
    else:
        return f"WARNING: Staging Docker container '{container_name}' cleanup returned: {msg}"


cleanup_staging_docker.__test__ = False  # type: ignore[attr-defined]

