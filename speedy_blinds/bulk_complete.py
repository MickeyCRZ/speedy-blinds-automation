"""
bulk_complete.py — Mode 3: Bulk Mark Orders Completed
======================================================
Fetches all orders from the ERP that are NOT yet completed and offers
to mark them completed in bulk, with a clear confirmation step.

Called from main.py when user selects [3].
"""

from __future__ import annotations

import re
import sys
from datetime import datetime, date

from tabulate import tabulate

import erp
import config


# ── ANSI colours ─────────────────────────────────────────────────────────────
def _c(code: str, text: str) -> str:
    if sys.stdout.isatty():
        return f"\033[{code}m{text}\033[0m"
    return text

GREEN  = lambda t: _c("32", t)
YELLOW = lambda t: _c("33", t)
RED    = lambda t: _c("31", t)
BOLD   = lambda t: _c("1",  t)
CYAN   = lambda t: _c("36", t)


def _parse_date(s: str) -> date | None:
    """Accept YYYY-MM-DD or DD-MM-YYYY or DD/MM/YYYY."""
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(s.strip(), fmt).date()
        except ValueError:
            continue
    return None


def _in_range(order: dict, from_dt: date, to_dt: date) -> bool:
    """Check if order's created_at date falls within the range."""
    raw = order.get("created_at") or ""
    # ERP returns ISO format: "2026-06-15T21:33:36.000000Z"
    try:
        d = datetime.strptime(raw[:10], "%Y-%m-%d").date()
        return from_dt <= d <= to_dt
    except ValueError:
        return False


def run_bulk_complete() -> None:
    """
    Interactive Mode 3 flow:
    1. Prompt date range.
    2. Fetch all ERP orders (paginated GET).
    3. Filter to those in range and NOT yet completed.
    4. Show table and prompt confirmation.
    5. PATCH one by one, sequentially.
    6. Print tally.
    """
    print(BOLD("\n═══ Bulk Mark Orders Completed ══════════════════════"))
    print(YELLOW(f"  Company : {config.ACTIVE_COMPANY_LABEL}"))
    print(YELLOW(f"  ERP URL : {config.ERP_BASE_URL}"))
    print()

    # ── Date range input ─────────────────────────────────────────────────────
    print("  Enter the date range for orders to check.")
    print("  Format: YYYY-MM-DD  (e.g. 2026-06-01)\n")

    while True:
        raw_from = input("  From date: ").strip()
        from_dt = _parse_date(raw_from)
        if from_dt:
            break
        print(RED("  Invalid date. Try YYYY-MM-DD."))

    while True:
        raw_to = input("  To date  : ").strip()
        to_dt = _parse_date(raw_to)
        if to_dt:
            break
        print(RED("  Invalid date. Try YYYY-MM-DD."))

    if from_dt > to_dt:
        print(RED("  ✗ 'From' date is after 'To' date. Exiting."))
        return

    # ── Fetch all orders from ERP ────────────────────────────────────────────
    print(YELLOW(f"\n  ⏳ Fetching orders from ERP ({config.ERP_BASE_URL})..."))
    token = erp.get_token()
    all_orders = erp.fetch_all_orders(token)

    if not all_orders:
        print(RED("  ✗ No orders returned from ERP. Check connection."))
        return

    print(GREEN(f"  ✓ Fetched {len(all_orders)} total order(s) from ERP."))

    # ── Filter: in date range and NOT yet completed ──────────────────────────
    in_range = [o for o in all_orders if _in_range(o, from_dt, to_dt)]
    pending  = [o for o in in_range  if str(o.get("status") or "").lower() != "completed"]
    done_cnt = len(in_range) - len(pending)

    print(f"  Orders in range ({raw_from} → {raw_to}): {len(in_range)}")
    print(f"  Already completed (will be skipped)  : {done_cnt}")
    print(f"  Pending (not yet completed)           : {len(pending)}")

    if not pending:
        print(GREEN("\n  ✓ All orders in this range are already completed. Nothing to do."))
        return

    # ── Show table ───────────────────────────────────────────────────────────
    print()
    rows = []
    for o in pending:
        created = (o.get("created_at") or "")[:10]
        rows.append([
            o.get("order_number", "?"),
            o.get("id", "?"),
            o.get("status", "?"),
            o.get("customer_name", "?"),
            o.get("total_price", "?"),
            created,
        ])

    print(tabulate(
        rows,
        headers=["Order #", "ERP ID", "Status", "Customer", "Price", "Created"],
        tablefmt="rounded_outline",
    ))
    print()

    # ── Confirmation ─────────────────────────────────────────────────────────
    print(BOLD("─" * 55))
    print(YELLOW(f"  ⚠️  {len(pending)} order(s) will be marked COMPLETED in the ERP."))
    print(YELLOW( "  This action is IRREVERSIBLE via this script."))
    print(BOLD("─" * 55))
    ans = input(f"\n  Type  yes  to proceed, or press Enter to abort: ").strip().lower()

    if ans != "yes":
        print(YELLOW("  Aborted — no changes made."))
        return

    # ── PATCH one by one ─────────────────────────────────────────────────────
    print()
    ok   = 0
    fail = 0

    for o in pending:
        erp_id   = o.get("id")
        order_num = o.get("order_number", "?")

        if not isinstance(erp_id, int) or erp_id <= 0:
            print(RED(f"  ✗ {order_num}  skipped — missing or invalid ERP id"))
            fail += 1
            continue

        success = erp.mark_order_completed(erp_id, order_num, token)
        if success:
            print(GREEN(f"  ✓ {order_num:12s}  completed  (id={erp_id})"))
            ok += 1
        else:
            print(RED(f"  ✗ {order_num:12s}  FAILED     (id={erp_id}) — check erp_updates.log"))
            fail += 1

    # ── Tally ─────────────────────────────────────────────────────────────────
    print()
    print(BOLD("─── Bulk Complete Summary ──────────────────────────"))
    print(GREEN(f"  ✓ Marked completed : {ok}"))
    if fail:
        print(RED(f"  ✗ Failed           : {fail}"))
    if done_cnt:
        print(YELLOW(f"  ↷ Already done     : {done_cnt} (skipped)"))
    print(BOLD("───────────────────────────────────────────────────"))
    print(YELLOW("  All PATCH attempts logged to erp_updates.log\n"))
