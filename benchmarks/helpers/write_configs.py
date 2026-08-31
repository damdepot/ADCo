#!/usr/bin/env python3
"""Write smallbank and tpcc db.config files from the ADCo root db.config.

Usage: write_configs.py <project_root> <staging|production>
"""

import configparser
import os
import sys

root, env = sys.argv[1], sys.argv[2]
cfg = configparser.ConfigParser()
cfg.read(os.path.join(root, "db.config"))


def write_config(path, sections):
    out = configparser.ConfigParser()
    for db, database in sections:
        s = cfg[f"{env}.{db}"]
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
    [("mysql", cfg[f"{env}.mysql"]["database"]), ("postgres", cfg[f"{env}.postgres"]["database"])],
)
write_config(
    os.path.join(root, "benchmarks/tools/tpcc/db.config"),
    [("mysql", "tpcc"), ("postgres", "tpcc")],
)