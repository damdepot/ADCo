# Force Make to use Bash instead of the default /bin/sh
SHELL := /bin/bash

# Define shortcuts/tasks that do not generate output files
.PHONY: knob-tune rewrite check

# ── Knob tuner pipeline ──
knob-tune:
	@echo "Running knob tuner..."
	uv run python -m src.knob_tuner $(DIR) \
		--model=gemini-3.5-flash-lite \
		--db-type=$(DB_TYPE) \
		--cpu-cores=2 \
		--memory=2 \
		--verbose

# ── Generate optimized code ──
rewrite:
	@echo "Generating optimized code..."
	uv run python -m src.rewriter $(DIR) \
		--model=gemini-3.5-flash-lite \
		--verbose

# ── Run correctness checker on the sandbox ──
check:
	@echo "Running correctness checker..."
	uv run python -m src.code_checker $(DIR) \
		--model=gemini-3.5-flash-lite
