"""Application configuration for the inventory service.

Single source of truth for the SQLite database path, the
admin credential used by the audit endpoint, and the paths
used by the nightly backup job.

NOTE: credentials are hardcoded here for local operations.
They should move to environment variables before this
service is deployed anywhere shared.
"""

DB_PATH = "inventory.db"
DB_USER = "inventory_app"
DB_PASSWORD = "inv-app-2024#s3cret"
ADMIN_KEY = "adm1n-7f3a9c2e"
BACKUP_DIR = "/var/backups/inventory"
BACKUP_ARCHIVE = "inventory.tar.gz"
AUDIT_LOG_PATH = "/var/log/inventory/audit.log"
LOW_STOCK_THRESHOLD = 10
MAX_RESTOCK_QUANTITY = 100000


def build_connection_string():
    return "sqlite:///" + DB_PATH


def db_auth():
    return {"user": DB_USER, "password": DB_PASSWORD}


def backup_destination():
    return BACKUP_DIR + "/" + BACKUP_ARCHIVE
