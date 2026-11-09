"""
db.py — Local SQLite Database for Payments Queue and Transaction History
========================================================================
Stores pending payments/charges queue and executed transaction history
so changes survive page refreshes and server restarts.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

_ROOT_DIR = Path(__file__).parent.parent.resolve()
DEFAULT_DB_PATH = _ROOT_DIR / "automate.db"
_ACTIVE_DB_PATH = DEFAULT_DB_PATH


def set_db_path(path: Path | str) -> None:
    """Override active database path (useful for testing)."""
    global _ACTIVE_DB_PATH
    _ACTIVE_DB_PATH = Path(path)
    init_db(_ACTIVE_DB_PATH)


def get_db_path() -> Path:
    return _ACTIVE_DB_PATH


def get_connection(db_path: Optional[Path | str] = None) -> sqlite3.Connection:
    target_path = Path(db_path) if db_path else _ACTIVE_DB_PATH
    conn = sqlite3.connect(str(target_path))
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: Optional[Path | str] = None) -> None:
    """Initialize database tables if they do not exist."""
    conn = get_connection(db_path)
    try:
        with conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS payment_queue (
                    id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    entry_type TEXT NOT NULL DEFAULT 'payment',
                    order_number TEXT DEFAULT '',
                    dealer TEXT DEFAULT '',
                    dealer_id INTEGER,
                    company_key TEXT DEFAULT 'speedy',
                    amount REAL NOT NULL DEFAULT 0.0,
                    is_negative INTEGER NOT NULL DEFAULT 0,
                    type TEXT NOT NULL DEFAULT 'EFT',
                    category TEXT DEFAULT '',
                    description TEXT DEFAULT '',
                    sender TEXT DEFAULT '',
                    payment_date TEXT DEFAULT '',
                    target_column_mode TEXT DEFAULT 'primary',
                    sheet_type TEXT DEFAULT 'new',
                    post_to_erp INTEGER NOT NULL DEFAULT 1,
                    push_to_sheets INTEGER NOT NULL DEFAULT 1,
                    status TEXT DEFAULT 'pending',
                    erp_price REAL,
                    sheet_check_json TEXT
                )
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS transaction_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    transaction_id TEXT,
                    executed_at TEXT NOT NULL,
                    entry_type TEXT NOT NULL DEFAULT 'payment',
                    order_number TEXT DEFAULT '',
                    dealer TEXT DEFAULT '',
                    dealer_id INTEGER,
                    company_key TEXT DEFAULT 'speedy',
                    amount REAL NOT NULL DEFAULT 0.0,
                    is_negative INTEGER NOT NULL DEFAULT 0,
                    type TEXT NOT NULL DEFAULT 'EFT',
                    category TEXT DEFAULT '',
                    description TEXT DEFAULT '',
                    sender TEXT DEFAULT '',
                    payment_date TEXT DEFAULT '',
                    post_to_erp INTEGER NOT NULL DEFAULT 1,
                    push_to_sheets INTEGER NOT NULL DEFAULT 1,
                    erp_line_id TEXT,
                    erp_status TEXT,
                    sheet_status TEXT,
                    sheet_cell TEXT,
                    sheet_row INTEGER,
                    errors_json TEXT,
                    full_result_json TEXT
                )
            """)

            conn.execute("CREATE INDEX IF NOT EXISTS idx_hist_executed ON transaction_history(executed_at DESC)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_hist_order ON transaction_history(order_number)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_hist_dealer ON transaction_history(dealer)")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Queue Operations
# ---------------------------------------------------------------------------

def get_queued_payments() -> list[dict[str, Any]]:
    """Fetch all pending payment and charge entries currently in the queue."""
    init_db()
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM payment_queue ORDER BY created_at DESC")
        rows = cur.fetchall()
        results = []
        for r in rows:
            d = dict(r)
            d["is_negative"] = bool(d.get("is_negative"))
            d["post_to_erp"] = bool(d.get("post_to_erp"))
            d["push_to_sheets"] = bool(d.get("push_to_sheets"))
            sc_raw = d.pop("sheet_check_json", None)
            d["sheetCheck"] = json.loads(sc_raw) if sc_raw else None
            results.append(d)
        return results
    finally:
        conn.close()


def save_queued_payment(item: dict[str, Any]) -> dict[str, Any]:
    """Insert or replace a queued payment item."""
    init_db()
    conn = get_connection()
    item_id = str(item.get("id") or ("pay_" + datetime.now().strftime("%Y%m%d%H%M%S%f")))
    created_at = item.get("created_at") or datetime.now().isoformat()
    sc = item.get("sheetCheck")
    sc_json = json.dumps(sc) if sc else None

    try:
        with conn:
            conn.execute("""
                INSERT OR REPLACE INTO payment_queue (
                    id, created_at, entry_type, order_number, dealer, dealer_id,
                    company_key, amount, is_negative, type, category, description,
                    sender, payment_date, target_column_mode, sheet_type,
                    post_to_erp, push_to_sheets, status, erp_price, sheet_check_json
                ) VALUES (
                    ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?,
                    ?, ?, ?, ?, ?
                )
            """, (
                item_id,
                created_at,
                str(item.get("entry_type") or "payment"),
                str(item.get("order_number") or ""),
                str(item.get("dealer") or ""),
                item.get("dealer_id"),
                str(item.get("company_key") or "speedy"),
                float(item.get("amount") or 0.0),
                1 if item.get("is_negative") else 0,
                str(item.get("type") or "EFT"),
                str(item.get("category") or ""),
                str(item.get("description") or ""),
                str(item.get("sender") or ""),
                str(item.get("payment_date") or ""),
                str(item.get("target_column_mode") or "primary"),
                str(item.get("sheet_type") or "new"),
                1 if item.get("post_to_erp", True) else 0,
                1 if item.get("push_to_sheets", True) else 0,
                str(item.get("status") or "pending"),
                item.get("erp_price"),
                sc_json,
            ))
        item["id"] = item_id
        item["created_at"] = created_at
        return item
    finally:
        conn.close()


def save_queued_payments_batch(items: list[dict[str, Any]]) -> int:
    """Save or update multiple items in the queue."""
    count = 0
    for it in items:
        save_queued_payment(it)
        count += 1
    return count


def sync_entire_queue(items: list[dict[str, Any]]) -> int:
    """Replace entire queue with the provided list (clears removed items)."""
    init_db()
    conn = get_connection()
    try:
        with conn:
            conn.execute("DELETE FROM payment_queue")
            for item in items:
                item_id = str(item.get("id") or ("pay_" + datetime.now().strftime("%Y%m%d%H%M%S%f")))
                created_at = item.get("created_at") or datetime.now().isoformat()
                sc = item.get("sheetCheck")
                sc_json = json.dumps(sc) if sc else None
                conn.execute("""
                    INSERT OR REPLACE INTO payment_queue (
                        id, created_at, entry_type, order_number, dealer, dealer_id,
                        company_key, amount, is_negative, type, category, description,
                        sender, payment_date, target_column_mode, sheet_type,
                        post_to_erp, push_to_sheets, status, erp_price, sheet_check_json
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?,
                        ?, ?, ?, ?, ?
                    )
                """, (
                    item_id,
                    created_at,
                    str(item.get("entry_type") or "payment"),
                    str(item.get("order_number") or ""),
                    str(item.get("dealer") or ""),
                    item.get("dealer_id"),
                    str(item.get("company_key") or "speedy"),
                    float(item.get("amount") or 0.0),
                    1 if item.get("is_negative") else 0,
                    str(item.get("type") or "EFT"),
                    str(item.get("category") or ""),
                    str(item.get("description") or ""),
                    str(item.get("sender") or ""),
                    str(item.get("payment_date") or ""),
                    str(item.get("target_column_mode") or "primary"),
                    str(item.get("sheet_type") or "new"),
                    1 if item.get("post_to_erp", True) else 0,
                    1 if item.get("push_to_sheets", True) else 0,
                    str(item.get("status") or "pending"),
                    item.get("erp_price"),
                    sc_json,
                ))
                item["id"] = item_id
                item["created_at"] = created_at
        return len(items)
    finally:
        conn.close()


def delete_queued_payment(item_id: str) -> bool:
    """Delete a single queued payment by ID."""
    init_db()
    conn = get_connection()
    try:
        with conn:
            cur = conn.execute("DELETE FROM payment_queue WHERE id = ?", (item_id,))
            return cur.rowcount > 0
    finally:
        conn.close()


def clear_queued_payments() -> int:
    """Clear all pending payments from the queue."""
    init_db()
    conn = get_connection()
    try:
        with conn:
            cur = conn.execute("DELETE FROM payment_queue")
            return cur.rowcount
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Transaction History Operations
# ---------------------------------------------------------------------------

def record_executed_transaction(item: dict[str, Any], result: dict[str, Any]) -> int:
    """
    Record an executed transaction to SQLite and delete it from the pending queue.
    """
    init_db()
    conn = get_connection()
    executed_at = datetime.now().isoformat()

    # Extract ERP outcomes
    erp_res = result.get("erp_result") or {}
    erp_line_id = str(erp_res.get("data", {}).get("id") or "") if isinstance(erp_res, dict) else ""
    erp_status = "posted" if erp_line_id else ("skipped" if not item.get("post_to_erp") else "failed")

    # Extract Sheet outcomes
    sheet_res = result.get("sheet_result") or {}
    sheet_status = sheet_res.get("status", "skipped" if not item.get("push_to_sheets") else "failed")
    sheet_cell = sheet_res.get("payment_cell", "")
    sheet_row = sheet_res.get("row")

    errors = result.get("errors") or []
    errors_json = json.dumps(errors) if errors else None
    full_result_json = json.dumps(result)

    item_id = str(item.get("id") or "")

    try:
        with conn:
            # 1. Insert into history
            cur = conn.execute("""
                INSERT INTO transaction_history (
                    transaction_id, executed_at, entry_type, order_number, dealer, dealer_id,
                    company_key, amount, is_negative, type, category, description,
                    sender, payment_date, post_to_erp, push_to_sheets,
                    erp_line_id, erp_status, sheet_status, sheet_cell, sheet_row,
                    errors_json, full_result_json
                ) VALUES (
                    ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?,
                    ?, ?, ?, ?, ?,
                    ?, ?
                )
            """, (
                item_id,
                executed_at,
                str(item.get("entry_type") or "payment"),
                str(item.get("order_number") or ""),
                str(item.get("dealer") or ""),
                item.get("dealer_id"),
                str(item.get("company_key") or "speedy"),
                float(item.get("amount") or 0.0),
                1 if item.get("is_negative") else 0,
                str(item.get("type") or "EFT"),
                str(item.get("category") or ""),
                str(item.get("description") or ""),
                str(item.get("sender") or ""),
                str(item.get("payment_date") or ""),
                1 if item.get("post_to_erp", True) else 0,
                1 if item.get("push_to_sheets", True) else 0,
                erp_line_id,
                erp_status,
                sheet_status,
                sheet_cell,
                sheet_row,
                errors_json,
                full_result_json,
            ))
            history_id = cur.lastrowid

            # 2. Remove from payment_queue if it was queued
            if item_id:
                conn.execute("DELETE FROM payment_queue WHERE id = ?", (item_id,))

            return history_id or 0
    finally:
        conn.close()


def get_transaction_history(limit: int = 100, search: Optional[str] = None, entry_type: Optional[str] = None) -> list[dict[str, Any]]:
    """Retrieve executed transaction history records."""
    init_db()
    conn = get_connection()
    try:
        cur = conn.cursor()
        query = "SELECT * FROM transaction_history"
        conditions = []
        params: list[Any] = []

        if entry_type and entry_type.lower() != "all":
            conditions.append("entry_type = ?")
            params.append(entry_type.lower())

        if search and search.strip():
            s = f"%{search.strip().lower()}%"
            conditions.append("(LOWER(order_number) LIKE ? OR LOWER(dealer) LIKE ? OR LOWER(sender) LIKE ?)")
            params.extend([s, s, s])

        if conditions:
            query += " WHERE " + " AND ".join(conditions)

        query += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, int(limit)))

        cur.execute(query, params)
        rows = cur.fetchall()
        results = []
        for r in rows:
            d = dict(r)
            d["is_negative"] = bool(d.get("is_negative"))
            d["post_to_erp"] = bool(d.get("post_to_erp"))
            d["push_to_sheets"] = bool(d.get("push_to_sheets"))
            err_raw = d.pop("errors_json", None)
            d["errors"] = json.loads(err_raw) if err_raw else []
            res_raw = d.pop("full_result_json", None)
            d["result"] = json.loads(res_raw) if res_raw else {}
            # Format time display
            try:
                dt = datetime.fromisoformat(d["executed_at"])
                d["pushed_at"] = dt.strftime("%I:%M:%S %p")
            except Exception:
                d["pushed_at"] = d.get("executed_at", "")
            results.append(d)
        return results
    finally:
        conn.close()


def clear_transaction_history() -> int:
    """Clear all executed transaction history from the database."""
    init_db()
    conn = get_connection()
    try:
        with conn:
            cur = conn.execute("DELETE FROM transaction_history")
            return cur.rowcount
    finally:
        conn.close()

