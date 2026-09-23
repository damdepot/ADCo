# Force Make to use Bash instead of the default /bin/sh
SHELL := /bin/bash

# Define shortcuts/tasks that do not generate output files
.PHONY: knob-tune check run rewrite-only tune-only intent-analyze

# ── Codebase intent analyzer ──
intent-analyze:
	@echo "Running codebase intent analyzer..."
	uv run python -m src.intent_analyzer $(DIR) \
		--model=gemini-3.5-flash-lite \
		--verbose

# ── Unified ADCo pipeline (rewriter + knob tuner) ──
run:
	@echo "Running unified ADCo pipeline..."
	uv run python -m src.adco $(DIR) \
		--model=gemini-3.5-flash-lite \
		--db-type=$(DB_TYPE) \
		--db-name=$(DB_NAME) \
		--cpu-cores=2 \
		--memory=2 \
		--verbose

rewrite-only:
	@echo "Running code rewriter only via unified pipeline..."
	uv run python -m src.adco $(DIR) \
		--model=gemini-3.5-flash-lite \
		--mode=rewrite-only \
		--sandbox-dir=$(SANDBOX_DIR) \
		--db-type=$(DB_TYPE) \
		--db-name=$(DB_NAME) \
		--verbose

tune-only:
	@echo "Running knob tuning only via unified pipeline..."
	uv run python -m src.adco $(DIR) \
		--model=gemini-3.5-flash-lite \
		--mode=tune-only \
		--db-type=$(DB_TYPE) \
		--db-name=$(DB_NAME) \
		--cpu-cores=2 \
		--memory=2 \
		--verbose


# ── Knob tuner pipeline ──
knob-tune:
	@echo "Running knob tuner..."
	uv run python -m src.knob_tuner $(DIR) \
		--model=gemini-3.5-flash-lite \
		--db-type=$(DB_TYPE) \
		--db-name=$(DB_NAME) \
		--cpu-cores=2 \
		--memory=2 \
		--verbose

# ── Run correctness checker on the sandbox ──
check:
	@echo "Running correctness checker..."
	uv run python -m src.code_checker $(DIR) \
		--model=gemini-3.5-flash-lite
