"""Knowledge-base planner — combines workload intent with knob tuning strategies."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from google.adk.tools import ToolContext


def _maybe_parse(value: object) -> dict:
    """Return *value* as a dict, JSON-parsing strings (stripping markdown fences)."""
    if isinstance(value, str):
        stripped = re.sub(r"^```[a-z]*\n?", "", value.strip(), flags=re.MULTILINE)
        stripped = re.sub(r"```$", "", stripped.strip())
        try:
            return json.loads(stripped.strip())
        except (json.JSONDecodeError, ValueError):
            return {}
    return value if isinstance(value, dict) else {}


@dataclass
class KnobStrategyDef:
    category: str
    name: str
    engine: str
    knobs: str
    definition: str
    objective: str
    formulas: str
    conditions: str
    restart_required: str
    risk_and_guardrails: str

    def detailed(self) -> str:
        parts = [f"### {self.name}"]
        parts.append(f"**Engine**: {self.engine}")
        parts.append(f"**Category**: {self.category}")
        parts.append(f"**Knobs**: {self.knobs}")
        parts.append(f"**Definition**: {self.definition}")
        parts.append(f"**Objective**: {self.objective}")
        parts.append(f"**Formulas / Baseline**: {self.formulas}")
        parts.append(f"**Conditions**: {self.conditions}")
        parts.append(f"**Restart Requirement**: {self.restart_required}")
        parts.append(f"**Risk & Guardrails**: {self.risk_and_guardrails}")
        return "\n".join(parts)


KB_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "knowledge_base", "knob_tuning_methods.md"
)


def _parse_knob_kb(kb_path: str | None = None, target_engine: str | None = None) -> list[KnobStrategyDef]:
    """Parse the knowledge-base markdown into structured KnobStrategyDef objects."""
    path = Path(kb_path or KB_PATH).resolve()
    if not path.exists():
        raise FileNotFoundError(f"Knowledge base not found: {path}")

    text = path.read_text(encoding="utf-8")

    strategies: list[KnobStrategyDef] = []
    current_section = ""
    current_strategy: dict[str, str] = {}

    def _flush() -> None:
        nonlocal current_strategy
        if current_strategy and "name" in current_strategy and current_strategy.get("definition"):
            engine_val = current_strategy.get("engine", "").strip().lower()
            if target_engine and target_engine.lower() not in engine_val:
                return
            strategies.append(KnobStrategyDef(
                category=current_strategy.get("category", current_section),
                name=current_strategy.get("name", ""),
                engine=current_strategy.get("engine", ""),
                knobs=current_strategy.get("knobs", ""),
                definition=current_strategy.get("definition", ""),
                objective=current_strategy.get("objective", ""),
                formulas=current_strategy.get("formulas / baseline", current_strategy.get("formulas", "")),
                conditions=current_strategy.get("conditions", ""),
                restart_required=current_strategy.get("restart requirement", ""),
                risk_and_guardrails=current_strategy.get("risk & guardrails", ""),
            ))

    for line in text.splitlines():
        if line.startswith("# ") and not line.startswith("## "):
            pass  # skip main title

        elif line.startswith("---"):
            _flush()
            current_strategy = {}

        elif line.startswith("### "):
            pass

        elif line.startswith("## "):
            _flush()
            name = line[3:].strip()
            # Check if this is a strategy (numbered) or a section header
            if re.match(r"^\d+\.", name):
                current_strategy = {"name": re.sub(r"^[\d.]+\s+", "", name).strip()}
            else:
                current_section = name
                current_strategy = {}

        elif current_strategy and line.startswith("*   **"):
            match = re.match(r"\*\s+\*\*([^*]+)\*\*:(.*)", line)
            if match:
                key = match.group(1).strip().lower()
                val = match.group(2).strip()
                current_strategy[key] = val
        elif current_strategy and line.strip().startswith("*") and "formulas / baseline" in current_strategy:
            # handle multiline bullet points in formulas
            if "formulas_multiline" not in current_strategy:
                current_strategy["formulas_multiline"] = True
                current_strategy["formulas / baseline"] += "\n" + line.strip()
            else:
                current_strategy["formulas / baseline"] += "\n" + line.strip()
                
    _flush()

    if not strategies:
        raise ValueError(f"Failed to parse any strategies from KB: {path}")

    return strategies


def plan_knob_tuning(db_type: str, workload_text: str = "", memory_gb: float = 1.0, cpu_cores: int = 1, feedback: str = "", max_strategies: int = 8) -> tuple[list[KnobStrategyDef], str]:
    """Produce a list of applicable strategies given workload intent, system specs, and failure feedback.
    
    Returns (selected_strategies, strategy_summary_text).
    """
    engine = "postgresql" if db_type.lower() in ("postgres", "postgresql", "pg") else "mysql"
    all_strategies = _parse_knob_kb(target_engine=engine)
    
    workload_lower = workload_text.lower()
    feedback_lower = feedback.lower()

    keyword_map: dict[str, list[str]] = {
        "PG_SHARED_MEMORY_MANAGEMENT": ["cache", "read", "memory", "buffer", "oltp"],
        "PG_PER_QUERY_EXECUTION_MEMORY": ["sort", "join", "hash", "aggregate", "olap", "batch", "complex", "spill", "temp"],
        "PG_WAL_CHECKPOINTING_AND_DURABILITY": ["write", "wal", "checkpoint", "insert", "update", "load"],
        "PG_QUERY_PLANNER_AND_IO_CONCURRENCY": ["scan", "ssd", "hdd", "index", "plan", "cost"],
        "PG_CONCURRENCY_AND_PARALLEL_WORKERS": ["connection", "parallel", "worker", "concurrent", "pool"],
        "PG_AUTOVACUUM_BACKGROUND_MAINTENANCE": ["vacuum", "bloat", "dead tuple", "update", "delete", "churn"],
        "PG_CHECKER_REMEDIATION_AND_FAILURE_RECOVERY": ["crash", "oom", "fail", "error", "restart", "start"],
        
        "MYSQL_GLOBAL_BUFFER_POOL_MANAGEMENT": ["cache", "read", "memory", "buffer", "pool"],
        "MYSQL_PER_SESSION_BUFFERS_AND_TEMP_TABLES": ["sort", "join", "temp", "heap", "group by", "order by", "olap"],
        "MYSQL_REDO_LOGGING_AND_TRANSACTION_DURABILITY": ["write", "redo", "flush", "commit", "insert", "update"],
        "MYSQL_STORAGE_IO_THREADS_AND_CAPACITY": ["io", "ssd", "nvme", "thread", "flush", "capacity"],
        "MYSQL_CONNECTIONS_AND_THREAD_CACHING": ["connection", "thread", "concurrent", "open"],
        "MYSQL_INNODB_PURGE_AND_BACKGROUND_MAINTENANCE": ["purge", "cleaner", "undo", "update", "delete"],
        "MYSQL_CHECKER_REMEDIATION_AND_FAILURE_RECOVERY": ["crash", "oom", "fail", "error", "restart", "start", "deprecated"]
    }

    scored: list[tuple[int, KnobStrategyDef]] = []
    
    for strat in all_strategies:
        score = 0
        keywords = keyword_map.get(strat.name, [])
        for kw in keywords:
            if kw in workload_lower:
                score += 1
            if kw in feedback_lower:
                score += 3  # High priority if it's mentioned in feedback/errors
                
        # Default strategies if score is 0
        if score == 0:
            if "MEMORY_MANAGEMENT" in strat.name or "BUFFER_POOL" in strat.name or "WAL" in strat.name or "REDO" in strat.name:
                score = 1
                
        if "REMEDIATION" in strat.name and ("crash" in feedback_lower or "oom" in feedback_lower or "error" in feedback_lower or "fail" in feedback_lower):
            score += 10
            
        if score > 0:
            scored.append((score, strat))

    scored.sort(key=lambda x: x[0], reverse=True)
    selected = [s for _, s in scored[:max_strategies]]

    if not selected:
        selected = all_strategies[:max_strategies]

    summary = "\n".join(s.detailed() for s in selected)
    return selected, summary


def get_knob_strategies(tool_context: ToolContext) -> str:
    """Select applicable knob tuning strategies based on workload, specs, and feedback.

    Reads session state (db_type, workload, memory_gb, cpu_cores, knob_checker_output)
    and stores the strategy summary text back to state as `knob_strategies`.
    """
    db_type = tool_context.state.get("db_type", "postgres")
    workload = tool_context.state.get("workload", "")
    memory_gb = float(tool_context.state.get("memory_gb", 1.0))
    cpu_cores = int(tool_context.state.get("cpu_cores", 1))
    feedback = tool_context.state.get("knob_checker_output", "")
    
    _, summary = plan_knob_tuning(
        db_type=db_type,
        workload_text=workload,
        memory_gb=memory_gb,
        cpu_cores=cpu_cores,
        feedback=feedback
    )
    
    tool_context.state["knob_strategies"] = summary
    return summary
