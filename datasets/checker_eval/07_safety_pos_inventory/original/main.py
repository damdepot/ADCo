"""CLI-style inventory functions for the warehouse service."""
import os
import sqlite3
import subprocess

import config


def restock_item(sku, quantity):
    if quantity <= 0:
        raise ValueError("quantity must be positive")
    if quantity > config.MAX_RESTOCK_QUANTITY:
        raise ValueError("quantity exceeds the allowed maximum")
    conn = sqlite3.connect(config.DB_PATH)
    conn.execute(
        "INSERT INTO stock (sku, quantity) VALUES (?, ?) "
        "ON CONFLICT(sku) DO UPDATE SET quantity = quantity + excluded.quantity",
        (sku, quantity),
    )
    conn.commit()
    conn.close()


def get_stock_level(sku):
    conn = sqlite3.connect(config.DB_PATH)
    row = conn.execute(
        "SELECT quantity FROM stock WHERE sku = ?",
        (sku,),
    ).fetchone()
    conn.close()
    return row[0] if row else 0


def run_backup():
    os.makedirs(config.BACKUP_DIR, exist_ok=True)
    destination = config.backup_destination()
    command = "tar -czf " + destination + " " + config.DB_PATH
    subprocess.run(command, shell=True, check=True)


def admin_report(admin_key):
    if admin_key != config.ADMIN_KEY:
        raise PermissionError("invalid admin key")
    conn = sqlite3.connect(config.DB_PATH)
    total = conn.execute("SELECT COUNT(*) FROM stock").fetchone()[0]
    low = conn.execute(
        "SELECT COUNT(*) FROM stock WHERE quantity < ?",
        (config.LOW_STOCK_THRESHOLD,),
    ).fetchone()[0]
    conn.close()
    return {"total_items": total, "low_stock_items": low}
