#!/usr/bin/env python3
"""Write smallbank and tpcc db.config files from a source config.

Usage: write_configs.py <project_root> <env_prefix> [config_file]

env_prefix prefixes section names (e.g. "staging."). Empty for plain
sections like baseline.config. Defaults to <project_root>/db.config.
"""

import configparser
import os
import sys

root = sys.argv[1]
prefix = f"{sys.argv[2]}." if sys.argv[2] else ""
config = sys.argv[3] if len(sys.argv) > 3 else os.path.join(root, "db.config")
cfg = configparser.ConfigParser()
cfg.read(config)


def write_config(path, sections):
    out = configparser.ConfigParser()
    for db, database in sections:
        s = cfg[f"{prefix}{db}"]
        out[db] = {
            "host": s["host"],
            "port": s["port"],
            "user": s["user"],
            "password": s["password"],
            "database": database,
        }
    with open(path, "w") as f:
        out.write(f)
    print(f"Wrote {path}")


write_config(
    os.path.join(root, "benchmarks/tools/smallbank/db.config"),
    [("mysql", cfg[f"{prefix}mysql"]["database"]), ("postgres", cfg[f"{prefix}postgres"]["database"])],
)
write_config(
    os.path.join(root, "benchmarks/tools/tpcc/db.config"),
    [("mysql", "tpcc"), ("postgres", "tpcc")],
)