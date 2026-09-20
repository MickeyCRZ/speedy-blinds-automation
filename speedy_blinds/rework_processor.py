import os
import sys
import csv
import io
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from datetime import datetime

sys.path.insert(0, os.path.dirname(__file__))
import config
import erp
from config import (
    NOTIFY_EMAIL_APP_PASSWORD,
    NOTIFY_EMAIL_FROM,
    NOTIFY_EMAIL_TO,
)

def parse_range_input(raw_input: str) -> list[int]:
    """Parse comma separated or hyphenated numbers into a list of integers."""
    nums = set()
    parts = raw_input.replace(' ', '').split(',')
    for part in parts:
        if not part:
            continue
        if '-' in part:
            try:
                start, end = part.split('-')
                for i in range(int(start), int(end) + 1):
                    nums.add(i)
            except ValueError:
                print(f"Skipping invalid range: {part}")
        else:
            try:
                nums.add(int(part))
            except ValueError:
                print(f"Skipping invalid number: {part}")
    return sorted(list(nums))

def process_reworks():
    print("\n🔍 Rework Processor")
    print("=" * 50)
    
    raw = input("Enter Rework Range (comma separated numbers e.g. 1, 2, 3 or 1-5): ").strip()
    if not raw:
        print("No input provided. Exiting.")
        return
        
    req_ids = parse_range_input(raw)
    if not req_ids:
        print("No valid numbers parsed. Exiting.")
        return
        
    print(f"\nProcessing {len(req_ids)} rework request(s)...")
    
    token = erp.get_token()
    tenant_id = config.ERP_TENANT_IDS[0]
    headers = {"Authorization": f"Bearer {token}", "X-Active-Tenant-Id": str(tenant_id)}
    
    csv_rows = []
    # Header matching the user's updated requirements
    csv_rows.append(["RW#", "Dealer", "Order #", "Type", "Line", "Room", "Line Notes", "Attributes", "Original Cost", "Rework Cost"])
    
    # Simple cache to avoid refetching the same order multiple times
    orders_cache = {}
    
    for req_id in req_ids:
        url = f"{config.ERP_BASE_URL}/admin/requests/{req_id}"
        resp = erp._SESSION.get(url, headers=headers)
        
        if resp.status_code != 200:
            print(f"  ✗ Rework ID {req_id} not found or error ({resp.status_code})")
            continue
            
        data = resp.json()
        req = data.get("data", data)
        
        if req.get("type_slug") != "rework":
            print(f"  ✗ ID {req_id} is not a rework request (it's {req.get('type_slug')})")
            continue
            
        field_data = req.get("field_data", {})
        rework_type = field_data.get("rework_type", "basic").lower()
        rework_lines = field_data.get("lines", [])
        
        linked = req.get("linked_entities", [])
        order_le = next((le for le in linked if le["entity"] == "order"), None)
        
        dealer_name = req.get("raised_by", {}).get("name", "Unknown")
        order_number = "Unknown"
        
        if not order_le:
            print(f"  ⚠ Rework ID {req_id} is missing a linked order.")
            continue
            
        order_id = order_le["id"]
        order_number = order_le.get("meta", {}).get("order_number", "Unknown")
        
        # Fetch order from cache or API
        if order_id not in orders_cache:
            o_url = f"{config.ERP_BASE_URL}/admin/orders/{order_id}"
            o_resp = erp._SESSION.get(o_url, headers=headers)
            if o_resp.status_code == 200:
                o_data = o_resp.json()
                orders_cache[order_id] = o_data.get("data", o_data)
        
        order_data = orders_cache.get(order_id)
        if order_data and order_data.get("dealer"):
            dealer_obj = order_data["dealer"]
            if isinstance(dealer_obj, dict):
                dealer_name = dealer_obj.get("name", dealer_name)
            else:
                dealer_name = dealer_obj
                
        lines_data = order_data.get("lines", []) if order_data else []
        
        # Process each line involved in this rework
        for rl in rework_lines:
            line_id = rl.get("id")
            line_num = rl.get("line_number", "?")
            line_notes = rl.get("description", "")
            
            attributes_str = "No attributes found"
            room = "Unknown"
            original_cost = "$0.00"
            rework_cost = "$0.00"
            
            target_line = next((l for l in lines_data if l["id"] == line_id), None)
            
            if target_line:
                line_price = float(target_line.get("total_price", 0))
                original_cost = f"${line_price:.2f}"
                
                if rework_type == "fabric":
                    calc = line_price * 0.70
                    rework_cost = f"${calc:.2f}"
                else:
                    rework_cost = "$15.00"
                    
                # Build attributes summary and extract room
                product = target_line.get("product", "Unknown")
                w = target_line.get("width", "?")
                h = target_line.get("height", "?")
                qty = target_line.get("quantity", 1)
                
                fabric = "None"
                color = "None"
                for attr in target_line.get("attributes", []):
                    fk = str(attr.get("field_key", "")).lower()
                    if fk == "room":
                        room = attr.get("value", "")
                    elif "fabric" in fk:
                        fabric = attr.get("value", "")
                    elif fk in ("color", "colour"):
                        if color == "None":
                            color = attr.get("value", "")
                
                attributes_str = f"{product} | {w}x{h} | Qty:{qty} | Fab:{fabric} | Col:{color}"
            else:
                print(f"  ⚠ Line ID {line_id} not found in Order {order_number}")
                
            print(f"  ✓ RW #{req_id} | {dealer_name} | Line {line_num} | {rework_type.title()} | {rework_cost}")
            
            csv_rows.append([
                f"RW #{req_id}",
                dealer_name,
                order_number,
                rework_type.title(),
                f"Line {line_num}",
                room,
                line_notes,
                attributes_str,
                original_cost,
                rework_cost
            ])
        
    if len(csv_rows) <= 1:
        print("\nNo reworks were successfully processed.")
        return
        
    print("\n⏳ Generating CSV and sending email...")
    
    # 1. Generate CSV in memory
    csv_buffer = io.StringIO()
    writer = csv.writer(csv_buffer)
    writer.writerows(csv_rows)
    csv_content = csv_buffer.getvalue()
    
    # 2. Construct Email
    if not all([NOTIFY_EMAIL_FROM, NOTIFY_EMAIL_TO, NOTIFY_EMAIL_APP_PASSWORD]):
        print("  ⚠️  Email not sent — NOTIFY_EMAIL_* not fully configured in .env")
        print("\n--- CSV PREVIEW ---")
        print(csv_content)
        return
        
    date_str = datetime.now().strftime("%B %-d, %Y")
    subject = f"[{config.ACTIVE_COMPANY_LABEL}] Reworks Summary — {date_str}"
    
    msg = MIMEMultipart()
    msg["Subject"] = subject
    msg["From"] = NOTIFY_EMAIL_FROM
    msg["To"] = NOTIFY_EMAIL_TO
    
    body = (
        f"Hello,\n\n"
        f"Attached is the reworks summary report for {len(csv_rows)-1} processed rework(s).\n\n"
        f"Best,\n"
        f"Speedy Blinds Automation"
    )
    msg.attach(MIMEText(body, "plain"))
    
    # Attach CSV
    attachment = MIMEBase("text", "csv")
    attachment.set_payload(csv_content.encode('utf-8'))
    encoders.encode_base64(attachment)
    attachment.add_header("Content-Disposition", f"attachment; filename=reworks_summary_{datetime.now().strftime('%Y%m%d')}.csv")
    msg.attach(attachment)
    
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=15) as server:
            server.login(NOTIFY_EMAIL_FROM, NOTIFY_EMAIL_APP_PASSWORD)
            server.sendmail(NOTIFY_EMAIL_FROM, NOTIFY_EMAIL_TO, msg.as_string())
        print(f"  📧 Email sent successfully to {NOTIFY_EMAIL_TO} with CSV attached!")
    except Exception as exc:
        print(f"  ⚠️  Email send failed: {exc}")

if __name__ == "__main__":
    config.set_company("speedy")
    process_reworks()
    print("\n✓ Done.\n")
