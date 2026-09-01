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

    def detailed(self) -> str:
        parts = [f"### {self.category}/{self.name}"]
        parts.append(f"**Goal**: {self.objective}")
        parts.append(f"**When**: {self.conditions}")
        parts.append(f"**How**: {self.mechanisms}")
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
            ))

    for line in text.splitlines():
        if line.startswith("# ") and not line.startswith("## "):
            pass  # skip main title

        elif line.startswith("---"):
            _flush()
            current_strategy = {}

        elif line.startswith("### "):
            _flush()
            current_strategy = {"category": current_section, "name": line[4:].strip()}

        elif line.startswith("## "):
            _flush()
            name = line[3:].strip()
            # Check if this is a strategy (numbered) or a section header
            if re.match(r"^\d+\.", name):
                current_strategy = {"category": "TOP_LEVEL", "name": name}
                current_section = name
            else:
                current_section = name
                current_strategy = {}

        elif current_strategy:
            stripped = line.strip()
            if stripped.startswith("*   **Definition**"):
                current_strategy["definition"] = stripped.split(":", 1)[1].strip() if ":" in stripped else ""
            elif stripped.startswith("*   **Objective**"):
                current_strategy["objective"] = stripped.split(":", 1)[1].strip() if ":" in stripped else ""
            elif stripped.startswith("*   **Conditions**"):
                current_strategy["conditions"] = stripped.split(":", 1)[1].strip() if ":" in stripped else ""
            elif "*   **Mechanisms**" in stripped or "*   **Mechanism**" in stripped:
                current_strategy["mechanisms"] = stripped.split(":", 1)[1].strip() if ":" in stripped else ""

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
        "PREDICATE_PUSHDOWN": ["filter", "where", "pushdown", "early"],
        "JOIN_ORDER_HINTS": ["join", "order", "hint", "plan", "optimizer"],
        "SEPARATING_QUERIES": ["separate", "split", "deconstruct", "oom", "memory", "complex", "monolithic"],
        "CONCURRENCY": ["parallel", "async", "batch", "concurrent", "thread", "execute many", "executemany"],
        "AGGREGATE_MERGE": ["aggregate", "group by", "sum(", "count(", "avg("],
        "FILTER_MERGE": ["filter", "where", "condition"],
        "PROJECT_MERGE": ["select", "projection", "column"],
        "SORT_REMOVE": ["order by", "sort", "limit"],
        "SEMI_JOIN_JOIN_TRANSPOSE": ["exists", "semi join", "in (select"],
        "SUBQUERY_UNNESTING": ["subquery", "correlated", "scalar", "unnest"],
        "JOIN_CONDITION_PUSH": ["join", "push", "predicate"],
        "FILTER_INTO_JOIN": ["join", "filter"],
        "JOIN_ADD_REDUNDANT_SEMI_JOIN": ["semi join", "exists"],
        "UNION_REMOVE": ["union", "set operation"],
        "WINDOW_REDUCE_EXPRESSIONS": ["window", "over", "partition by"],
    }

    scored: list[tuple[int, StrategyDef]] = []
    for strat in all_strategies:
        clean = _clean_name(strat.name).upper()
        keywords = keyword_map.get(clean, keyword_map.get(strat.name.upper(), []))
        score = sum(2 for kw in keywords if kw in intent_lower)
        if score > 0:
            scored.append((score, strat))

    scoring_boost = {
        "COMBINING_QUERIES": 3,
        "PREDICATE_PUSHDOWN": 2,
        "CONCURRENCY": 2,
    }

    # Use clean name for boost lookup too
    scored.sort(key=lambda x: x[0] + scoring_boost.get(_clean_name(x[1].name).upper(), 0), reverse=True)
    selected = [s for _, s in scored[:max_strategies]]

    if not selected:
        selected = all_strategies[:max_strategies]

    summary = "\n".join(s.detailed() for s in selected)
    return selected, summary
