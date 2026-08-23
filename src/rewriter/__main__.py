"""ADCo Rewriter package — ADK-based code rewriter for application-database co-optimization.

Usage:
    uv run python -m rewriter <target_dir> [--model <model_name>]
"""

from src.rewriter.main import main

if __name__ == "__main__":
    main()
