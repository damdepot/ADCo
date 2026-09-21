"""Knowledge-base planner — combines extracted intent with rewrite strategies."""

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
class StrategyDef:
    category: str
    name: str
    definition: str
    objective: str = ""
    conditions: str = ""
    mechanisms: str = ""
    risks: str = ""
    safety_rules: str = ""

    def detailed(self) -> str:
        header = f"### {self.category}/{self.name}" if self.category and self.category != "TOP_LEVEL" else f"### {self.name}"
        parts = [header]
        if self.definition:
            parts.append(f"**Definition**: {self.definition}")
        if self.objective:
            parts.append(f"**Goal**: {self.objective}")
        if self.conditions:
            parts.append(f"**When**: {self.conditions}")
        if self.mechanisms:
            parts.append(f"**How**:\n{self.mechanisms}" if "\n" in self.mechanisms else f"**How**: {self.mechanisms}")
        if self.risks:
            parts.append(f"**Risks**:\n{self.risks}" if "\n" in self.risks else f"**Risks**: {self.risks}")
        if self.safety_rules:
            parts.append(f"**Safety**:\n{self.safety_rules}" if "\n" in self.safety_rules else f"**Safety**: {self.safety_rules}")
        return "\n".join(parts)


KB_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "knowledge_base", "query_rewrite_methods.md"
)


def _parse_kb(kb_path: str | None = None) -> list[StrategyDef]:
    """Parse the knowledge-base markdown into structured StrategyDef objects."""
    path = Path(kb_path or KB_PATH).resolve()
    if not path.exists():
        raise FileNotFoundError(f"Knowledge base not found: {path}")

    text = path.read_text(encoding="utf-8")

    strategies: list[StrategyDef] = []
    current_section = ""
    current_strategy: dict[str, str] = {}

    def _flush() -> None:
        nonlocal current_strategy
        if current_strategy and "name" in current_strategy and current_strategy.get("definition"):
            strategies.append(StrategyDef(
                category=current_strategy.get("category", current_section),
                name=current_strategy.get("name", ""),
                definition=current_strategy.get("definition", ""),
                objective=current_strategy.get("objective", ""),
                conditions=current_strategy.get("conditions", ""),
                mechanisms=current_strategy.get("mechanisms", ""),
                risks=current_strategy.get("risks", ""),
                safety_rules=current_strategy.get("safety_rules", ""),
            ))

    FIELD_RE = re.compile(r"^[\*\-]\s+\*\*([^*]+)\*\*:\s*(.*)")
    current_field = None
    for line in text.splitlines():
        if line.startswith("# ") and not line.startswith("## "):
            pass
        elif line.startswith("---"):
            _flush()
            current_strategy = {}
            current_field = None
        elif line.startswith("### "):
            _flush()
            current_strategy = {"category": current_section, "name": line[4:].strip()}
            current_field = None
        elif line.startswith("## "):
            _flush()
            name = line[3:].strip()
            if re.match(r"^\d+\.", name):
                current_strategy = {"category": "TOP_LEVEL", "name": name}
                current_section = name
            else:
                current_section = name
                current_strategy = {}
            current_field = None
        elif current_strategy is not None:
            stripped = line.strip()
            match = FIELD_RE.match(line.lstrip())
            if match:
                field_name = match.group(1).strip().lower()
                field_value = match.group(2).strip()
                if field_name == "definition":
                    current_strategy["definition"] = field_value
                    current_field = "definition"
                elif field_name == "objective":
                    current_strategy["objective"] = field_value
                    current_field = "objective"
                elif field_name == "conditions":
                    current_strategy["conditions"] = field_value
                    current_field = "conditions"
                elif field_name == "mechanisms" or field_name == "mechanism":
                    current_strategy["mechanisms"] = field_value
                    current_field = "mechanisms"
                elif field_name == "risks":
                    current_strategy["risks"] = field_value
                    current_field = "risks"
                elif field_name in [
                    "safety requirement", "safety requirements",
                    "safety rule", "safety rules", "safety",
                    "decision factors", "decision factor",
                    "verification principle", "verification principles"
                ]:
                    current_strategy["safety_rules"] = field_value
                    current_field = "safety_rules"
            elif line.strip().startswith("*") or line.strip().startswith("-"):
                if current_field in ["mechanisms", "risks", "safety_rules"]:
                    if current_strategy.get(current_field):
                        current_strategy[current_field] += "\n" + line.strip()
                    else:
                        current_strategy[current_field] = line.strip()
            elif line.strip() == "":
                pass
            else:
                if current_field and current_strategy.get(current_field):
                    pass # Or append to existing field if needed, but not specified.

    _flush()

    if not strategies:
        raise ValueError(f"Failed to parse any strategies from KB: {path}")

    return strategies


def _clean_name(name: str) -> str:
    """Strip number prefixes from KB strategy names: '1. COMBINING_QUERIES' -> 'COMBINING_QUERIES'."""
    return re.sub(r"^[\d.]+\s+", "", name).strip()


def get_optimization_strategies(tool_context: ToolContext) -> str:
    """Select applicable optimization strategies based on the extracted intent.

    Reads the structured intent from ``intent_extractor_output`` in session
    state, builds an intent text from its fields (connection, queries,
    transactions, n_plus_one, concurrency, orm, notes, and the
    optimization_targets file/description entries), then selects applicable
    strategies. Stores the strategy summary text back to state as ``strategies``.
    """
    intent_output = _maybe_parse(tool_context.state.get("intent_extractor_output"))
    if not intent_output:
        return "ERROR: intent_extractor_output not set in state — call intent_extractor first"
    lines = [
        f"CONNECTION: {intent_output.get('connection', '')}",
        f"QUERIES: {intent_output.get('queries', '')}",
        f"TRANSACTIONS: {intent_output.get('transactions', '')}",
        f"N_PLUS_ONE: {intent_output.get('n_plus_one', '')}",
        f"CONCURRENCY: {intent_output.get('concurrency', '')}",
        f"ORM: {intent_output.get('orm', '')}",
    ]
    notes = intent_output.get("notes", "")
    if notes:
        lines.append(f"NOTES: {notes}")
    for t in intent_output.get("optimization_targets", []) or []:
        lines.append(f"- {t.get('file', '')}: {t.get('description', '')}")
    intent_text = "\n".join(lines)
    if not intent_text.strip():
        return "ERROR: intent_extractor_output has no usable fields"
    _, summary = plan(intent_text)
    tool_context.state["strategies"] = summary
    return summary


def plan(intent_text: str, max_strategies: int = 5) -> tuple[list[StrategyDef], str]:
    """Produce a list of applicable strategies given extracted intent.
    
    Returns (selected_strategies, strategy_summary_text).
    Uses keyword matching for a fast, token-free selection.
    """
    all_strategies = _parse_kb()
    intent_lower = intent_text.lower()

    keyword_map: dict[str, list[str]] = {
        "COMBINING_QUERIES": ["combine", "merge", "multiple", "sequential", "n+1", "loop", "for ", "cte", "round-trip"],
        "N_PLUS_ONE_QUERY_ELIMINATION": ["n+1", "loop", "for ", "batch", "eager"],
        "QUERY_BATCHING": ["batch", "in (", "executemany"],
        "REDUNDANT_QUERY_ELIMINATION": ["redundant", "cache", "repeated"],
        "PREDICATE_PUSHDOWN": ["filter", "where", "pushdown", "early"],
        "PROJECTION_PUSHDOWN": ["select", "projection", "column"],
        "APPLICATION_LOGIC_PUSHDOWN": ["logic", "database", "pushdown"],
        "SUBQUERY_REWRITE": ["subquery", "correlated", "scalar", "unnest"],
        "EXISTS_AND_IN_REWRITE": ["exists", "in ("],
        "NOT_IN_TO_ANTI_JOIN": ["not in", "anti join", "not exists"],
        "OR_TO_UNION": ["or ", "union"],
        "UNION_OPTIMIZATION": ["union", "set operation"],
        "CTE_OPTIMIZATION": ["cte", "with "],
        "JOIN_ORDER_OPTIMIZATION": ["join", "order", "plan"],
        "JOIN_TYPE_OPTIMIZATION": ["join", "inner", "outer"],
        "JOIN_ELIMINATION": ["join", "eliminate", "redundant"],
        "PRE_AGGREGATION_BEFORE_JOIN": ["aggregate", "before", "join"],
        "SARGABILITY_OPTIMIZATION": ["sargable", "index", "function"],
        "SELECT_STAR_ELIMINATION": ["select *", "star"],
        "LIMIT_AND_TOP_N_PUSHDOWN": ["limit", "top", "offset"],
        "REDUNDANT_OPERATION_ELIMINATION": ["redundant", "operation", "distinct"],
        "APPLICATION_SIDE_AGGREGATION_TO_SQL": ["aggregate", "group by", "sum(", "count("],
        "APPLICATION_SIDE_SORTING_TO_SQL": ["sort", "order by"],
        "ROUND_TRIP_REDUCTION": ["round trip", "network", "latency"],
        "INDEPENDENT_QUERY_PARALLELISM": ["parallel", "async", "concurrent"],
        "LOOP_TO_SET_OPERATION": ["loop", "set", "union"],
        "RESULT_SET_REDUCTION": ["result", "reduce", "size"],
        "SQL_SEMANTIC_SAFETY": ["safety", "semantic", "sql"],
        "OPTIMIZATION_SELECTION": ["optimization", "selection", "choose"],
        "VERIFICATION": ["verify", "test", "check"],
    }

    scored: list[tuple[int, StrategyDef]] = []
    for strat in all_strategies:
        clean = _clean_name(strat.name).upper()
        keywords = keyword_map.get(clean, keyword_map.get(strat.name.upper(), []))
        score = sum(2 for kw in keywords if kw in intent_lower)
        if score > 0:
            scored.append((score, strat))

    scoring_boost = {
        "N_PLUS_ONE_QUERY_ELIMINATION": 4,
        "QUERY_BATCHING": 3,
        "COMBINING_QUERIES": 3,
        "LOOP_TO_SET_OPERATION": 3,
        "PREDICATE_PUSHDOWN": 2,
        "ROUND_TRIP_REDUCTION": 2,
    }

    # Use clean name for boost lookup too
    scored.sort(key=lambda x: x[0] + scoring_boost.get(_clean_name(x[1].name).upper(), 0), reverse=True)
    selected = [s for _, s in scored[:max_strategies]]

    if not selected:
        selected = all_strategies[:max_strategies]

    summary = "\n".join(s.detailed() for s in selected)
    return selected, summary
