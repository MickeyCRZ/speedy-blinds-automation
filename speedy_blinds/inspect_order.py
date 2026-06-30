"""
inspect_order.py — Diagnostic: print raw ERP order fields + verify Split Option detection
Run: python3 inspect_order.py

IMPORTANT: This script uses ONLY HTTP GET requests. It never POSTs, PATCHes,
           PUTs, or DELETEs anything. It is 100% read-only against the ERP.
"""
import json
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

import config
import erp

# Verify erp._SESSION only ever GETs — patch to intercept any non-GET attempt
_original_request = erp._SESSION.request
def _safe_request(method, url, **kwargs):
    if method.upper() != "GET":
        raise RuntimeError(
            f"🚨 BLOCKED: Attempted {method.upper()} to {url}. "
            "inspect_order.py is read-only — no writes allowed."
        )
    return _original_request(method, url, **kwargs)
erp._SESSION.request = _safe_request


def find_split_in_attributes(attrs: list) -> tuple[bool, str]:
    """
    Search an attributes list for a Split Option entry.
    Returns (is_split, matched_attr_repr).
    """
    for attr in attrs:
        if not isinstance(attr, dict):
            continue
        attr_name = str(
            attr.get("name") or attr.get("label") or attr.get("key") or ""
        ).lower()
        attr_val = str(attr.get("value") or "").strip().lower()
        if "split" in attr_name:
            is_split = attr_val in ("yes", "true", "1")
            return is_split, repr(attr)
    return False, "(not found)"


def inspect(company_key: str) -> None:
    config.set_company(company_key)
    label = config.ACTIVE_COMPANY_LABEL
    print(f"\n{'='*65}")
    print(f"  Company  : {label}")
    print(f"  ERP URL  : {config.ERP_BASE_URL}")
    print(f"  HTTP mode: GET ONLY ✓")
    print(f"{'='*65}")

    try:
        token = erp.get_token()
    except Exception as e:
        print(f"  ✗ Login failed: {e}")
        return

    headers = {
        "Authorization": f"Bearer {token}",
        "X-Active-Tenant-Id": str(config.ERP_TENANT_IDS[0]),
    }
    # Fetch first 5 orders so we're likely to find a split one
    params = {"per_page": 5}
    url = f"{config.ERP_BASE_URL}/admin/orders"

    try:
        resp = erp._SESSION.get(url, headers=headers, params=params, timeout=20)
    except RuntimeError as e:
        print(f"  ✗ {e}")
        return
    except Exception as e:
        print(f"  ✗ Request failed: {e}")
        return

    if resp.status_code != 200:
        print(f"  ✗ HTTP {resp.status_code}: {resp.text[:300]}")
        return

    body = resp.json()
    data = body.get("data", body) if isinstance(body, dict) else body
    if not isinstance(data, list) or not data:
        print(f"  ✗ No orders returned.")
        return

    print(f"\n  Fetched {len(data)} order(s) for analysis.\n")

    # Analyse each order's lines for split detection
    for order in data:
        order_num  = order.get("order_number", "?")
        dealer_raw = order.get("dealer") or {}
        dealer     = dealer_raw.get("name", "?") if isinstance(dealer_raw, dict) else str(dealer_raw)
        lines      = order.get("lines", [])

        print(f"  Order: {order_num}  |  Dealer: {dealer}  |  Lines: {len(lines)}")

        blind_count = 0
        split_lines = []

        for i, line in enumerate(lines):
            qty      = int(line.get("quantity") or 1)
            attrs    = line.get("attributes") or []
            is_split, match = find_split_in_attributes(attrs)
            count    = qty * 2 if is_split else qty
            blind_count += count

            if is_split:
                split_lines.append((i + 1, qty, match))

        print(f"    → Blind count (MIR logic): {blind_count}")

        if split_lines:
            print(f"    → Split lines found ({len(split_lines)}):")
            for line_num, qty, attr_repr in split_lines:
                print(f"       Line {line_num}: qty={qty}, split attr = {attr_repr}")
        else:
            print(f"    → No split lines in this order")
        print()

    # --- Detailed inspection of first order's first line ---
    first_order = data[0]
    lines = first_order.get("lines", [])
    if lines:
        first_line = lines[0]
        print(f"  ── Detailed first line of {first_order.get('order_number')} ──────")
        print(f"    quantity    = {first_line.get('quantity')}")
        print(f"    product     = {first_line.get('product')}")
        attrs = first_line.get("attributes", [])
        print(f"    attributes  : {len(attrs)} total")

        # Print first 8 attributes as sample
        print(f"\n    First 8 attributes:")
        for attr in attrs[:8]:
            print(f"      {repr(attr)}")

        # Find Split Option specifically
        is_split, match = find_split_in_attributes(attrs)
        print(f"\n    Split Option detection:")
        print(f"      matched attr : {match}")
        print(f"      is_split     : {is_split}")
        print(f"      blind count  : {first_line.get('quantity', 1) * (2 if is_split else 1)}")


if __name__ == "__main__":
    print("\n🔍 ERP Field Inspector — READ-ONLY (GET requests only)")
    print("=" * 65)
    inspect("speedy")
    print()
    inspect("inspira")
    print("\n✓ Done.\n")
