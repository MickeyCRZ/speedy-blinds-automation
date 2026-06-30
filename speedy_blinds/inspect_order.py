"""
inspect_order.py — Diagnostic: print raw ERP order fields
Run: python3 inspect_order.py
Fetches one real order from each ERP and prints key fields.
"""
import json
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

import config
import erp

# Fields we care about for MIR blind-qty detection
FIELDS_OF_INTEREST = {
    "qty", "quantity", "items_count", "total_items",
    "item_count", "blind_count", "blinds", "count",
    "total_qty", "total_quantity", "no_of_blinds",
    "order_number", "total_price", "dealer",
}


def inspect(company_key: str) -> None:
    config.set_company(company_key)
    label = config.ACTIVE_COMPANY_LABEL
    print(f"\n{'='*60}")
    print(f"  Company : {label}")
    print(f"  ERP URL : {config.ERP_BASE_URL}")
    print(f"{'='*60}")

    try:
        token = erp.get_token()
    except Exception as e:
        print(f"  ✗ Login failed: {e}")
        return

    headers = {
        "Authorization": f"Bearer {token}",
        "X-Active-Tenant-Id": str(config.ERP_TENANT_IDS[0]),
    }
    params = {"per_page": 1}
    url = f"{config.ERP_BASE_URL}/admin/orders"

    try:
        resp = erp._SESSION.get(url, headers=headers, params=params, timeout=20)
    except Exception as e:
        print(f"  ✗ Request failed: {e}")
        return

    if resp.status_code != 200:
        print(f"  ✗ HTTP {resp.status_code}: {resp.text[:300]}")
        return

    body = resp.json()
    # Handle both list and {"data": [...]} shapes
    data = body.get("data", body) if isinstance(body, dict) else body
    if not isinstance(data, list) or not data:
        print(f"  ✗ No orders in response. Raw: {str(body)[:300]}")
        return

    order = data[0]
    order_num = order.get("order_number", "?")
    print(f"\n  ✓ Order fetched : {order_num}")
    print(f"  Total keys      : {len(order)}")

    # --- Print ALL fields ---
    print(f"\n  ALL FIELDS:")
    for k, v in sorted(order.items()):
        # Skip long nested objects for readability
        if isinstance(v, (dict, list)) and len(str(v)) > 80:
            v_str = f"[{type(v).__name__}, len={len(v)}]"
        else:
            v_str = repr(v)
        marker = "  ◄◄◄ CANDIDATE" if k in FIELDS_OF_INTEREST else ""
        print(f"    {k:40s} = {v_str}{marker}")

    # --- Print sub-items if present ---
    for items_key in ("items", "order_items", "line_items", "products", "lines"):
        if items_key in order and isinstance(order[items_key], list) and order[items_key]:
            first_line = order[items_key][0]
            print(f"\n  FIRST ITEM in order['{items_key}'] ({len(order[items_key])} total items):")
            print(f"  Fields inside each line ({len(first_line)} keys):")
            for k, v in sorted(first_line.items()):
                v_str = repr(v) if len(repr(v)) <= 100 else f"[{type(v).__name__}, len={len(str(v))}]"
                print(f"    {k:40s} = {v_str}")
            break

    # --- Highlighted summary ---
    print(f"\n  ── KEY CANDIDATES ──────────────────────────────")
    found_any = False
    for field in sorted(FIELDS_OF_INTEREST):
        if field in order:
            print(f"    {field:40s} = {repr(order[field])}")
            found_any = True
    if not found_any:
        print("    (none of the candidate fields were present at top level)")


if __name__ == "__main__":
    inspect("speedy")
    print()
    inspect("inspira")
    print("\n✓ Done.\n")
