"""Pydantic models for knob_checker sub-agent."""

from typing import Any, Literal
from pydantic import BaseModel, Field


class KnobCheckIssue(BaseModel):
    """An issue detected during staging validation of recommended knobs."""

    knob: str = Field(description="Name of the knob causing or associated with the issue")
    severity: Literal["low", "medium", "high", "critical"] = Field(
        description="Severity of the issue (low, medium, high, critical)"
    )
    category: str = Field(
        description="Category: crash, connectivity_failure, memory_overflow, crud_failure, invalid_value, syntax_error, performance_regression"
    )
    description: str = Field(description="Detailed description of what failed during validation")
    suggestion: str = Field(
        default="",
        description="Suggested remediation or adjusted parameter value for the knob recommender",
    )


class SysbenchMetrics(BaseModel):
    """Metrics recorded from a sysbench benchmark run."""

    tps: float = 0.0
    qps: float = 0.0
    latency_avg_ms: float = 0.0
    latency_p95_ms: float = 0.0
    errors: int = 0


class BenchmarkResult(BaseModel):
    """Option B sysbench stress test benchmark result comparing baseline vs tuned performance."""

    status: Literal["PASS", "FAIL", "REGRESSION", "SKIPPED", "ERROR", "NOT_RUN"] = "NOT_RUN"
    baseline: SysbenchMetrics = Field(default_factory=SysbenchMetrics)
    tuned: SysbenchMetrics = Field(default_factory=SysbenchMetrics)
    performance_delta_pct: float = 0.0
    regression_detected: bool = False
    baseline_tps: float = 0.0
    tuned_tps: float = 0.0
    delta_pct: float = 0.0
    qps: float = 0.0
    details: dict[str, Any] = Field(default_factory=dict)


class StagingCheckDetails(BaseModel):
    """Detailed health check flags."""

    connectivity: bool = Field(default=True, description="Whether TCP/DB connection succeeded")
    ping: bool = Field(default=True, description="Whether ping (SELECT 1) query succeeded")
    table_scan: bool = Field(default=True, description="Whether schema table scan succeeded")
    crud: bool = Field(default=True, description="Whether CRUD test lifecycle succeeded")


class VerifiedKnobDetail(BaseModel):
    """Details of a verified active knob."""

    knob: str
    expected_value: str
    actual_value: str = ""
    unit: str = ""
    pending_restart: bool = False
    status: Literal["VERIFIED", "MISMATCH", "PENDING_RESTART", "UNKNOWN", "NOT_FOUND"] = "UNKNOWN"


class StagingTestResults(BaseModel):
    """Option A test report returned from staging validation."""

    status: str = Field(default="ok", description="Overall health check status (ok or error)")
    checks: StagingCheckDetails = Field(
        default_factory=StagingCheckDetails,
        description="Individual check status flags",
    )
    tables_found: list[str] = Field(default_factory=list, description="Tables discovered during scan")
    crud_result: str = Field(default="passed", description="Result of temporary table CRUD lifecycle")
    error: str | None = Field(default=None, description="Error description if any check failed")
    verified_knobs: list[VerifiedKnobDetail] = Field(default_factory=list)


class KnobCheckerOutput(BaseModel):
    """Structured output from the knob checker agent."""

    status: Literal["PASS", "FAIL"] = Field(
        description="PASS if all staging tests and health checks succeed, FAIL if restart, connectivity, or CRUD tests fail"
    )
    issues: list[KnobCheckIssue] = Field(
        default_factory=list,
        description="List of issues and failures encountered during staging validation",
    )
    test_results: StagingTestResults = Field(
        default_factory=StagingTestResults,
        description="Detailed test results from staging health and CRUD validation",
    )
    benchmark_results: BenchmarkResult | None = Field(
        default=None,
        description="Option B sysbench stress test benchmark results comparing baseline vs tuned performance",
    )
    summary: str = Field(
        default="",
        description="Executive summary of staging validation results and readiness for live deployment",
    )
    verified_knobs: list[VerifiedKnobDetail] = Field(default_factory=list)
