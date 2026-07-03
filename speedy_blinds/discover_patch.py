"""
discover_patch.py — ERP PATCH endpoint discovery
==================================================
Step 1: Fetches a real order and shows ALL its fields (GET only, safe).
Step 2: Asks you which order to test a PATCH on, then sends ONE PATCH
        request and prints the full response.

Run: python3 discover_patch.py
"""
import sys
import os
import json
import re
sys.path.insert(0, os.path.dirname(__file__))

import config
import erp

# ── Choose company ───────────────────────────────────────────────────────────
print("\n  Which company?")
print("  [1] Speedy Blinds")
print("  [2] Inspira Blinds")
choice = input("  Choice: ").strip()
config.set_company("inspira" if choice == "2" else "speedy")
print(f"\n  Company : {config.ACTIVE_COMPANY_LABEL}")
print(f"  ERP URL : {config.ERP_BASE_URL}")

token = erp.get_token()
headers_base = {
    "Authorization": f"Bearer {token}",
    "X-Active-Tenant-Id": str(config.ERP_TENANT_IDS[0]),
}

# ── Step 1: Fetch one order and print ALL fields (GET — safe) ────────────────
order_num = input("\n  Enter an order number to inspect (e.g. ORD-0378): ").strip()

print(f"\n  ── GET /admin/orders?search={order_num} ──────────────────────")
resp = erp._SESSION.get(
    f"{config.ERP_BASE_URL}/admin/orders",
    headers=headers_base,
    params={"search": order_num, "per_page": 10},
    timeout=20,
)
print(f"  HTTP {resp.status_code}")

body = resp.json()
orders = body.get("data", body) if isinstance(body, dict) else body
target_order = None

def _norm(n):
    digits = re.sub(r"\D", "", str(n or ""))
    return digits.lstrip("0") or "0"

target_digits = _norm(order_num)
for o in (orders if isinstance(orders, list) else []):
    if _norm(o.get("order_number", "")) == target_digits:
        target_order = o
        break

if not target_order:
    print(f"  ✗ Order {order_num} not found in response.")
    sys.exit(1)

print(f"\n  Found: {target_order.get('order_number')}  (internal id = {target_order.get('id')})")
print(f"\n  ALL TOP-LEVEL FIELDS:")
for k, v in sorted(target_order.items()):
    if isinstance(v, (dict, list)):
        print(f"    {k:35s} = [{type(v).__name__}]")
    else:
        print(f"    {k:35s} = {repr(v)}")

erp_id      = target_order.get("id")
cur_status  = target_order.get("status")
cur_pay_st  = target_order.get("payment_status")
print(f"\n  ── Status fields ─────────────────────────────────────")
print(f"    id               = {erp_id}")
print(f"    status           = {repr(cur_status)}")
print(f"    payment_status   = {repr(cur_pay_st)}")

# ── Step 2: Test a PATCH ─────────────────────────────────────────────────────
print(f"\n  ── PATCH test ────────────────────────────────────────")
print(f"  We will try to PATCH order {target_order.get('order_number')} (id={erp_id}).")
print(f"  Current status: {repr(cur_status)}")
print()
print("  What payload would you like to test?")
print("  [1] {\"status\": \"completed\"}")
print("  [2] {\"status\": \"done\"}")
print("  [3] {\"payment_status\": \"paid\"}")
print("  [4] Enter custom JSON payload")
print("  [0] Skip PATCH test (GET only was enough)")
patch_choice = input("\n  Choice: ").strip()

if patch_choice == "0":
    print("\n  Skipping PATCH. All done — review the fields above.")
    sys.exit(0)

payloads = {
    "1": {"status": "completed"},
    "2": {"status": "done"},
    "3": {"payment_status": "paid"},
}

if patch_choice == "4":
    raw = input("  Enter JSON payload: ").strip()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"  ✗ Invalid JSON: {e}")
        sys.exit(1)
elif patch_choice in payloads:
    payload = payloads[patch_choice]
else:
    print("  Invalid choice.")
    sys.exit(1)

print(f"\n  ⚠️  About to PATCH {config.ERP_BASE_URL}/admin/orders/{erp_id}")
print(f"  Payload : {json.dumps(payload)}")
confirm = input("\n  Type  yes  to proceed, anything else to abort: ").strip().lower()
if confirm != "yes":
    print("  Aborted.")
    sys.exit(0)

patch_resp = erp._SESSION.patch(
    f"{config.ERP_BASE_URL}/admin/orders/{erp_id}",
    json=payload,
    headers=headers_base,
    timeout=15,
)
print(f"\n  HTTP Status : {patch_resp.status_code}")
print(f"  Response    :")
try:
    print(json.dumps(patch_resp.json(), indent=4))
except Exception:
    print(patch_resp.text[:1000])

# Re-fetch the order to see if status actually changed
print(f"\n  ── Re-fetching order to verify ───────────────────────")
verify_resp = erp._SESSION.get(
    f"{config.ERP_BASE_URL}/admin/orders",
    headers=headers_base,
    params={"search": order_num, "per_page": 10},
    timeout=20,
)
verify_orders = verify_resp.json().get("data", [])
for o in (verify_orders if isinstance(verify_orders, list) else []):
    if _norm(o.get("order_number", "")) == target_digits:
        print(f"    status           = {repr(o.get('status'))}")
        print(f"    payment_status   = {repr(o.get('payment_status'))}")
        break

print("\n  ✓ Discovery complete. Share the output above.\n")
