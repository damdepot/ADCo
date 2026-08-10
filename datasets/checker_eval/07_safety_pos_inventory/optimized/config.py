# ADCO_OPTIMIZED: 07_safety_pos_inventory
"""Application configuration for the inventory service.

Secrets are loaded from the environment instead of being
checked into source control. All values are validated at
import time so a misconfigured deployment fails fast
instead of surfacing confusing errors later.
"""

import os
import re


def _env(name, default=None):
    value = os.environ.get(name, default)
    if default is None and value is None:
        raise RuntimeError("Missing required environment variable: " + name)
    return value


def _safe_path(value, name):
    """Whitelist only safe filesystem characters; reject shell metacharacters and traversal."""
    if not value or value.startswith("-") or ".." in value:
        raise ValueError(f"Invalid {name}: {value!r}")
    if not re.fullmatch(r"[A-Za-z0-9_./-]+", value):
        raise ValueError(f"Invalid {name}: {value!r}")
    return value


DB_PATH = _safe_path(_env("INVENTORY_DB_PATH", "inventory.db"), "DB_PATH")
ADMIN_KEY = _env("INVENTORY_ADMIN_KEY")
BACKUP_DIR = _safe_path(_env("INVENTORY_BACKUP_DIR", "/var/backups/inventory"), "BACKUP_DIR")
BACKUP_ARCHIVE = "inventory.tar.gz"
AUDIT_LOG_PATH = _safe_path(_env("INVENTORY_AUDIT_LOG", "/var/log/inventory/audit.log"), "AUDIT_LOG_PATH")
LOW_STOCK_THRESHOLD = 10
MAX_RESTOCK_QUANTITY = 100000


def backup_destination():
    return os.path.join(BACKUP_DIR, BACKUP_ARCHIVE)


def env_summary():
    return {
        "db_path": DB_PATH,
        "backup_dir": BACKUP_DIR,
        "audit_log": AUDIT_LOG_PATH,
        "low_stock_threshold": LOW_STOCK_THRESHOLD,
    }
