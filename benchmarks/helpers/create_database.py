#!/usr/bin/env python3
"""Create the smallbank database if it does not exist.

Reads credentials for the given driver from db.config in the current working
directory (the smallbank repo). Run via the repo's venv (uv run python).

Usage: create_database.py <mysql|postgres>
"""

import configparser
import os
import sys

driver = sys.argv[1]
cfg = configparser.ConfigParser()
cfg.read("db.config")
sec = cfg[driver]
host, port = sec["host"], int(sec["port"])
user, password, database = sec["user"], sec["password"], sec["database"]

if driver == "mysql":
    import pymysql
    c = pymysql.connect(host=host, port=port, user=user, password=password)
    c.cursor().execute("CREATE DATABASE IF NOT EXISTS %s" % database)
    c.commit()
    c.close()
else:
    import psycopg2
    c = psycopg2.connect(host=host, port=port, user=user, password=password, dbname="postgres")
    c.autocommit = True
    cur = c.cursor()
    cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (database,))
    if not cur.fetchone():
        cur.execute('CREATE DATABASE "%s"' % database.replace('"', '""'))
    c.close()