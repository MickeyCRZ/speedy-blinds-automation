"""
main.py — Speedy Blinds / Inspira Blinds Order Automation
==========================================================
Usage:
    python main.py                          # paste WhatsApp text in terminal
    python main.py --file orders.txt        # read from a .txt file
    python main.py --file orders.txt --no-confirm  # batch push, no prompts
    python main.py --dry-run                # parse + ERP lookup, NO sheet writes
"""

from __future__ import annotations

import argparse
import sys
from datetime import date

from tabulate import tabulate

import erp
import notifier
import parser as order_parser
import sheets
import config
import mir
from notifier import RunReport


# ── ANSI colours (gracefully disabled on Windows) ──────────────────────────
def _c(code: str, text: str) -> str:
    if sys.stdout.isatty():
        return f"\033[{code}m{text}\033[0m"
    return text

GREEN  = lambda t: _c("32", t)
YELLOW = lambda t: _c("33", t)
RED    = lambda t: _c("31", t)
BOLD   = lambda t: _c("1",  t)
CYAN   = lambda t: _c("36", t)


def banner(company_label: str = ""):
    tag = f" — {company_label}" if company_label else ""
    print(BOLD(CYAN(f"""
╬══════════════════════════════════════════════════╪
║   Dealer Order Automation{tag:<26}║
╚══════════════════════════════════════════════════╨
""")))


def choose_mode() -> str:
    """Prompt the user to select a mode. Returns 'orders' or 'mir'."""
    print(BOLD("What would you like to do?"))
    print("  [1] Process WhatsApp Orders")
    print("  [2] MIR Calculate")
    print()
    while True:
        raw = input("Enter number or mode (orders/mir): ").strip().lower()
        if raw in ("1", "orders", "order", "whatsapp"):
            return "orders"
        if raw in ("2", "mir", "mir calculate", "calculate"):
            return "mir"
        print(RED(f"  Unrecognised choice '{raw}'. Please enter 1 or 2."))


def choose_company() -> str:
    """Prompt the user to select a company. Returns the config key ('speedy' or 'inspira')."""
    companies = list(config.COMPANY.keys())   # e.g. ['speedy', 'inspira']

    print(BOLD("Which company's orders are you processing?"))
    for i, key in enumerate(companies, 1):
        label = config.COMPANY[key]["label"]
        print(f"  [{i}] {label}")
    print()

    while True:
        raw = input("Enter number or name (speedy/inspira): ").strip().lower()

        # Accept number
        if raw.isdigit():
            idx = int(raw) - 1
            if 0 <= idx < len(companies):
                return companies[idx]

        # Accept short key directly
        if raw in config.COMPANY:
            return raw

        # Accept partial label match (e.g. "speedy blinds" → "speedy")
        for key, cfg in config.COMPANY.items():
            if raw in cfg["label"].lower() or raw in key:
                return key

        print(RED(f"  Unrecognised choice '{raw}'. Please enter 1, 2, 'speedy', or 'inspira'."))


def read_input(file_path: str | None) -> str:
    if file_path:
        try:
            with open(file_path, encoding="utf-8") as fh:
                text = fh.read()
            print(GREEN(f"✓ Loaded {file_path}"))
            return text
        except FileNotFoundError:
            print(RED(f"✗ File not found: {file_path}"))
            sys.exit(1)

    print(YELLOW("Paste WhatsApp text below. Press Enter twice (blank line) when done:"))
    lines: list[str] = []
    try:
        while True:
            line = input()
            if line == "" and lines and lines[-1] == "":
                break
            lines.append(line)
    except EOFError:
        pass  # support piped input
    return "\n".join(lines)


def display_orders(orders: list[dict]) -> None:
    rows = []
    for i, o in enumerate(orders, 1):
        is_rework = str(o.get("type") or "").lower() == "rework"
        amount = f"${o['amount']:.2f}" if o.get("amount") is not None else "—"
        if is_rework:
            # Rework price comes from WhatsApp text (stored in price = amount)
            price = CYAN(f"${o['price']:.2f} [RW]") if o.get("price") is not None else YELLOW("—")
        else:
            price = f"${o['price']:.2f}" if o.get("price") is not None else RED("NOT FOUND")
        rows.append([
            i,
            o.get("dealer")        or RED("UNKNOWN"),
            o.get("date")          or "—",
            o.get("order_number")  or "—",
            o.get("customer_name") or ("—" if not is_rework else ""),
            o.get("qty")           or ("—" if not is_rework else ""),
            amount,
            price,
        ])
    print()
    print(tabulate(
        rows,
        headers=["#", "Dealer", "Date", "Order #", "Customer", "Qty", "Amount", "Price (ERP/Text)"],
        tablefmt="rounded_outline",
    ))
    print()


def confirm_orders(orders: list[dict]) -> list[dict]:
    """Ask per-order confirmation. Returns the list of approved orders."""
    approved: list[dict] = []
    skip_all = False

    for i, order in enumerate(orders, 1):
        if skip_all:
            break

        print(BOLD(f"Order {i}/{len(orders)}:"), end="  ")
        print(
            f"[{order.get('dealer','?')}]  "
            f"{order.get('order_number','?')}  "
            f"{order.get('customer_name','?')}  "
            f"Qty={order.get('qty','?')}  "
            f"${order.get('amount','?')}"
        )

        while True:
            ans = input("  Push to sheet? [y]es / [n]o / [a]ll / [q]uit: ").strip().lower()
            if ans in ("y", "yes", ""):
                approved.append(order)
                break
            elif ans in ("n", "no"):
                print(YELLOW("  ↷ Skipped"))
                break
            elif ans in ("a", "all"):
                approved.extend(orders[i - 1:])  # include current + rest
                skip_all = True
                break
            elif ans in ("q", "quit"):
                print(YELLOW("Quitting — no further orders pushed."))
                return approved
            else:
                print("  Please enter y, n, a, or q.")

    return approved


def _run_order_mode(args) -> None:
    """Run the existing WhatsApp order processing workflow."""
    # ── Company selection ─────────────────────────────────────────────────────
    company_key = choose_company()
    config.set_company(company_key)
    company_label = config.COMPANY[company_key]["label"]
    print(GREEN(f"\n✓ Company set to: {company_label}"))
    print(GREEN(f"  ERP: {config.ERP_BASE_URL}\n"))
    banner(company_label)  # reprint banner with company name

    # Initialise the run report — collects everything for notifications
    report = RunReport()

    # 1. Read raw input
    raw_text = read_input(args.file)
    if not raw_text.strip():
        print(RED("No text provided. Exiting."))
        sys.exit(1)

    # 2. Parse with Gemini
    print(YELLOW("\n⏳ Sending to Gemini for extraction..."))
    try:
        orders = order_parser.parse_orders(raw_text)
    except Exception as e:
        print(RED(f"\n✗ Gemini parsing failed: {e}"))
        sys.exit(1)

    if not orders:
        print(YELLOW("No orders found in the provided text."))
        sys.exit(0)

    print(GREEN(f"\n✓ Extracted {len(orders)} order(s)."))
    report.orders_extracted = len(orders)

    # 2.5. Enrich with ERP prices
    print(YELLOW("\n⏳ Fetching prices from ERP..."))
    try:
        erp.enrich_orders(orders)
    except RuntimeError as e:
        print(RED(f"\n✗ ERP lookup failed: {e}"))
        print(YELLOW("  Continuing without prices — column G will be blank."))
        report.erp_failed = True
        for order in orders:
            order.setdefault("price", None)

    # Track orders where price wasn't found
    report.prices_not_found = [
        o.get("order_number", "?") for o in orders if o.get("price") is None
    ]

    display_orders(orders)

    # 3. Dry-run bail-out
    if args.dry_run:
        print(BOLD(YELLOW("\n⚠️  DRY-RUN mode — nothing will be written to Google Sheets.")))
        print(YELLOW("   Re-run without --dry-run to actually push the orders.\n"))
        sys.exit(0)

    # 3. Confirm
    if args.no_confirm:
        to_push = orders
        print(YELLOW(f"--no-confirm mode: pushing all {len(to_push)} orders.\n"))
    else:
        to_push = confirm_orders(orders)
        # Track which orders the user declined
        pushed_nums = {o.get("order_number") for o in to_push}
        report.skipped_user = [
            o for o in orders if o.get("order_number") not in pushed_nums
        ]

    report.orders_selected = len(to_push)

    if not to_push:
        print(YELLOW("\nNo orders selected. Done."))
        sys.exit(0)

    # 4. Push to Sheets
    print(YELLOW(f"\n⏳ Writing {len(to_push)} order(s) to Google Sheets..."))
    unmatched: list[dict] = []

    for order in to_push:
        dealer = order.get("dealer", "UNKNOWN")
        on_num = order.get("order_number", "?")
        try:
            result = sheets.write_order(order)
            updated = result.get("updates", {}).get("updatedRange", "?")
            print(GREEN(f"  ✓ {dealer} — {on_num}  →  {updated}"))
            report.written.append({**order, "range": updated})
        except ValueError as e:
            err_str = str(e)
            if "No spreadsheet ID configured" in err_str:
                print(YELLOW(f"  ⚠️  Unknown dealer '{dealer}' — saving to unmatched_orders.json"))
                unmatched.append(order)
                report.unmatched_dealers.append(order)
            else:
                print(RED(f"  ✗ {dealer} — {on_num}  →  {e}"))
                report.errors.append({"dealer": dealer, "order_number": on_num, "error": err_str})
        except RuntimeError as e:
            err_str = str(e)
            print(RED(f"  ✗ {dealer} — {on_num}  →  {e}"))
            report.errors.append({"dealer": dealer, "order_number": on_num, "error": err_str})

    # Save unmatched orders to disk
    if unmatched:
        report.save_unmatched(unmatched)

    # 5. Summary
    print()
    print(BOLD("─── Summary ───────────────────────────────"))
    print(GREEN(f"  ✓ Written          : {len(report.written)}"))
    print(YELLOW(f"  ↷ Skipped by user  : {len(report.skipped_user)}"))
    if report.unmatched_dealers:
        print(YELLOW(f"  ⚠ Unknown dealers  : {len(report.unmatched_dealers)} (saved to unmatched_orders.json)"))
    if report.prices_not_found:
        print(YELLOW(f"  ⚠ Prices missing   : {len(report.prices_not_found)} order(s) — column G blank"))
    if report.errors:
        print(RED(f"  ✗ Errors           : {len(report.errors)}"))
    print(BOLD("───────────────────────────────────────────"))

    # 6. Dispatch notifications
    print(YELLOW("\n📣 Sending notifications..."))
    report.dispatch()


def main():
    parser = argparse.ArgumentParser(
        description="Push WhatsApp order completions to dealer Google Sheets."
    )
    parser.add_argument(
        "--file", "-f",
        metavar="PATH",
        help="Path to a .txt file containing WhatsApp order text",
    )
    parser.add_argument(
        "--no-confirm",
        action="store_true",
        help="Skip per-order confirmation and push everything automatically",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse orders and fetch ERP prices but do NOT write to Google Sheets",
    )
    args = parser.parse_args()

    banner()

    # ── Mode selection ────────────────────────────────────────────────────────
    mode = choose_mode()
    print()

    if mode == "mir":
        # MIR mode: select company first, then run MIR
        company_key = choose_company()
        config.set_company(company_key)
        company_label = config.COMPANY[company_key]["label"]
        print(GREEN(f"\n✓ Company set to: {company_label}"))
        print(GREEN(f"  ERP: {config.ERP_BASE_URL}\n"))
        mir.run_mir(dry_run=args.dry_run)
    else:
        _run_order_mode(args)

if __name__ == "__main__":
    main()
