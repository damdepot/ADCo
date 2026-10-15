"""Read-only tools for the diagnosis_agent — re-exported, zero side effects.

Only ``read_knob_details`` is exposed so the agent can ground knob-name
references in the live inventory. No write/persist/apply tools by design
(Wave 2a purity).
"""

from src.knob_tuner.tools.reading import read_knob_details

__all__ = [
    "read_knob_details",
]
