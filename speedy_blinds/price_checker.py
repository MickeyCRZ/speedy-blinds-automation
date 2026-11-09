from __future__ import annotations

import os
import sys
import re
import csv
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
import config
import erp

# 100% read-only enforcement
_original_request = erp._SESSION.request
def _safe_request(method, url, **kwargs):
    if method.upper() != "GET":
        raise RuntimeError(f"🚨 BLOCKED: Attempted {method.upper()} to {url}. price_checker.py is strictly read-only.")
    return _original_request(method, url, **kwargs)
erp._SESSION.request = _safe_request

def to_float(val, default=0.0):
    try:
        return float(val) if val is not None else default
    except (ValueError, TypeError):
        return default

def extract_order_numbers(text: str) -> list[str]:
    """
    Extract order numbers from text. Supports:
    - Comma-separated: '0201, 0202, ORD-0203'
    - Line-by-line lists
    - Space/tab separated
    - WhatsApp / messy text snippets with order numbers
    Deduplicates preserving original order.
    """
    tokens = re.split(r'[,;\n]+', text)
    ord_nums: list[str] = []
    seen: set[str] = set()

    for token in tokens:
        token = token.strip()
        if not token:
            continue
        matches = re.findall(r'(?i)(?:ORD-|RW)?\d{3,}', token)
        if matches:
            for m in matches:
                m_clean = m.strip().upper()
                if m_clean and m_clean not in seen:
                    seen.add(m_clean)
                    ord_nums.append(m_clean)
        else:
            clean_token = re.sub(r'[^a-zA-Z0-9-]', '', token).upper()
            if clean_token and any(c.isdigit() for c in clean_token) and clean_token not in seen:
                seen.add(clean_token)
                ord_nums.append(clean_token)

    return ord_nums

def lookup_order_in_erp(ord_num: str, token: str) -> tuple[dict | None, str]:
    """
    Look up order in ERP across all configured tenants.
    Returns (order_dict, status_str).
    """
    url = f"{config.ERP_BASE_URL}/admin/orders"
    target = ''.join(filter(str.isdigit, ord_num)).lstrip('0')
    if not target:
        target = ord_num.strip()

    tenant_ids = config.ERP_TENANT_IDS or [1]

    for tid in tenant_ids:
        headers = {"Authorization": f"Bearer {token}", "X-Active-Tenant-Id": str(tid)}
        try:
            resp = erp._SESSION.get(url, headers=headers, params={"search": ord_num}, timeout=20)
        except Exception as exc:
            return None, f"Network Error ({exc})"

        if resp.status_code == 401:
            return None, "HTTP 401 (Unauthorized)"
        if resp.status_code != 200:
            continue

        try:
            data = resp.json().get("data", [])
        except Exception:
            continue

        if not isinstance(data, list):
            continue

        for o in data:
            o_num = ''.join(filter(str.isdigit, str(o.get("order_number", "")))).lstrip('0')
            if o_num == target:
                return o, "OK"

    return None, "NOT FOUND"

def parse_order_metrics(order: dict) -> dict:
    """Calculate cost, quoted price, square footage, and rates per sq ft."""
    cost = to_float(order.get('total_price', 0))
    quote = to_float(order.get('total_quoted_price', 0))

    total_sqft = 0.0
    rates = set()

    for line in order.get("lines", []):
        qty = int(to_float(line.get("quantity", 1)))
        sq = to_float(line.get("sqft", 0))
        if sq == 0.0:
            w = to_float(line.get("width", 0))
            h = to_float(line.get("height", 0))
            sq = max((w / 12) * (h / 12), 10.0)

        total_sqft += (sq * qty)

        r = to_float(line.get("rate_per_sqft", 0))
        if r > 0:
            rates.add(r)

    rate_str = ", ".join(f"${r:,.2f}" for r in sorted(rates)) if rates else "N/A"

    return {
        "cost": cost,
        "quote": quote,
        "sqft": total_sqft,
        "rate_str": rate_str
    }

def run_price_checker():
    workspace_dir = Path(__file__).parent.parent
    default_orders_file = workspace_dir / "orders.txt"

    print("\nInitializing ERP connection...")
    token = erp.get_token()

    from tabulate import tabulate

    print("\n" + "=" * 65)
    print("  BATCH PRICE CHECKER (Direct ERP — No Groq Limit)")
    print("=" * 65)

    text = ""

    # Offer to load from orders.txt if present
    if default_orders_file.exists():
        print(f"  [1] Load from 'orders.txt' (found in workspace)")
        print(f"  [2] Enter another file path")
        print(f"  [3] Paste order numbers directly into terminal")
        choice = input("\nSelect option [1/2/3, default 1, or 'q' to quit]: ").strip()
        if choice.lower() in ('q', 'quit'):
            return

        if choice in ("", "1") or choice.lower() == "orders.txt":
            print(f"📂 Loading orders from: {default_orders_file.name}")
            with open(default_orders_file, "r", encoding="utf-8") as f:
                text = f.read()
        elif choice == "2":
            fpath_input = input("Enter file path (e.g. orders.txt or /path/to/file): ").strip()
            fpath = Path(fpath_input)
            if not fpath.is_absolute():
                fpath = workspace_dir / fpath
            if not fpath.exists():
                print(f"❌ File not found: {fpath}")
                return
            print(f"📂 Loading orders from: {fpath.name}")
            with open(fpath, "r", encoding="utf-8") as f:
                text = f.read()
        elif choice == "3" or not choice.isdigit():
            # Check if user entered a file name directly or pasted orders
            check_path = workspace_dir / choice if not Path(choice).is_absolute() else Path(choice)
            if check_path.exists() and check_path.is_file():
                print(f"📂 Loading orders from: {check_path.name}")
                with open(check_path, "r", encoding="utf-8") as f:
                    text = f.read()
            elif len(extract_order_numbers(choice)) > 0:
                text = choice
            else:
                print("\nPaste order numbers (comma-separated, lines, or text).")
                print("Press Enter twice (or type 'done' / 'q' on a new line) when finished:\n")
                lines = []
                while True:
                    try:
                        line = input()
                        line_stripped = line.strip().lower()
                        if line_stripped in ('q', 'quit') and not lines:
                            return
                        if line_stripped in ('q', 'quit', 'done'):
                            break
                        if line == "" and lines and lines[-1] == "":
                            break
                        lines.append(line)
                    except EOFError:
                        break
                text = "\n".join(lines)
    else:
        print("  [1] Load from file (e.g. orders.txt)")
        print("  [2] Paste order numbers directly into terminal")
        choice = input("\nSelect option [1/2, or 'q' to quit]: ").strip()
        if choice.lower() in ('q', 'quit'):
            return

        if choice == "1" or choice.endswith(('.txt', '.csv')):
            fpath_input = choice if choice.endswith(('.txt', '.csv')) else input("Enter file path: ").strip()
            fpath = Path(fpath_input)
            if not fpath.is_absolute():
                fpath = workspace_dir / fpath
            if not fpath.exists():
                print(f"❌ File not found: {fpath}")
                return
            print(f"📂 Loading orders from: {fpath.name}")
            with open(fpath, "r", encoding="utf-8") as f:
                text = f.read()
        else:
            print("\nPaste order numbers (comma-separated, lines, or text).")
            print("Press Enter twice (or type 'done' / 'q' on a new line) when finished:\n")
            lines = []
            while True:
                try:
                    line = input()
                    line_stripped = line.strip().lower()
                    if line_stripped in ('q', 'quit') and not lines:
                        return
                    if line_stripped in ('q', 'quit', 'done'):
                        break
                    if line == "" and lines and lines[-1] == "":
                        break
                    lines.append(line)
                except EOFError:
                    break
            text = "\n".join(lines)
    if not text.strip():
        print("No input provided.")
        return

    ord_nums = extract_order_numbers(text)
    if not ord_nums:
        print("No order numbers detected in the input.")
        return

    total_orders = len(ord_nums)
    batch_size = 10
    total_batches = (total_orders + batch_size - 1) // batch_size

    # Define output files
    workspace_dir = Path(__file__).parent.parent
    output_txt = workspace_dir / "price_check_results.txt"
    output_csv = workspace_dir / "price_check_results.csv"

    company_label = getattr(config, "ACTIVE_COMPANY_LABEL", "Speedy Blinds")
    timestamp_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    print(f"\n📋 Detected {total_orders:,} unique order(s).")
    print(f"📦 Processing in {total_batches:,} batch(es) of {batch_size} orders each.")
    print(f"💾 Saving results sequentially to:")
    print(f"   • Text file : {output_txt}")
    print(f"   • CSV file  : {output_csv}")
    print("=" * 65)

    # Prepare files
    with open(output_txt, "w", encoding="utf-8") as txt_f, open(output_csv, "w", newline="", encoding="utf-8") as csv_f:
        csv_writer = csv.writer(csv_f)
        csv_writer.writerow(["Index", "Order Number", "Cost", "Quote Price", "Total Sq Ft", "Rate / SqFt", "Status"])
        csv_f.flush()

        txt_f.write("=" * 95 + "\n")
        txt_f.write(f"SPEEDY BLINDS — PRICE CHECK REPORT\n")
        txt_f.write(f"Generated  : {timestamp_str}\n")
        txt_f.write(f"Company    : {company_label}\n")
        txt_f.write(f"Orders     : {total_orders:,} total  |  Batch Size: {batch_size}\n")
        txt_f.write("=" * 95 + "\n")
        header_line = f"{'#':<5} {'Order #':<16} {'Cost':>12} {'Quote Price':>14} {'Total Sq Ft':>14} {'Rate / SqFt':>16}   {'Status':<12}\n"
        txt_f.write(header_line)
        txt_f.write("-" * 95 + "\n")
        txt_f.flush()

        grand_total_cost = 0.0
        grand_total_quote = 0.0
        found_count = 0
        not_found_count = 0
        error_count = 0
        global_idx = 0

        for b_idx in range(total_batches):
            batch_num = b_idx + 1
            start_i = b_idx * batch_size
            end_i = min(start_i + batch_size, total_orders)
            batch = ord_nums[start_i:end_i]

            print(f"\n⏳ [Batch {batch_num}/{total_batches}] Checking orders {start_i + 1} to {end_i}...")
            batch_display_rows = []

            for ord_num in batch:
                global_idx += 1
                order, status = lookup_order_in_erp(ord_num, token)

                # Auto-refresh token if 401 Unauthorized occurs
                if "401" in status:
                    print("  🔑 Token expired during lookup. Re-authenticating with ERP...")
                    token = erp._login()
                    order, status = lookup_order_in_erp(ord_num, token)

                if order:
                    found_count += 1
                    m = parse_order_metrics(order)
                    cost = m["cost"]
                    quote = m["quote"]
                    sqft = m["sqft"]
                    rate_str = m["rate_str"]
                    grand_total_cost += cost
                    grand_total_quote += quote
                    display_num = str(order.get("order_number") or ord_num)

                    # Append to TXT
                    txt_line = f"{global_idx:<5} {display_num:<16} {f'${cost:,.2f}':>12} {f'${quote:,.2f}':>14} {f'{sqft:,.2f}':>14} {rate_str:>16}   {'OK':<12}\n"
                    txt_f.write(txt_line)
                    txt_f.flush()

                    # Append to CSV
                    csv_writer.writerow([global_idx, display_num, f"{cost:.2f}", f"{quote:.2f}", f"{sqft:.2f}", rate_str, "OK"])
                    csv_f.flush()

                    batch_display_rows.append([display_num, f"${cost:,.2f}", f"${quote:,.2f}", f"{sqft:,.2f}", rate_str, "✓ OK"])
                else:
                    if status == "NOT FOUND":
                        not_found_count += 1
                        disp_status = "NOT FOUND"
                    else:
                        error_count += 1
                        disp_status = status

                    # Append to TXT
                    txt_line = f"{global_idx:<5} {ord_num:<16} {'—':>12} {'—':>14} {'—':>14} {'—':>16}   {disp_status:<12}\n"
                    txt_f.write(txt_line)
                    txt_f.flush()

                    # Append to CSV
                    csv_writer.writerow([global_idx, ord_num, "", "", "", "", disp_status])
                    csv_f.flush()

                    batch_display_rows.append([ord_num, "—", "—", "—", "—", f"✗ {disp_status}"])

            # Print batch summary table in terminal
            print(tabulate(
                batch_display_rows,
                headers=["Order", "Cost", "Quote", "Total SqFt", "Rate/SqFt", "Status"],
                tablefmt="simple"
            ))
            print(f"✓ Batch {batch_num}/{total_batches} finished ({end_i}/{total_orders} total saved to file).")

            # Pause between batches to protect ERP API from rate limits
            if b_idx < total_batches - 1:
                time.sleep(0.5)

        # Write final grand total and stats to TXT file
        txt_f.write("-" * 95 + "\n")
        txt_f.write(f"{'GRAND TOTAL':<22} {f'${grand_total_cost:,.2f}':>12} {f'${grand_total_quote:,.2f}':>14}\n")
        txt_f.write("=" * 95 + "\n")
        txt_f.write("SUMMARY STATS:\n")
        txt_f.write(f"  • Total Orders Checked : {total_orders:,}\n")
        txt_f.write(f"  • Successfully Found   : {found_count:,}\n")
        txt_f.write(f"  • Not Found in ERP     : {not_found_count:,}\n")
        if error_count > 0:
            txt_f.write(f"  • Errors Encountered   : {error_count:,}\n")
        txt_f.write("=" * 95 + "\n")
        txt_f.flush()

        # Write grand total to CSV
        csv_writer.writerow([])
        csv_writer.writerow(["GRAND TOTAL", "", f"{grand_total_cost:.2f}", f"{grand_total_quote:.2f}", "", "", ""])
        csv_writer.writerow(["Total Orders Checked", total_orders])
        csv_writer.writerow(["Successfully Found", found_count])
        csv_writer.writerow(["Not Found", not_found_count])
        if error_count > 0:
            csv_writer.writerow(["Errors", error_count])
        csv_f.flush()

    print("\n" + "=" * 65)
    print("                     ALL DONE! 🎉")
    print("=" * 65)
    print(f"  • Total Orders Checked : {total_orders:,}")
    print(f"  • Found in ERP         : {found_count:,}")
    print(f"  • Not Found            : {not_found_count:,}")
    if error_count > 0:
        print(f"  • Errors Encountered   : {error_count:,}")
    print(f"  • Grand Total Cost     : ${grand_total_cost:,.2f}")
    print(f"  • Grand Total Quote    : ${grand_total_quote:,.2f}")
    print("-" * 65)
    print(f"📄 Results written sequentially to:")
    print(f"   1. Text Report : {output_txt}")
    print(f"   2. CSV File    : {output_csv}")
    print("=" * 65 + "\n")

if __name__ == '__main__':
    print("\n🔍 Price Checker — READ-ONLY (GET requests only)")
    print("=" * 65)
    config.set_company("speedy")
    run_price_checker()
    print("\n✓ Done.\n")
