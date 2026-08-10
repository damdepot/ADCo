"""
SQLite-backed telemetry for ADCo rewriter and checker runs.

Rewriter: rewriter_runs (summary) + rewriter_steps (per-step details).
Checker: checker_runs (one row per run).

The ``sandbox_id`` links rewriter and checker runs together.

Usage:
    from telemetry import RewriterRun

    # Rewriter
    with RewriterRun(model_name="gemini-2.5-flash", target="/path/to/code") as run:
        run.record_step("scanner", duration_ms=120)
        run.record_step("code_optimizer", duration_ms=5000, input_tokens=500, output_tokens=2000)
        run.set_sandbox_id("abc123")

    # Checker
    with RewriterRun(run_type="checker", model_name="gemini-2.5-flash",
                     sandbox_id="abc123") as run:
        run.record_check(status="PASS", reason="", failure_category="")
"""

from __future__ import annotations

import datetime
import os
import sqlite3
import time
from typing import Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(ROOT, "telemetry", "telemetry.db")


def _open_db() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    conn = _open_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS rewriter_runs (
            id              TEXT PRIMARY KEY,
            sandbox_id      TEXT,
            timestamp       TEXT NOT NULL,
            target          TEXT,
            model           TEXT,
            run_status      TEXT CHECK(run_status IN ('running', 'success', 'fail')),
            total_duration_ms INTEGER DEFAULT 0,
            total_input_tokens  INTEGER DEFAULT 0,
            total_output_tokens INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS rewriter_steps (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id          TEXT NOT NULL REFERENCES rewriter_runs(id),
            timestamp       TEXT NOT NULL,
            step            TEXT NOT NULL,
            step_duration_ms INTEGER,
            llm_input_tokens  INTEGER DEFAULT 0,
            llm_output_tokens INTEGER DEFAULT 0,
            output_json     TEXT
        );

        CREATE TABLE IF NOT EXISTS checker_runs (
            id              TEXT PRIMARY KEY,
            sandbox_id      TEXT,
            timestamp       TEXT NOT NULL,
            model           TEXT,
            run_status      TEXT CHECK(run_status IN ('running', 'success', 'fail')),
            checker_status  TEXT DEFAULT '',
            summary         TEXT DEFAULT '',
            total_duration_ms INTEGER DEFAULT 0,
            llm_input_tokens  INTEGER DEFAULT 0,
            llm_output_tokens INTEGER DEFAULT 0,
            output_json     TEXT
        );

        CREATE TABLE IF NOT EXISTS tpcc_runs (
            id              TEXT PRIMARY KEY,
            sandbox_id      TEXT,
            timestamp       TEXT NOT NULL,
            driver          TEXT,
            duration_ms     INTEGER DEFAULT 0,
            run_status      TEXT CHECK(run_status IN ('running', 'success', 'fail')),
            exit_code       INTEGER,
            total_executed  INTEGER DEFAULT 0,
            total_time_us   REAL DEFAULT 0,
            total_tps       REAL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS smallbank_runs (
            id              TEXT PRIMARY KEY,
            sandbox_id      TEXT,
            timestamp       TEXT NOT NULL,
            driver          TEXT,
            duration_ms     INTEGER DEFAULT 0,
            run_status      TEXT CHECK(run_status IN ('running', 'success', 'fail')),
            exit_code       INTEGER,
            total_executed  INTEGER DEFAULT 0,
            total_time_us   REAL DEFAULT 0,
            total_tps       REAL DEFAULT 0
        );
    """)
    # Migration: add columns if missing (for DBs created before additions)
    for table, col, col_type in [
        ("rewriter_steps", "output_json", "TEXT"),
        ("checker_runs", "output_json", "TEXT"),
        ("checker_runs", "checker_status", "TEXT DEFAULT ''"),
        ("checker_runs", "summary", "TEXT DEFAULT ''"),
        ("checker_runs", "sandbox_id", "TEXT"),
        ("tpcc_runs", "sandbox_id", "TEXT"),
    ]:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}")
        except sqlite3.OperationalError:
            pass
    conn.commit()
    conn.close()


class RewriterRun:
    """Records rewriter steps or checker results into SQLite."""

    def __init__(
        self,
        run_type: str = "rewriter",
        model_name: str = "",
        sandbox_id: str = "",
        target: str = "",
        run_id: str = "",
    ) -> None:
        self.run_id = run_id or _new_id()
        self.run_type = run_type
        self.model_name = model_name
        self.sandbox_id = sandbox_id
        self.target = target
        self.started_at = time.time()
        self._conn: Optional[sqlite3.Connection] = None
        self._run_created: bool = False

    def set_sandbox_id(self, sandbox_id: str) -> None:
        self.sandbox_id = sandbox_id

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = _open_db()
        return self._conn

    def _now_iso(self) -> str:
        return datetime.datetime.now(datetime.timezone.utc).isoformat()

    def _ensure_run(self) -> None:
        if not self._run_created:
            conn = self._get_conn()
            if self.run_type == "rewriter":
                conn.execute(
                    "INSERT INTO rewriter_runs (id, sandbox_id, timestamp, target, model, run_status) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (self.run_id, self.sandbox_id or None, self._now_iso(),
                     self.target or None, self.model_name, "running"),
                )
            elif self.run_type == "checker":
                conn.execute(
                    "INSERT INTO checker_runs (id, sandbox_id, timestamp, model, run_status) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (self.run_id, self.sandbox_id or None, self._now_iso(),
                     self.model_name, "running"),
                )
            # benchmark: no summary row — data goes to tpcc_runs / smallbank_runs
            conn.commit()
            self._run_created = True

    def __enter__(self) -> "RewriterRun":
        init_db()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:  # type: ignore
        status = "fail" if exc_type is not None else "success"
        total_ms = int((time.time() - self.started_at) * 1000)
        conn = self._get_conn()

        if self.run_type == "rewriter":
            if self._run_created:
                total_input, total_output = _sum_step_tokens(conn, self.run_id)
                conn.execute(
                    "UPDATE rewriter_runs SET run_status = ?, total_duration_ms = ?, "
                    "total_input_tokens = ?, total_output_tokens = ?, sandbox_id = ? "
                    "WHERE id = ?",
                    (status, total_ms, total_input, total_output,
                     self.sandbox_id or None, self.run_id),
                )
            else:
                conn.execute(
                    "INSERT INTO rewriter_runs (id, sandbox_id, timestamp, target, model, "
                    "run_status, total_duration_ms, total_input_tokens, total_output_tokens) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (self.run_id, self.sandbox_id or None, self._now_iso(),
                     self.target or None, self.model_name, status, total_ms, 0, 0),
                )
        elif self.run_type == "checker" and self._run_created:
            conn.execute(
                "UPDATE checker_runs SET run_status = ?, total_duration_ms = ?, "
                "sandbox_id = ? WHERE id = ?",
                (status, total_ms, self.sandbox_id or None, self.run_id),
            )
        # benchmark: no summary row to finalize

        conn.commit()
        conn.close()
        return False

    # ── Rewriter step API ──

    def record_step(
        self,
        step: str,
        duration_ms: int,
        input_tokens: int = 0,
        output_tokens: int = 0,
        usage_metadata=None,
    ) -> int:
        """Record a pipeline step. Returns the step's auto-increment ID."""
        self._ensure_run()
        if usage_metadata is not None:
            it, ot = _extract_tokens(usage_metadata)
            if it:
                input_tokens = it
            if ot:
                output_tokens = ot
        conn = self._get_conn()
        cur = conn.execute(
            "INSERT INTO rewriter_steps (run_id, timestamp, step, step_duration_ms, "
            "llm_input_tokens, llm_output_tokens) VALUES (?, ?, ?, ?, ?, ?)",
            (self.run_id, self._now_iso(), step, duration_ms,
             input_tokens, output_tokens),
        )
        conn.commit()
        return cur.lastrowid

    def update_step_output(self, step: str, output: object) -> None:
        """Update the output_json for the latest step with the given name in this run."""
        import json
        conn = self._get_conn()
        row = conn.execute(
            "SELECT id FROM rewriter_steps "
            "WHERE run_id = ? AND step = ? AND output_json IS NULL "
            "ORDER BY id DESC LIMIT 1",
            (self.run_id, step),
        ).fetchone()
        if not row:
            return
        output_json = json.dumps(output, default=str, ensure_ascii=False) if output is not None else None
        conn.execute(
            "UPDATE rewriter_steps SET output_json = ? WHERE id = ?",
            (output_json, row[0]),
        )
        conn.commit()

    # ── Checker API ──

    def record_check(
        self,
        output: object = None,
        usage_metadata=None,
    ) -> None:
        """Record the checker's structured output as JSON and extract summary columns."""
        self._ensure_run()
        input_tokens, output_tokens = _extract_tokens(usage_metadata)
        import json
        output_json = json.dumps(output, default=str, ensure_ascii=False) if output is not None else None
        checker_status = ""
        summary = ""
        if output is not None:
            if isinstance(output, dict):
                checker_status = str(output.get("status", ""))
                summary = str(output.get("summary", ""))
            else:
                checker_status = str(getattr(output, "status", ""))
                summary = str(getattr(output, "summary", ""))
        conn = self._get_conn()
        conn.execute(
            "UPDATE checker_runs SET llm_input_tokens = ?, llm_output_tokens = ?, "
            "checker_status = ?, summary = ?, output_json = ? WHERE id = ?",
            (input_tokens, output_tokens, checker_status, summary, output_json, self.run_id),
        )
        conn.commit()

    # ── Benchmark API ──

    def record_benchmark(
        self,
        benchmark: str,
        driver: str = "",
        duration_ms: int = 0,
        exit_code: int = 0,
        total_executed: int = 0,
        total_time_us: float = 0,
        total_tps: float = 0,
    ) -> None:
        """Record a benchmark run result (tpcc or smallbank)."""
        self._ensure_run()
        run_status = "success" if exit_code == 0 else "fail"
        table = f"{benchmark}_runs"
        conn = self._get_conn()
        conn.execute(
            f"INSERT OR REPLACE INTO {table} "
            "(id, sandbox_id, timestamp, driver, duration_ms, "
            "run_status, exit_code, total_executed, total_time_us, total_tps) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self.run_id, self.sandbox_id or None, self._now_iso(), driver,
                duration_ms, run_status, exit_code,
                total_executed, total_time_us, total_tps,
            ),
        )
        conn.commit()


# ── Helpers ──

def _new_id() -> str:
    import uuid
    return uuid.uuid4().hex[:12]


def _extract_tokens(usage_metadata) -> tuple[int, int]:
    if usage_metadata is None:
        return 0, 0

    input_fields = ("prompt_token_count", "promptTokenCount", "input_tokens")
    output_fields = (
        "response_token_count", "responseTokenCount",
        "candidates_token_count", "candidatesTokenCount",
        "output_tokens",
    )

    input_tokens = 0
    for fld in input_fields:
        val = getattr(usage_metadata, fld, None)
        if val is not None:
            input_tokens = int(val)
            break

    output_tokens = 0
    for fld in output_fields:
        val = getattr(usage_metadata, fld, None)
        if val is not None:
            output_tokens = int(val)
            break

    return input_tokens, output_tokens


def extract_tokens(usage_metadata) -> tuple[int, int]:
    """Extract input/output token counts from ADK usage_metadata."""
    return _extract_tokens(usage_metadata)


def _sum_step_tokens(conn: sqlite3.Connection, run_id: str) -> tuple[int, int]:
    row = conn.execute(
        "SELECT COALESCE(SUM(llm_input_tokens), 0), COALESCE(SUM(llm_output_tokens), 0) "
        "FROM rewriter_steps WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    return row if row else (0, 0)
