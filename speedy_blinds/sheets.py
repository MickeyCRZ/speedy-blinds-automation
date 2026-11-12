"""
sheets.py — Google Sheets writer
=================================
Authenticates via service account and appends order rows to the
correct dealer spreadsheet.
"""

from __future__ import annotations

import os
import re
import time

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

import config
from config import (
    CREDENTIALS_FILE,
    FIRST_COLUMN,
    SHEET_TAB,
    WRITE_COLUMNS,
)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# Column letter of the last data column we write
_LAST_COLUMN = chr(ord(FIRST_COLUMN) + WRITE_COLUMNS - 1)
_RANGE_TEMPLATE = f"{SHEET_TAB}!{FIRST_COLUMN}:{_LAST_COLUMN}"

# Export for backward compatibility with older scripts and tests
DEALER_SHEETS = getattr(config, "DEALER_SHEETS", {})
DEALER_ALIASES = getattr(config, "DEALER_ALIASES", {})


def _execute_with_retry(request, max_retries=4):
    """Execute a Google API request with exponential backoff on 500-level and rate-limit errors."""
    delay = 2
    for attempt in range(max_retries):
        try:
            return request.execute()
        except HttpError as e:
            if e.resp.status in [500, 502, 503, 504, 429] and attempt < max_retries - 1:
                print(f"  ⚠️  Google API {e.resp.status} error, retrying in {delay}s...")
                time.sleep(delay)
                delay *= 2
            else:
                raise

def _get_service():
    """Build and return an authenticated Sheets API service."""
    creds_path = os.path.join(os.path.dirname(__file__), "..", CREDENTIALS_FILE)
    if not os.path.exists(creds_path):
        raise FileNotFoundError(
            f"credentials.json not found at {creds_path!r}. "
            "Download it from Google Cloud Console and place it at the project root."
        )
    creds = service_account.Credentials.from_service_account_file(
        creds_path, scopes=SCOPES
    )
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


from datetime import datetime

def _order_to_row(order: dict) -> list:
    """
    Convert an order or rework dict to a list matching columns B–G:
        B: Month
        C: Date
        D: Order Number (ORD-XXXX for orders, RW### for reworks)
        E: Customer Name  (blank for reworks)
        F: Qty            (blank for reworks)
        G: Total Cost     (ERP price for orders, text price for reworks)
    """
    is_rework = str(order.get("type") or "").lower() == "rework"

    raw_date = str(order.get("date") or "").strip()
    month_str = ""
    date_str = raw_date

    if raw_date:
        try:
            d = datetime.strptime(raw_date, "%Y-%m-%d")
            month_str = d.strftime("%B")
            date_str = f"{d.day}-{d.strftime('%B-%Y')}"
        except ValueError:
            pass

    order_num = str(order.get("order_number") or "").strip()
    if not is_rework:
        # Regular orders: ensure ORD-XXXX format
        if order_num and not order_num.upper().startswith("ORD-"):
            if order_num.isdigit():
                order_num = f"ORD-{int(order_num):04d}"
            else:
                order_num = f"ORD-{order_num}"

    if is_rework:
        customer_name = ""   # reworks have no customer
        qty_val       = ""   # reworks have no qty
    else:
        customer_name = str(order.get("customer_name") or "").strip().title()
        if order.get("unknown_dealer_fallback") and order.get("original_dealer"):
            customer_name = f"{customer_name} (Dealer: {order.get('original_dealer')})"
        qty_val = order.get("qty") if order.get("qty") is not None else ""

    dealer = order.get("dealer")
    amount = order.get("amount")
    price = order.get("price")
    final_cost = amount if amount is not None else price

    base_row = [
        date_str,
        order_num,
        customer_name,
        qty_val,
        final_cost if final_cost is not None else "",
    ]

    if dealer in ["Phil", "Alen"]:
        return [month_str, ""] + base_row
    else:
        return [month_str] + base_row


def col_to_num(col: str) -> int:
    num = 0
    for ch in col.upper():
        num = num * 26 + (ord(ch) - ord('A') + 1)
    return num


def num_to_col(num: int) -> str:
    s = ""
    while num > 0:
        num, r = divmod(num - 1, 26)
        s = chr(65 + r) + s
    return s


def _build_order_row_and_range(order: dict, next_row: int, cfg: dict | None = None, sheet_type: str = "new") -> tuple[list, str]:
    """
    Build the contiguous row values array and target range string based on configured columns.
    Dynamically positions Month, Date, Order #, Customer Name, Quantity, and Price/Cost.
    """
    cfg = cfg or {}
    tab_name = cfg.get("tab_name", SHEET_TAB)

    # Resolve configured column letters (fallback to Old vs New defaults)
    month_col = (cfg.get("month_col") or ("A" if sheet_type == "old" else "B")).upper()
    date_col = (cfg.get("date_col") or "C").upper()
    order_col = (cfg.get("order_col") or "D").upper()
    customer_col = (cfg.get("customer_col") or "E").upper()
    qty_col = (cfg.get("qty_col") or "F").upper()
    price_col = (cfg.get("price_col") or "G").upper()

    is_rework = str(order.get("type") or "").lower() == "rework"
    raw_date = str(order.get("date") or "").strip()
    month_str = ""
    date_str = raw_date
    if raw_date:
        try:
            d = datetime.strptime(raw_date, "%Y-%m-%d")
            month_str = d.strftime("%B")
            date_str = f"{d.day}-{d.strftime('%B-%Y')}"
        except ValueError:
            pass

    order_num = str(order.get("order_number") or "").strip()
    if not is_rework:
        if order_num and not order_num.upper().startswith("ORD-"):
            if order_num.isdigit():
                order_num = f"ORD-{int(order_num):04d}"
            else:
                order_num = f"ORD-{order_num}"

    if is_rework:
        customer_name = ""
        qty_val = ""
    else:
        customer_name = str(order.get("customer_name") or "").strip().title()
        if order.get("unknown_dealer_fallback") and order.get("original_dealer"):
            customer_name = f"{customer_name} (Dealer: {order.get('original_dealer')})"
        qty_val = order.get("qty") if order.get("qty") is not None else ""

    amount = order.get("amount")
    price = order.get("price")
    final_cost = amount if amount is not None else price
    cost_val = final_cost if final_cost is not None else ""

    # Map column letter -> cell value
    col_map = {}
    if month_col:
        col_map[month_col] = month_str
    if date_col:
        col_map[date_col] = date_str
    if order_col:
        col_map[order_col] = order_num
    if customer_col:
        col_map[customer_col] = customer_name
    if qty_col:
        col_map[qty_col] = qty_val
    if price_col:
        col_map[price_col] = cost_val

    sorted_cols = sorted(col_map.keys(), key=col_to_num)
    min_col_num = col_to_num(sorted_cols[0])
    max_col_num = col_to_num(sorted_cols[-1])
    min_col_str = num_to_col(min_col_num)
    max_col_str = num_to_col(max_col_num)

    row_len = max_col_num - min_col_num + 1
    row_values = [""] * row_len

    for c, val in col_map.items():
        offset = col_to_num(c) - min_col_num
        row_values[offset] = val

    target_range = f"{tab_name}!{min_col_str}{next_row}:{max_col_str}{next_row}"
    return row_values, target_range


def is_row_occupied(row_data: list, cfg: dict | None = None) -> bool:
    """
    Check if a row is genuinely occupied by an order, rework, payment, or manual note.
    Safely ignores:
      - Trailing running balance formulas (e.g. repeated '$3,172.13')
      - Default zero payment formula values (e.g. '$0.00', '0.00', '$ -', '0')
      - Empty spacer rows
    A row is only occupied if it has at least one identifying transaction field:
    Order #, Customer Name, Date, Month, or Notes/Comments.
    """
    if not row_data:
        return False

    config_dict = cfg or {}

    def get_val(col_str: str | None) -> str:
        if not col_str:
            return ""
        idx = col_to_num(col_str) - 1
        if 0 <= idx < len(row_data):
            return str(row_data[idx]).strip()
        return ""

    # 1. Primary order identifiers: Order # and Customer Name
    order_col = config_dict.get("order_col", "D")
    cust_col = config_dict.get("customer_col", "E")
    order_val = get_val(order_col)
    cust_val = get_val(cust_col)
    if order_val or cust_val:
        return True

    # 2. Date and Month (e.g. Col C, Col B or Col A)
    date_col = config_dict.get("date_col", "C")
    month_col = config_dict.get("month_col", "B")
    date_val = get_val(date_col)
    month_val = get_val(month_col)
    if date_val or month_val:
        return True

    # Check alternate Month position (Col A in old sheets)
    month_alt_val = get_val("A")
    if month_alt_val and month_alt_val.lower() in (
        "january", "february", "march", "april", "may", "june",
        "july", "august", "september", "october", "november", "december",
        "jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"
    ):
        return True

    # 3. Comments / Notes column (e.g. Col K or T)
    comments_col = config_dict.get("comments_col", "K")
    if comments_col:
        c_val = get_val(comments_col)
        if c_val:
            return True

    return False


def _ensure_row_capacity(service, sheet_id: str, tab_name: str, target_row: int) -> None:
    """
    Ensure the spreadsheet has enough rows to accommodate target_row.
    If target_row exceeds the sheet's row count, append additional rows automatically.
    """
    try:
        clean_tab = tab_name.strip("'")
        req_meta = service.spreadsheets().get(spreadsheetId=sheet_id)
        sheet_meta = _execute_with_retry(req_meta)
        target_sheet_id = None
        current_rows = 1000
        for s in sheet_meta.get("sheets", []):
            props = s.get("properties", {})
            if props.get("title") == clean_tab:
                target_sheet_id = props.get("sheetId", 0)
                current_rows = props.get("gridProperties", {}).get("rowCount", 1000)
                break

        if target_sheet_id is not None and target_row > current_rows:
            rows_to_add = max(target_row - current_rows + 50, 100)
            req_append = service.spreadsheets().batchUpdate(
                spreadsheetId=sheet_id,
                body={
                    "requests": [
                        {
                            "appendDimension": {
                                "sheetId": target_sheet_id,
                                "dimension": "ROWS",
                                "length": rows_to_add,
                            }
                        }
                    ]
                }
            )
            _execute_with_retry(req_append)
    except Exception as exc:
        print(f"  ⚠️ Warning: could not check/expand row capacity: {exc}")



def _highlight_range_red(service, sheet_id: str, updated_range: str) -> None:
    """
    Given an updatedRange like ''2026'!B14:G14', apply a light red background color.
    """
    if not updated_range:
        return

    if "!" in updated_range:
        tab_name, range_str = updated_range.split("!", 1)
        tab_name = tab_name.strip("'")
    else:
        tab_name = SHEET_TAB
        range_str = updated_range

    match = re.search(r"\d+", range_str)
    if not match:
        return
    row_num = int(match.group())
    row_index = row_num - 1   # 0-based

    meta = service.spreadsheets().get(spreadsheetId=sheet_id).execute()
    tab_id = 0
    for s in meta.get("sheets", []):
        if s.get("properties", {}).get("title") == tab_name:
            tab_id = s.get("properties", {}).get("sheetId", 0)
            break

    # Send batchUpdate to color the row light red (#FFD4D4)
    req = {
        "requests": [
            {
                "repeatCell": {
                    "range": {
                        "sheetId": tab_id,
                        "startRowIndex": row_index,
                        "endRowIndex": row_index + 1,
                        "startColumnIndex": 1, # B
                        "endColumnIndex": 7,   # G
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "backgroundColor": {
                                "red": 1.0,
                                "green": 0.8,
                                "blue": 0.8,
                            }
                        }
                    },
                    "fields": "userEnteredFormat.backgroundColor",
                }
            }
        ]
    }
    service.spreadsheets().batchUpdate(spreadsheetId=sheet_id, body=req).execute()


def write_order(order: dict) -> dict:
    """
    Append a single order as a new row in the dealer's spreadsheet.

    Returns the Google Sheets API response (includes updatedRange).
    Raises ValueError if dealer is unknown or spreadsheet ID is missing.
    """
    dealer = order.get("dealer")
    if not dealer:
        raise ValueError(f"Order has no dealer: {order}")

    # Resolve canonical dealer name from aliases (case-insensitive)
    alias_map = globals().get("DEALER_ALIASES", getattr(config, "DEALER_ALIASES", {}))
    canonical_dealer = alias_map.get(str(dealer).strip().lower(), dealer)
    sheets_map = globals().get("DEALER_SHEETS", getattr(config, "DEALER_SHEETS", {}))
    sheet_id = sheets_map.get(canonical_dealer) or sheets_map.get(dealer) or config.DEALER_SHEETS.get(canonical_dealer) or config.DEALER_SHEETS.get(dealer)
    if not sheet_id:
        try:
            from speedy_blinds import dealers_manager
            dinfo = dealers_manager.get_dealer_info(dealer)
            if dinfo and dinfo.get("sheet_id"):
                canonical_dealer = dinfo["dealer_name"]
                sheet_id = dinfo["sheet_id"]
        except Exception:
            pass

    if sheet_id:
        dealer = canonical_dealer
        order["dealer"] = canonical_dealer

    if not sheet_id or sheet_id == "SPREADSHEET_ID_HERE":
        raise ValueError(
            f"No spreadsheet ID configured for dealer '{dealer}'. "
            f"Edit DEALER_SHEETS in config.py."
        )

    # Load sheet configs and determine sheet type
    dinfo = None
    configs = {}
    try:
        from speedy_blinds import dealers_manager
        dinfo = dealers_manager.get_dealer_info(dealer)
        configs = dealers_manager.get_sheet_configs()
    except Exception:
        pass

    sheet_type = dinfo.get("sheet_type", "new") if dinfo else ("old" if dealer in ["Phil", "Alen"] else "new")
    cfg = configs.get(sheet_type, configs.get("new", {}))
    tab_name = cfg.get("tab_name", SHEET_TAB)

    try:
        service = _get_service()
        
        # Find the last populated row across all columns A to M
        # Uses is_row_occupied() to protect order lines, rework lines, EFT/Cash payments,
        # comments, and lump-sum deposits while ignoring pre-filled formula defaults ($0.00 / running balance)
        req_get = service.spreadsheets().values().get(
            spreadsheetId=sheet_id,
            range=f"'{tab_name}'!A:M"
        )
        read_result = _execute_with_retry(req_get)
        
        values = read_result.get("values", [])
        last_row = 0
        for i, row_data in enumerate(values):
            if is_row_occupied(row_data, cfg=cfg):
                last_row = i + 1
        
        next_row = max(last_row + 1, 1)

        # Ensure grid capacity before writing to avoid HttpError 400 exceeds grid limits
        _ensure_row_capacity(service, sheet_id, tab_name, next_row)

        row_values, target_range = _build_order_row_and_range(order, next_row, cfg=cfg, sheet_type=sheet_type)
        body = {"values": [row_values]}

        req_update = (
            service.spreadsheets()
            .values()
            .update(
                spreadsheetId=sheet_id,
                range=target_range,
                valueInputOption="USER_ENTERED",
                body=body,
            )
        )
        try:
            result = _execute_with_retry(req_update)
        except Exception as exc:
            if "exceeds grid limits" in str(exc).lower():
                _ensure_row_capacity(service, sheet_id, tab_name, next_row + 100)
                result = _execute_with_retry(req_update)
            else:
                raise exc
        
        # Mock the updates dict structure that append() normally returns
        result["updates"] = {"updatedRange": result.get("updatedRange", target_range)}
        
        if order.get("unknown_dealer_fallback"):
            updated_range = result.get("updates", {}).get("updatedRange")
            if updated_range:
                _highlight_range_red(service, sheet_id, updated_range)
        return result
    except HttpError as e:
        raise RuntimeError(
            f"Google Sheets API error for dealer '{dealer}': {e}"
        ) from e


def write_orders(orders: list[dict], skip_unknown: bool = True) -> tuple[int, int]:
    """
    Write multiple orders. Returns (success_count, skipped_count).
    If skip_unknown=True, dealers without a configured sheet ID are skipped
    (with a warning) instead of raising an exception.
    """
    success = 0
    skipped = 0
    for order in orders:
        try:
            write_order(order)
            success += 1
        except ValueError as e:
            if skip_unknown:
                print(f"  ⚠️  Skipped: {e}")
                skipped += 1
            else:
                raise
    return success, skipped
