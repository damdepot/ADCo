# Force Make to use Bash instead of the default /bin/sh
SHELL := /bin/bash

# Define shortcuts/tasks that do not generate output files
.PHONY: rewrite run check test gen-run chain clean clean-all

# ── Generate optimized code ──
rewrite:
	@echo "Generating optimized code..."
	uv run python -m src.rewriter $(DIR) \
		--model=gemini-3.5-flash-lite \
		--verbose

# ── Run TPCC benchmark ──	
tpcc:
	@echo "Running TPCC benchmark..."
	uv run python -m benchmarks.src $(DIR) --type tpcc \
		mysql \
		--config=$(DIR)/configs/mysql.config \
		--clients=1 \
		--warehouses=1 \
		--duration=60 \
		--reset

# ── Run SmallBank benchmark ──
smallbank:
	@echo "Running SmallBank benchmark..."
	uv run python -m benchmarks.src $(DIR) --type smallbank \
		test \
		--accounts 100000 \
		--transactions 30000 \
		--threads 1

# ── Run correctness checker on the sandbox ──
check:
	@echo "Running correctness checker..."
	uv run python -m src.code_checker $(DIR) \
		--model=gemini-3.5-flash-lite

# ── Cleanup ──
clean:
	@echo "Dropping baseline database..."
	@mysql -h 127.0.0.1 -u root -pmysql_root_password -e "DROP DATABASE IF EXISTS \`tpcc-baseline\`" 2>/dev/null || true
	@echo "Dropping baseline database completed."
