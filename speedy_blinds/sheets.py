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
    base_row = [
        date_str,
        order_num,
        customer_name,
        qty_val,
        order.get("price") if order.get("price") is not None else "",
    ]

    if dealer in ["Phil", "Alen"]:
        return [month_str, ""] + base_row
    else:
        return [month_str] + base_row



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

    sheet_id = config.DEALER_SHEETS.get(dealer)
    if not sheet_id or sheet_id == "SPREADSHEET_ID_HERE":
        raise ValueError(
            f"No spreadsheet ID configured for dealer '{dealer}'. "
            f"Edit DEALER_SHEETS in config.py."
        )

    row = _order_to_row(order)
    body = {"values": [row]}

    try:
        service = _get_service()
        
        # Find the last populated row based on column B or D
        req_get = service.spreadsheets().values().get(
            spreadsheetId=sheet_id,
            range=f"{SHEET_TAB}!B:D"
        )
        read_result = _execute_with_retry(req_get)
        
        values = read_result.get("values", [])
        last_row = 0
        for i, row_data in enumerate(values):
            b_val = str(row_data[0]).strip() if len(row_data) > 0 else ""
            d_val = str(row_data[2]).strip() if len(row_data) > 2 else ""
            if b_val or d_val:
                last_row = i + 1
        
        next_row = max(last_row + 1, 1)
        
        if dealer in ["Phil", "Alen"]:
            target_range = f"{SHEET_TAB}!A{next_row}:G{next_row}"
        else:
            target_range = f"{SHEET_TAB}!B{next_row}:G{next_row}"

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
        result = _execute_with_retry(req_update)
        
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
