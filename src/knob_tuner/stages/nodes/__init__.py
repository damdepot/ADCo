"""Wave 2b stage function nodes for the knob-tuner pipeline.

Import-safe standalone functions taking ``(ctx, node_input, ...state params)``
and returning Pydantic outputs (or plain dicts where specified). ``ctx`` is
duck-typed (only ``ctx.state`` — a mutable mapping — and optionally
``ctx.route`` are touched), so nodes run with fake contexts in unit tests.
The staged graph in ``workflow.py`` is authoritative; the
validation/compile/screen/decision logic here backs its nodes.

Phase 5.2 layout: implementations live in concern modules
(``_common`` shared leaf helpers, ``accounting``, ``diagnosis``,
``stats_coercion``, ``preflight_decision``); this package root re-exports
the 7 public stage names plus the compatibility surface tests and
``workflow.py`` rely on (``structure_staging_issues``, ``_state``,
``_baseline_cache_key``, ``_extract_winner_plan``).
"""

from __future__ import annotations

from src.knob_tuner.stages.nodes._common import _state as _state
from src.knob_tuner.stages.nodes.accounting import (
    structure_staging_issues as structure_staging_issues,
)
from src.knob_tuner.stages.nodes.preflight_decision import (
    decision,
    prepare_run,
    production_preflight,
)
from src.knob_tuner.stages.nodes.preflight_decision import (
    _extract_winner_plan as _extract_winner_plan,
)
from src.knob_tuner.stages.nodes.preflight_decision import (
    confirm_winner as confirm_winner,
)
from src.knob_tuner.stages.nodes.stats_coercion import (
    _baseline_cache_key as _baseline_cache_key,
)
from src.knob_tuner.stages.nodes.stats_coercion import (
    compile_candidate,
    confirmation_controller,
    materialize_inventory,
    screen_candidate,
)

__all__ = [
    "compile_candidate",
    "confirmation_controller",
    "confirm_winner",
    "decision",
    "materialize_inventory",
    "prepare_run",
    "production_preflight",
    "screen_candidate",
]
