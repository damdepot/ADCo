"""ADCo Knob Checker — verify tuner-applied knobs are active on the live database.

Usage:
    uv run python -m src.knob_checker <run_dir> --db-name <db> [OPTIONS]
"""

from src.knob_checker.main import main

if __name__ == "__main__":
    main()
