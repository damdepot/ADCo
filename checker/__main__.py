"""ADCo Checker — safety check for sandbox-optimized code before production.

Usage:
    uv run python -m checker <sandbox_dir> [--model <model_name>]
"""

from checker.main import main

if __name__ == "__main__":
    main()
