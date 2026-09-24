# Force Make to use Bash instead of the default /bin/sh
SHELL := /bin/bash

# ── Resource contract ──
# CPU_CORES and MEMORY_GB are required for every target that tunes the
# database (run, tune-only, knob-tune). They are forwarded to the pipeline as
# --cpu-cores / --memory and must be supplied explicitly. Example:
#
#   make run CPU_CORES=4 MEMORY_GB=8 DIR=benchmarks/tools/tpcc \
#       SANDBOX_DIR=out/tpcc DB_TYPE=postgres DB_NAME=tpcc
#
# rewrite-only, intent-analyze and check do not require resources.

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
	$(if $(CPU_CORES),,$(error CPU_CORES is required: make run CPU_CORES=4 MEMORY_GB=8 DIR=...))
	$(if $(MEMORY_GB),,$(error MEMORY_GB is required: make run CPU_CORES=4 MEMORY_GB=8 DIR=...))
	@echo "Running unified ADCo pipeline..."
	uv run python -m src.adco $(DIR) \
		--model=gemini-3.5-flash-lite \
		--db-type=$(DB_TYPE) \
		--db-name=$(DB_NAME) \
		--sandbox-dir=$(SANDBOX_DIR) \
		--cpu-cores=$(CPU_CORES) \
		--memory=$(MEMORY_GB) \
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
	$(if $(CPU_CORES),,$(error CPU_CORES is required: make tune-only CPU_CORES=4 MEMORY_GB=8 DIR=...))
	$(if $(MEMORY_GB),,$(error MEMORY_GB is required: make tune-only CPU_CORES=4 MEMORY_GB=8 DIR=...))
	@echo "Running knob tuning only via unified pipeline..."
	uv run python -m src.adco $(DIR) \
		--model=gemini-3.5-flash-lite \
		--mode=tune-only \
		--db-type=$(DB_TYPE) \
		--db-name=$(DB_NAME) \
		--cpu-cores=$(CPU_CORES) \
		--memory=$(MEMORY_GB) \
		--verbose


# ── Knob tuner pipeline ──
knob-tune:
	$(if $(CPU_CORES),,$(error CPU_CORES is required: make knob-tune CPU_CORES=4 MEMORY_GB=8 DIR=...))
	$(if $(MEMORY_GB),,$(error MEMORY_GB is required: make knob-tune CPU_CORES=4 MEMORY_GB=8 DIR=...))
	@echo "Running knob tuner..."
	uv run python -m src.knob_tuner $(DIR) \
		--model=gemini-3.5-flash-lite \
		--db-type=$(DB_TYPE) \
		--db-name=$(DB_NAME) \
		--cpu-cores=$(CPU_CORES) \
		--memory=$(MEMORY_GB) \
		--verbose

# ── Run correctness checker on the sandbox ──
check:
	@echo "Running correctness checker..."
	uv run python -m src.code_checker $(DIR) \
		--model=gemini-3.5-flash-lite
