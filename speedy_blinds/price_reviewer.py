import os
import sys
import json
from decimal import Decimal, ROUND_HALF_UP

sys.path.insert(0, os.path.dirname(__file__))
import config
import erp
import requests

# 100% read-only enforcement
_original_request = erp._SESSION.request
def _safe_request(method, url, **kwargs):
    if method.upper() != "GET":
        raise RuntimeError(f"🚨 BLOCKED: Attempted {method.upper()} to {url}. price_reviewer.py is strictly read-only.")
    return _original_request(method, url, **kwargs)
erp._SESSION.request = _safe_request

def round_price(val):
    return float(Decimal(str(val)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))

def to_float(val, default=0.0):
    try:
        return float(val) if val is not None else default
    except (ValueError, TypeError):
        return default

def verify_and_recalculate():
    print("Initializing ERP connection...")
    token = erp.get_token()
    tenant_id = config.ERP_TENANT_IDS[0]
    headers = {"Authorization": f"Bearer {token}", "X-Active-Tenant-Id": str(tenant_id)}

    # Import tabulate for 'box' formatting
    from tabulate import tabulate

    while True:
        try:
            ord_nums_input = input("\nEnter ORD Numbers (comma or space separated, or 'q' to quit): ").strip()
            if not ord_nums_input:
                continue
            
            ord_nums = [n.strip() for n in ord_nums_input.replace(',', ' ').split() if n.strip()]
            
            if 'q' in [n.lower() for n in ord_nums]:
                break

            valid_orders = []

            for ord_num in ord_nums:
                # Fetch order
                url = f"{config.ERP_BASE_URL}/admin/orders"
                resp = erp._SESSION.get(url, headers=headers, params={"search": ord_num})
                if resp.status_code != 200:
                    print(f"Failed to fetch order {ord_num}: HTTP {resp.status_code}")
                    continue
                
                data = resp.json().get("data", [])
                
                # Match exact order
                target = ''.join(filter(str.isdigit, ord_num)).lstrip('0')
                order = None
                for o in data:
                    o_num = ''.join(filter(str.isdigit, str(o.get("order_number", "")))).lstrip('0')
                    if o_num == target:
                        order = o
                        break
                
                if not order:
                    print(f"Order {ord_num} not found.")
                    continue
                
                valid_orders.append(order)

            if not valid_orders:
                continue

            # Review Box
            print("\n" + "="*50)
            print("                ORDERS REVIEW")
            print("="*50)
            review_rows = []
            for order in valid_orders:
                order_num = order.get('order_number')
                lines = order.get("lines", [])
                
                for i, line in enumerate(lines, 1):
                    product = line.get('product', 'Unknown')
                    fabric = "None"
                    color = "None"
                    for attr in line.get("attributes", []):
                        fk = str(attr.get("field_key", "")).lower()
                        if "fabric" in fk:
                            fabric = attr.get("value", "")
                        elif fk in ("color", "colour"):
                            if color == "None":
                                color = attr.get("value", "")
                            
                    schedule = line.get('schedule', 'Unknown')
                    w = line.get('width')
                    h = line.get('height')
                    qty = line.get('quantity')
                    line_price = line.get('total_price')
                    
                    review_rows.append([
                        order_num if i == 1 else "",
                        f"{w}x{h}",
                        qty,
                        product,
                        fabric,
                        color,
                        schedule,
                        f"${line_price}"
                    ])
                    
            print(tabulate(
                review_rows, 
                headers=["Order", "Dimensions", "Qty", "Product", "Fabric", "Color", "Schedule", "Price"], 
                tablefmt="grid"
            ))

            # Fetch Metadata
            print("\nFetching Schedule A and Pricing Rates from ERP...")
            sch_resp = erp._SESSION.get(f"{config.ERP_BASE_URL}/admin/schedules", headers=headers).json().get("data", [])
            pt_resp = erp._SESSION.get(f"{config.ERP_BASE_URL}/admin/product-types", headers=headers).json().get("data", [])
            pr_resp = erp._SESSION.get(f"{config.ERP_BASE_URL}/admin/pricing-rates", headers=headers, params={"per_page": 1000}).json().get("data", [])

            schedule_a_id = next((s["id"] for s in sch_resp if str(s["name"]).strip().lower() == "schedule a"), None)
            if not schedule_a_id:
                print("Error: 'Schedule A' not found in ERP.")
                continue

            pt_map = {pt["name"].lower(): pt for pt in pt_resp}

            # Calculation Box
            calc_rows = []
            grand_total_original = 0.0
            grand_total_new = 0.0

            for order in valid_orders:
                lines = order.get("lines", [])
                order_original_total = to_float(order.get('total_price', 0))
                new_total_price = 0.0

                for i, line in enumerate(lines, 1):
                    product_name = str(line.get('product')).lower()
                    w = to_float(line.get('width', 0))
                    h = to_float(line.get('height', 0))
                    qty = int(to_float(line.get('quantity', 1)))
                    current_price = to_float(line.get('total_price', 0))
                    current_rate = to_float(line.get('rate_per_sqft', 0))
                    
                    pt_info = pt_map.get(product_name)
                    min_sqft = to_float(pt_info.get("min_sqft", 10.0)) if pt_info else 10.0
                    
                    # Formula Verification
                    calculated_sqft_per_blind = max((w / 12) * (h / 12), min_sqft)
                    total_calculated_sqft = round(calculated_sqft_per_blind * qty, 4)
                    expected_base = round_price(total_calculated_sqft * current_rate)
                    
                    # Schedule A Calculation
                    pt_id = pt_info["id"] if pt_info else None
                    sch_a_rate = next((to_float(pr["rate_per_sqft"]) for pr in pr_resp if pr["product_type_id"] == pt_id and pr["schedule_id"] == schedule_a_id), None)
                    
                    if sch_a_rate is None:
                        sch_a_rate = current_rate
                    
                    new_base = round_price(total_calculated_sqft * sch_a_rate)
                    # Preserve modifiers
                    modifiers = current_price - expected_base
                    new_line_price = round_price(new_base + modifiers)
                    new_total_price += new_line_price
                    
                calc_rows.append([
                    order.get('order_number'),
                    f"${order_original_total}",
                    f"${round_price(new_total_price)}"
                ])
                grand_total_original += order_original_total
                grand_total_new += new_total_price

            calc_rows.append([
                "GRAND TOTAL",
                f"${round_price(grand_total_original)}",
                f"${round_price(grand_total_new)}"
            ])

            print("\n" + "="*50)
            print("            SCHEDULE A PRICE CALCULATION")
            print("="*50)
            print(tabulate(
                calc_rows,
                headers=["Order", "Original Price", "New Price (Schedule A)"],
                tablefmt="fancy_grid"
            ))

        except Exception as e:
            print(f"Error processing: {e}")

if __name__ == '__main__':
    print("\n🔍 Price Reviewer — READ-ONLY (GET requests only)")
    print("=" * 65)
    config.set_company("speedy")
    verify_and_recalculate()
    print("\n✓ Done.\n")
