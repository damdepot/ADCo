"""Knowledge-base planner — combines extracted intent with rewrite strategies."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from google.adk.tools import ToolContext

from src.code_rewriter._common import _maybe_parse, format_intent_lines
from src.code_rewriter.tools.pipeline_analysis import build_contracts_from_intent, format_dependency_slice_map


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
        if line.startswith("---"):
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
    intent_text = format_intent_lines(intent_output)
    if not intent_text.strip():
        return "ERROR: intent_extractor_output has no usable fields"
    
    n_targets = len(intent_output.get("optimization_targets", []) or [])
    max_strats = 7 if n_targets >= 4 else 5
    
    selected, summary = plan(intent_text, max_strategies=max_strats)
    selected_names = [_clean_name(s.name).upper() for s in selected]
    tool_context.state["selected_strategy_names"] = selected_names
    tool_context.state["strategies"] = summary

    # Rebuild contracts so primary_strategy reflects planner selection (was
    # previously stuck on the default COMBINING_QUERIES from main.py).
    target_dir = tool_context.state.get("target") or tool_context.state.get("sandbox", "")
    if target_dir:
        try:
            analyses, contracts = build_contracts_from_intent(
                target_dir,
                intent_output,
                selected_strategy_names=selected_names,
            )
            if contracts:
                tool_context.state["rewrite_contracts"] = [c.model_dump() for c in contracts]
                tool_context.state["pipeline_analysis_markdown"] = format_dependency_slice_map(
                    analyses, contracts, target_dir
                )
        except Exception:
            pass  # keep pre-built contracts if rebuild fails
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
        "APPLICATION_LOGIC_PUSHDOWN": ["logic", "database", "pushdown", "payment", "aggregat", "sequential"],
        "SUBQUERY_REWRITE": ["subquery", "correlated", "scalar", "unnest"],
        "EXISTS_AND_IN_REWRITE": ["exists", "in ("],
        "NOT_IN_TO_ANTI_JOIN": ["not in", "anti join", "not exists"],
        "OR_TO_UNION": ["or ", "union"],
        "UNION_OPTIMIZATION": ["union", "set operation"],
        "CTE_OPTIMIZATION": ["cte", "with "],
        "JOIN_ORDER_OPTIMIZATION": ["join", "order", "plan"],
        "JOIN_TYPE_OPTIMIZATION": ["join", "inner", "outer"],
        "JOIN_ELIMINATION": ["join", "eliminate", "redundant", "count", "distinct"],
        "PRE_AGGREGATION_BEFORE_JOIN": ["aggregate", "before", "join", "count", "distinct"],
        "SARGABILITY_OPTIMIZATION": ["sargable", "index", "function"],
        "SELECT_STAR_ELIMINATION": ["select *", "star"],
        "LIMIT_AND_TOP_N_PUSHDOWN": ["limit", "top", "offset"],
        "REDUNDANT_OPERATION_ELIMINATION": ["redundant", "operation", "distinct"],
        "APPLICATION_SIDE_AGGREGATION_TO_SQL": ["aggregate", "group by", "sum(", "count("],
        "APPLICATION_SIDE_SORTING_TO_SQL": ["sort", "order by"],
        "ROUND_TRIP_REDUCTION": ["round trip", "network", "latency", "sequential", "payment", "deposit"],
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
        "APPLICATION_LOGIC_PUSHDOWN": 3,
        "PRE_AGGREGATION_BEFORE_JOIN": 2,
        "JOIN_ELIMINATION": 2,
    }

    # Use clean name for boost lookup too
    scored.sort(key=lambda x: x[0] + scoring_boost.get(_clean_name(x[1].name).upper(), 0), reverse=True)
    selected = [s for _, s in scored[:max_strategies]]

    if not selected:
        selected = all_strategies[:max_strategies]

    summary = "\n".join(s.detailed() for s in selected)
    return selected, summary
