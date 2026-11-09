"""
payments_service.py — Service layer for EFT & Cash payments processing
========================================================================
Handles:
1. Groq AI parsing of unstructured and structured bulk payment text.
2. ERP endpoint order lookup to auto-populate canonical dealer names.
3. Pre-checking order existence and cell occupancy in dealer spreadsheets.
4. Applying EFT & Cash payments (with negative sign support and comments)
   to configured columns based on Old Sheet vs New Sheet layouts.
5. If order not found in sheet, appending a new row at the bottom and
   highlighting in light red.
"""

from __future__ import annotations

import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import requests
from googleapiclient.errors import HttpError

import config
from speedy_blinds import dealers_manager, erp, sheets

_ROOT_DIR = Path(__file__).parent.parent.resolve()
_PATCH_LOG_PATH = _ROOT_DIR / "erp_updates.log"


def _col_to_index(col_letter: str) -> int:
    """Convert column letter (e.g. 'A', 'D', 'H') to 0-based index."""
    col = (col_letter or "A").strip().upper()
    idx = 0
    for char in col:
        idx = idx * 26 + (ord(char) - ord("A") + 1)
    return max(idx - 1, 0)


def _log_payment_action(action: str, dealer: str, order_number: str, detail: str) -> None:
    """Append one line to the audit log for every payment push attempt."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    line = f"{ts} | PAYMENT_{action} | {dealer} | {order_number} | {detail}\n"
    try:
        with open(_PATCH_LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError:
        pass


def _normalize_order_num(val: Any) -> str:
    """Normalize order number for robust matching (e.g. 'ORD-0246' -> '246')."""
    if not val:
        return ""
    s = str(val).strip()
    digits = re.sub(r"\D", "", s)
    if digits:
        return digits.lstrip("0") or "0"
    return s.upper()


# ---------------------------------------------------------------------------
# 1. Groq AI Parser for Bulk Payments & Charges
# ---------------------------------------------------------------------------

_GROQ_PAYMENT_PROMPT = """You are an intelligent data extraction assistant for an order processing automation system.
Extract payment and charge entries from the provided input text.
The text may be raw WhatsApp chats, bank alert SMS, emails, or structured text like:
'Linked Order number: Amount : Sender/Money Given by/ Description : Type/Method : Dealer'

For each entry found, extract a JSON object with these EXACT keys:
- "entry_type": string ("payment" or "charge". If text mentions surcharge, charge, fee, rework fee, remake, freight, or extra, use "charge"; otherwise use "payment")
- "order_number": string (normalize to ORD-XXXX if order number has digits, or preserve format)
- "amount": number (positive float, or negative float if text says 'minus', 'refund', 'deduction', 'less', or has a minus sign)
- "type": string (for payments: "EFT", "Cash", or "CC". For charges: "Charge")
- "description": string (the reason, fee description, or sender/payer name)
- "sender": string (the name of the payer, sender, bank reference, or money giver; empty string if none)
- "dealer": string (dealer name if explicitly mentioned, otherwise empty string)
- "payment_date": string (YYYY-MM-DD format if date is mentioned; otherwise empty string)
- "is_negative": boolean (true if the amount represents a negative deduction/refund/minus)

Return ONLY a valid JSON array of these objects with no markdown backticks, no code fence, and no explanation.
If no entries are found, return [].
"""


def _parse_fallback_regex(raw_text: str, default_date: str | None = None) -> list[dict[str, Any]]:
    """Fallback parser for standard colon, comma, or tab delimited lines."""
    results = []
    lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
    today_str = default_date or datetime.now().strftime("%Y-%m-%d")

    for line in lines:
        if line.lower().startswith("linked order") or line.lower().startswith("order"):
            # Header line
            continue

        # Split on colon, comma, or pipe
        parts = [p.strip() for p in re.split(r"[:|,]", line) if p.strip()]
        if not parts:
            continue

        order_raw = parts[0]
        # Normalize order
        match_ord = re.search(r"(?:ORD[-_ ]?)?(\d+)", order_raw, re.IGNORECASE)
        ord_num = f"ORD-{int(match_ord.group(1)):04d}" if match_ord else order_raw

        amount = 0.0
        is_neg = False
        sender = ""
        ptype = "EFT"
        entry_type = "payment"
        dealer = ""

        if len(parts) > 1:
            amt_str = parts[1].replace("$", "").replace(",", "").strip()
            if "-" in amt_str or "minus" in amt_str.lower() or "refund" in amt_str.lower():
                is_neg = True
            clean_amt = re.sub(r"[^\d.]", "", amt_str)
            try:
                amount = float(clean_amt) if clean_amt else 0.0
                if is_neg and amount > 0:
                    amount = -amount
            except ValueError:
                amount = 0.0

        if len(parts) > 2:
            sender = parts[2]
            if any(k in sender.lower() for k in ("charge", "surcharge", "fee", "rework", "remake", "freight", "ladder")):
                entry_type = "charge"
                ptype = "Charge"

        if len(parts) > 3:
            raw_type = parts[3].upper()
            if "CHARGE" in raw_type or "FEE" in raw_type:
                entry_type = "charge"
                ptype = "Charge"
            elif "CASH" in raw_type:
                ptype = "Cash"
                entry_type = "payment"
            elif "CC" in raw_type or "CREDIT" in raw_type:
                ptype = "CC"
                entry_type = "payment"
            else:
                ptype = "EFT"
                entry_type = "payment"

        if len(parts) > 4:
            dealer = parts[4]

        results.append({
            "entry_type": entry_type,
            "order_number": ord_num,
            "amount": amount,
            "type": ptype,
            "description": sender or ("Charge" if entry_type == "charge" else "Payment"),
            "sender": sender,
            "dealer": dealer,
            "payment_date": today_str,
            "is_negative": is_neg or (amount < 0),
            "post_to_erp": True,
            "push_to_sheets": True,
        })

    return results


def parse_bulk_payments_with_groq(raw_text: str, default_date: str | None = None) -> list[dict[str, Any]]:
    """
    Parse freeform or structured bulk payments and charges text using Groq AI.
    Falls back to regex parsing if Groq is unavailable.
    """
    clean_text = (raw_text or "").strip()
    if not clean_text:
        return []

    today_str = default_date or datetime.now().strftime("%Y-%m-%d")

    # Try Groq AI first
    groq_key = os.getenv("GROQ_API_KEY") or getattr(config, "GROQ_API_KEY", "")
    if groq_key:
        try:
            from groq import Groq
            client = Groq(api_key=groq_key)
            resp = client.chat.completions.create(
                model=getattr(config, "GROQ_MODEL", "openai/gpt-oss-120b"),
                messages=[
                    {"role": "system", "content": _GROQ_PAYMENT_PROMPT},
                    {"role": "user", "content": f"Parse this payment / charge input text:\n\n{clean_text}"},
                ],
                temperature=0.0,
            )
            content = resp.choices[0].message.content.strip()
            # Clean possible markdown formatting
            if content.startswith("```"):
                content = re.sub(r"^```[a-zA-Z]*\n?", "", content)
                content = re.sub(r"\n?```$", "", content)
            items = json.loads(content)
            if isinstance(items, list):
                # Ensure defaults
                for it in items:
                    if not it.get("payment_date"):
                        it["payment_date"] = today_str
                    amt = float(it.get("amount") or 0.0)
                    if it.get("is_negative") and amt > 0:
                        amt = -amt
                    it["amount"] = amt
                    it["is_negative"] = bool(it.get("is_negative") or amt < 0)
                    
                    e_type = str(it.get("entry_type") or "payment").lower()
                    it["entry_type"] = "charge" if "charge" in e_type or "fee" in e_type else "payment"

                    raw_t = str(it.get("type", "")).upper()
                    if it["entry_type"] == "charge":
                        it["type"] = "Charge"
                    elif "CASH" in raw_t:
                        it["type"] = "Cash"
                    elif "CC" in raw_t or "CREDIT" in raw_t:
                        it["type"] = "CC"
                    else:
                        it["type"] = "EFT"

                    if not it.get("description"):
                        it["description"] = it.get("sender") or ("Charge" if it["entry_type"] == "charge" else "Payment")

                    it["post_to_erp"] = True
                    it["push_to_sheets"] = True

                return items
        except Exception as e:
            print(f"⚠️ Groq AI parse error: {e}. Falling back to regex.")

    # Fallback parser
    return _parse_fallback_regex(clean_text, default_date=today_str)


# ---------------------------------------------------------------------------
# 2. ERP Endpoint Order Lookup & Dealer Auto-Population
# ---------------------------------------------------------------------------

def lookup_order_in_erp(order_number: str, preferred_company: str | None = None) -> dict[str, Any]:
    """
    Look up an order number in ERP to discover its price, status, payment status,
    dealer ID, and canonical dealer name.
    Searches preferred company first, then checks across all companies if needed.
    """
    ord_clean = (order_number or "").strip()
    if not ord_clean:
        return {"found": False, "error": "Order number is empty"}

    companies = [preferred_company] if preferred_company in config.COMPANY else ["speedy", "inspira", "houston"]

    for ckey in companies:
        try:
            token = erp._login() if not erp._is_token_valid(*erp._read_cached_token()) else erp.get_token()
            fin = erp.fetch_order_financial_details(ord_clean, token)
            if fin:
                erp_dealer = fin.get("dealer_name") or ""
                dealer_id = fin.get("dealer_id")
                dinfo = dealers_manager.get_dealer_info(erp_dealer, ckey)
                canonical_name = dinfo["dealer_name"] if dinfo else erp_dealer
                sheet_type = dinfo.get("sheet_type", "new") if dinfo else "new"

                return {
                    "found": True,
                    "order_number": ord_clean,
                    "company_key": ckey,
                    "company_label": config.COMPANY[ckey]["label"],
                    "erp_id": fin.get("erp_id"),
                    "total_price": fin.get("total_price"),
                    "total_quoted_price": fin.get("total_quoted_price"),
                    "status": fin.get("status"),
                    "payment_status": fin.get("payment_status", "due"),
                    "actual_commission": fin.get("actual_commission", 0.0),
                    "total_payments_received": fin.get("total_payments_received", 0.0),
                    "dealer_id": dealer_id,
                    "raw_dealer": erp_dealer,
                    "dealer": canonical_name,
                    "sheet_type": sheet_type,
                    "blind_count": fin.get("blind_count", 0),
                }

            # Fallback to older fetch_price
            res = erp.fetch_price(ord_clean, token)
            if res:
                total_price, erp_id, status, erp_dealer, blind_count = res
                dinfo = dealers_manager.get_dealer_info(erp_dealer, ckey)
                canonical_name = dinfo["dealer_name"] if dinfo else erp_dealer
                sheet_type = dinfo.get("sheet_type", "new") if dinfo else "new"

                return {
                    "found": True,
                    "order_number": ord_clean,
                    "company_key": ckey,
                    "company_label": config.COMPANY[ckey]["label"],
                    "erp_id": erp_id,
                    "total_price": total_price,
                    "status": status,
                    "payment_status": "due",
                    "dealer_id": None,
                    "raw_dealer": erp_dealer,
                    "dealer": canonical_name,
                    "sheet_type": sheet_type,
                    "blind_count": blind_count,
                }
        except Exception:
            continue

    return {"found": False, "order_number": ord_clean}


def enrich_payments_with_erp(payments: list[dict[str, Any]], preferred_company: str | None = None) -> list[dict[str, Any]]:
    """
    Enrich a list of parsed payments or charges with ERP dealer and order details in parallel.
    Mutates items with ERP findings.
    """
    if not payments:
        return []

    def _lookup_item(item: dict[str, Any]) -> dict[str, Any]:
        ord_num = item.get("order_number") or ""
        erp_res = lookup_order_in_erp(ord_num, preferred_company=preferred_company)
        if erp_res.get("found"):
            if not item.get("dealer"):
                item["dealer"] = erp_res.get("dealer")
            item["dealer_id"] = erp_res.get("dealer_id")
            item["company_key"] = erp_res.get("company_key")
            item["erp_price"] = erp_res.get("total_price")
            item["erp_status"] = erp_res.get("status")
            item["payment_status"] = erp_res.get("payment_status")
            item["sheet_type"] = erp_res.get("sheet_type", "new")
        else:
            if item.get("dealer"):
                dinfo = dealers_manager.get_dealer_info(item["dealer"])
                if dinfo:
                    item["dealer"] = dinfo["dealer_name"]
                    item["company_key"] = dinfo["company_key"]
                    item["sheet_type"] = dinfo.get("sheet_type", "new")
        return item

    with ThreadPoolExecutor(max_workers=min(10, len(payments))) as pool:
        futures = [pool.submit(_lookup_item, p) for p in payments]
        return [f.result() for f in futures]


def lookup_multiple_orders_in_erp(order_numbers: list[str], preferred_company: str | None = None) -> dict[str, Any]:
    """
    Look up multiple orders in parallel via ERP.
    Used for splitting a single combined payment across multiple orders.
    """
    unique_orders = []
    seen = set()
    for o in order_numbers:
        norm = str(o).strip()
        if norm and norm not in seen:
            seen.add(norm)
            unique_orders.append(norm)

    results = []
    total_price = 0.0
    dealers = set()
    dealer_ids = set()

    with ThreadPoolExecutor(max_workers=min(len(unique_orders) or 1, 6)) as executor:
        future_map = {executor.submit(lookup_order_in_erp, o, preferred_company): o for o in unique_orders}
        for fut in as_completed(future_map):
            ord_name = future_map[fut]
            try:
                res = fut.result()
                results.append(res)
                if res.get("found"):
                    price = float(res.get("total_price") or 0.0)
                    total_price += price
                    if res.get("dealer"):
                        dealers.add(res["dealer"])
                    if res.get("dealer_id"):
                        dealer_ids.add(res["dealer_id"])
            except Exception as e:
                results.append({"found": False, "order_number": ord_name, "error": str(e)})

    order_index = {o: i for i, o in enumerate(unique_orders)}
    results.sort(key=lambda r: order_index.get(r.get("order_number", ""), 999))

    common_dealer = next(iter(dealers)) if len(dealers) == 1 else (list(dealers)[0] if dealers else "")
    common_dealer_id = next(iter(dealer_ids)) if len(dealer_ids) == 1 else (list(dealer_ids)[0] if dealer_ids else None)

    return {
        "orders": results,
        "count": len(results),
        "total_expected_price": round(total_price, 2),
        "common_dealer": common_dealer,
        "common_dealer_id": common_dealer_id,
        "multiple_dealers_detected": len(dealers) > 1,
    }



# ---------------------------------------------------------------------------
# 3. Sheet Pre-Check
# ---------------------------------------------------------------------------

def check_payment_in_sheet(
    dealer: str,
    order_number: str,
    payment_type: str = "EFT",
    company_key: str | None = None,
) -> dict[str, Any]:
    """
    Inspect the dealer's spreadsheet to check if the order row exists and whether
    the payment cells already contain values.
    """
    dinfo = dealers_manager.get_dealer_info(dealer, company_key)
    if not dinfo or not dinfo.get("sheet_id"):
        return {"found": False, "error": f"Dealer '{dealer}' has no configured spreadsheet ID."}

    sheet_id = dinfo["sheet_id"]
    sheet_type = dinfo.get("sheet_type", "new")
    configs = dealers_manager.get_sheet_configs()
    cfg = configs.get(sheet_type, configs.get("new", {}))

    tab_name = cfg.get("tab_name", "2026")
    order_col = (cfg.get("order_col") or "D").upper()
    eft_col = (cfg.get("eft_col") or "H").upper()
    eft_alt_col = (cfg.get("eft_alt_col") or cfg.get("alt_col") or "J").upper()
    cash_col = (cfg.get("cash_col") or "I").upper()
    cash_alt_col = (cfg.get("cash_alt_col") or "L").upper()
    comments_col = (cfg.get("comments_col") or "K").upper()

    is_eft = (payment_type or "").upper() == "EFT"
    primary_col = eft_col if is_eft else cash_col
    alt_col = eft_alt_col if is_eft else cash_alt_col

    is_general_deposit = not order_number or str(order_number).strip().upper() in ("LUMP-SUM", "LUMPSUM", "GENERAL", "ACCOUNT", "NONE", "UNLINKED", "BLANK")
    if is_general_deposit:
        return {
            "found": False,
            "is_general_deposit": True,
            "dealer": dinfo["dealer_name"],
            "sheet_type": sheet_type,
            "sheet_id": sheet_id,
            "tab_name": tab_name,
            "order_col": order_col,
            "primary_col": primary_col,
            "alt_col": alt_col,
            "message": "General deposit without order number. Will append to an empty row at the bottom as 'LUMP-SUM' (protected from WhatsApp orders).",
        }

    service = sheets._get_service()
    try:
        # Read the order column
        req = service.spreadsheets().values().get(
            spreadsheetId=sheet_id,
            range=f"'{tab_name}'!{order_col}:{order_col}",
        )
        res = sheets._execute_with_retry(req)
        values = res.get("values", [])

        target_norm = _normalize_order_num(order_number)
        matched_row = None

        for idx, row in enumerate(values):
            if row:
                cell_val = str(row[0]).strip()
                if _normalize_order_num(cell_val) == target_norm:
                    matched_row = idx + 1
                    break

        if not matched_row:
            return {
                "found": False,
                "dealer": dinfo["dealer_name"],
                "sheet_type": sheet_type,
                "sheet_id": sheet_id,
                "tab_name": tab_name,
                "order_col": order_col,
                "message": f"Order {order_number} not found in sheet '{tab_name}'",
            }

        # Read row data for primary, alt, and comments columns
        cols_to_check = [primary_col, alt_col, comments_col]
        min_col = min(cols_to_check, key=_col_to_index)
        max_col = max(cols_to_check, key=_col_to_index)

        req_row = service.spreadsheets().values().get(
            spreadsheetId=sheet_id,
            range=f"'{tab_name}'!{min_col}{matched_row}:{max_col}{matched_row}",
        )
        row_res = sheets._execute_with_retry(req_row)
        row_vals = row_res.get("values", [[]])[0] if row_res.get("values") else []

        def _get_val_at_col(c_letter: str) -> str:
            offset = _col_to_index(c_letter) - _col_to_index(min_col)
            if 0 <= offset < len(row_vals):
                return str(row_vals[offset]).strip()
            return ""

        primary_val = _get_val_at_col(primary_col)
        alt_val = _get_val_at_col(alt_col)
        comments_val = _get_val_at_col(comments_col)

        return {
            "found": True,
            "dealer": dinfo["dealer_name"],
            "sheet_type": sheet_type,
            "sheet_id": sheet_id,
            "tab_name": tab_name,
            "row": matched_row,
            "primary_col": primary_col,
            "primary_val": primary_val,
            "alt_col": alt_col,
            "alt_val": alt_val,
            "eft_col": eft_col,
            "eft_alt_col": eft_alt_col,
            "cash_col": cash_col,
            "cash_alt_col": cash_alt_col,
            "comments_col": comments_col,
            "comments_val": comments_val,
            "is_primary_occupied": bool(primary_val),
            "is_alt_occupied": bool(alt_val),
        }
    except Exception as e:
        return {"found": False, "error": f"Google Sheets API error: {e}"}


# ---------------------------------------------------------------------------
# 4. Highlight Helper (Full Row Light Red or Amber)
# ---------------------------------------------------------------------------

def _highlight_full_row(service, sheet_id: str, tab_name: str, row_num: int, max_cols: int = 15, color: str = "red") -> None:
    """Highlight row row_num across columns A through column max_cols in light red (#FFD4D4) or soft amber (#FFF3CD)."""
    try:
        meta = service.spreadsheets().get(spreadsheetId=sheet_id).execute()
        tab_id = 0
        for s in meta.get("sheets", []):
            if s.get("properties", {}).get("title") == tab_name:
                tab_id = s.get("properties", {}).get("sheetId", 0)
                break

        row_index = row_num - 1
        bg = {"red": 1.0, "green": 0.83, "blue": 0.83} if color == "red" else {"red": 1.0, "green": 0.95, "blue": 0.80}
        req = {
            "requests": [
                {
                    "repeatCell": {
                        "range": {
                            "sheetId": tab_id,
                            "startRowIndex": row_index,
                            "endRowIndex": row_index + 1,
                            "startColumnIndex": 0,
                            "endColumnIndex": max_cols,
                        },
                        "cell": {
                            "userEnteredFormat": {
                                "backgroundColor": bg
                            }
                        },
                        "fields": "userEnteredFormat.backgroundColor",
                    }
                }
            ]
        }
        service.spreadsheets().batchUpdate(spreadsheetId=sheet_id, body=req).execute()
    except Exception as exc:
        print(f"  ⚠️ Failed to highlight row {row_num}: {exc}")


def _highlight_full_row_red(service, sheet_id: str, tab_name: str, row_num: int, max_cols: int = 15) -> None:
    _highlight_full_row(service, sheet_id, tab_name, row_num, max_cols, color="red")


# ---------------------------------------------------------------------------
# 5. Push Payment to Google Sheet
# ---------------------------------------------------------------------------

def push_payment_to_sheet(payment: dict[str, Any]) -> dict[str, Any]:
    """
    Apply a single payment to the dealer's spreadsheet.

    Workflow:
    - Resolves dealer info, sheet ID, and sheet_type ('old' or 'new').
    - Reads configured columns (order_col, eft_col, cash_col, alt_col, comments_col).
    - Matches order row using normalized order number.
    - If found:
      - Writes payment amount to primary (or alternate) column.
      - Appends '<Date>-<Sender>' to the comments column.
      - Sets warning if the target cell already had a value or was diverted to alternate.
    - If NOT found (User Choice A4):
      - Appends a new row at the bottom of the sheet with order number, amount, and comments.
      - Highlights the newly appended row in light red.
    """
    dealer_raw = str(payment.get("dealer") or "").strip()
    order_raw = str(payment.get("order_number") or "").strip()
    amount_raw = payment.get("amount", 0.0)
    ptype = str(payment.get("type") or "EFT").strip()
    sender = str(payment.get("sender") or "").strip()
    pay_date = str(payment.get("payment_date") or "").strip() or datetime.now().strftime("%Y-%m-%d")
    is_neg = bool(payment.get("is_negative") or False)
    target_mode = str(payment.get("target_column_mode") or "primary").lower()
    company_key = payment.get("company_key")

    if not dealer_raw:
        raise ValueError(f"Payment has no dealer specified for order '{order_raw}'.")

    # If order_number is blank, general, or lump-sum: treat as an account lump-sum deposit
    is_general_deposit = not order_raw or str(order_raw).strip().upper() in ("LUMP-SUM", "LUMPSUM", "GENERAL", "ACCOUNT", "NONE", "UNLINKED", "BLANK")
    if is_general_deposit:
        order_raw = "LUMP-SUM"

    # Calculate final numeric amount
    try:
        amount_val = float(amount_raw)
        if is_neg and amount_val > 0:
            amount_val = -amount_val
        elif not is_neg and "-" in str(amount_raw):
            amount_val = -abs(amount_val)
    except (ValueError, TypeError):
        amount_val = 0.0

    # Resolve dealer
    dinfo = dealers_manager.get_dealer_info(dealer_raw, company_key)
    if not dinfo or not dinfo.get("sheet_id"):
        raise ValueError(f"No configured spreadsheet ID for dealer '{dealer_raw}'.")

    sheet_id = dinfo["sheet_id"]
    sheet_type = dinfo.get("sheet_type", "new")
    canonical_dealer = dinfo["dealer_name"]

    # Load sheet configs
    configs = dealers_manager.get_sheet_configs()
    cfg = configs.get(sheet_type, configs.get("new", {}))

    tab_name = cfg.get("tab_name", "2026")
    order_col = (cfg.get("order_col") or "D").upper()
    eft_col = (cfg.get("eft_col") or "H").upper()
    eft_alt_col = (cfg.get("eft_alt_col") or cfg.get("alt_col") or "J").upper()
    cash_col = (cfg.get("cash_col") or "I").upper()
    cash_alt_col = (cfg.get("cash_alt_col") or "L").upper()
    comments_col = (cfg.get("comments_col") or "K").upper()

    is_eft = ptype.upper() == "EFT"
    primary_col = eft_col if is_eft else cash_col
    alt_col = eft_alt_col if is_eft else cash_alt_col

    service = sheets._get_service()

    # Search for order number row in order_col (skip search for general lump-sum deposits)
    matched_row = None
    if not is_general_deposit:
        req_orders = service.spreadsheets().values().get(
            spreadsheetId=sheet_id,
            range=f"'{tab_name}'!{order_col}:{order_col}",
        )
        res_orders = sheets._execute_with_retry(req_orders)
        values = res_orders.get("values", [])

        target_norm = _normalize_order_num(order_raw)

        for idx, row in enumerate(values):
            if row:
                cval = str(row[0]).strip()
                if _normalize_order_num(cval) == target_norm:
                    matched_row = idx + 1
                    break

    new_comment = f"{pay_date}-{sender}" if sender else pay_date
    warning_msg = None

    if matched_row:
        # ORDER FOUND IN SHEET
        # Determine target payment column
        chosen_col = primary_col
        if target_mode == "alternate":
            chosen_col = alt_col
        elif target_mode == "auto":
            # Check if primary cell has value
            req_check = service.spreadsheets().values().get(
                spreadsheetId=sheet_id,
                range=f"'{tab_name}'!{primary_col}{matched_row}",
            )
            res_check = sheets._execute_with_retry(req_check)
            existing_val = str(res_check.get("values", [[""]])[0][0]).strip() if res_check.get("values") else ""
            if existing_val:
                chosen_col = alt_col
                warning_msg = f"Primary {ptype} cell {primary_col}{matched_row} already had '{existing_val}'. Wrote to Alternate {ptype} Col {alt_col}."
            else:
                chosen_col = primary_col

        # Check existing value in the chosen cell
        if not warning_msg:
            req_cell = service.spreadsheets().values().get(
                spreadsheetId=sheet_id,
                range=f"'{tab_name}'!{chosen_col}{matched_row}",
            )
            res_cell = sheets._execute_with_retry(req_cell)
            ex_val = str(res_cell.get("values", [[""]])[0][0]).strip() if res_cell.get("values") else ""
            if ex_val:
                warning_msg = f"Warning: Cell {chosen_col}{matched_row} already had '{ex_val}'. Overwritten with {amount_val}."

        # Fetch existing comment in comments_col
        req_comm = service.spreadsheets().values().get(
            spreadsheetId=sheet_id,
            range=f"'{tab_name}'!{comments_col}{matched_row}",
        )
        res_comm = sheets._execute_with_retry(req_comm)
        existing_comm = str(res_comm.get("values", [[""]])[0][0]).strip() if res_comm.get("values") else ""
        final_comment = f"{existing_comm}; {new_comment}" if existing_comm else new_comment

        # Batch update: payment amount and comments
        data_updates = [
            {
                "range": f"'{tab_name}'!{chosen_col}{matched_row}",
                "values": [[amount_val]],
            },
            {
                "range": f"'{tab_name}'!{comments_col}{matched_row}",
                "values": [[final_comment]],
            },
        ]
        body_update = {
            "valueInputOption": "USER_ENTERED",
            "data": data_updates,
        }
        service.spreadsheets().values().batchUpdate(spreadsheetId=sheet_id, body=body_update).execute()

        detail = f"Row {matched_row} | {chosen_col}{matched_row}={amount_val} | {comments_col}{matched_row}={final_comment}"
        if warning_msg:
            detail += f" | {warning_msg}"
        _log_payment_action("MATCHED", canonical_dealer, order_raw, detail)

        return {
            "status": "success",
            "dealer": canonical_dealer,
            "order_number": order_raw,
            "row": matched_row,
            "payment_cell": f"{chosen_col}{matched_row}",
            "amount": amount_val,
            "comments_cell": f"{comments_col}{matched_row}",
            "comments": final_comment,
            "sheet_type": sheet_type,
            "warning": warning_msg,
        }

    else:
        # ORDER NOT FOUND IN SHEET OR GENERAL LUMP-SUM DEPOSIT -> APPEND TO FIRST TRULY EMPTY ROW
        # Read columns A through M to ensure orders, reworks, payments, and comments are all accounted for
        req_all = service.spreadsheets().values().get(
            spreadsheetId=sheet_id,
            range=f"'{tab_name}'!A:M",
        )
        res_all = sheets._execute_with_retry(req_all)
        all_vals = res_all.get("values", [])
        last_row = 0
        for i, r in enumerate(all_vals):
            # If ANY cell in this row has content, it is occupied and protected
            if any(str(c).strip() for c in r):
                last_row = i + 1

        next_row = max(last_row + 1, 1)

        # Parse date for Month and Date columns
        month_str = ""
        date_display = pay_date
        try:
            d = datetime.strptime(pay_date, "%Y-%m-%d")
            month_str = d.strftime("%B")
            date_display = f"{d.day}-{d.strftime('%B-%Y')}"
        except Exception:
            pass

        # Write Order # in order_col, Amount in primary_col, Comment in comments_col
        data_updates = [
            {
                "range": f"'{tab_name}'!{order_col}{next_row}",
                "values": [[order_raw]],
            },
            {
                "range": f"'{tab_name}'!{primary_col}{next_row}",
                "values": [[amount_val]],
            },
            {
                "range": f"'{tab_name}'!{comments_col}{next_row}",
                "values": [[new_comment]],
            },
        ]

        # Populate Month (Col B) and Date (Col C) if order_col is D
        if order_col.upper() == "D":
            if month_str:
                data_updates.append({
                    "range": f"'{tab_name}'!B{next_row}",
                    "values": [[month_str]],
                })
            if date_display:
                data_updates.append({
                    "range": f"'{tab_name}'!C{next_row}",
                    "values": [[date_display]],
                })

        body_update = {
            "valueInputOption": "USER_ENTERED",
            "data": data_updates,
        }
        service.spreadsheets().values().batchUpdate(spreadsheetId=sheet_id, body=body_update).execute()

        # Highlight newly appended row: soft amber for general lump-sum deposit, light red for missing specific order
        hl_color = "amber" if is_general_deposit else "red"
        _highlight_full_row(service, sheet_id, tab_name, next_row, max_cols=20, color=hl_color)

        if is_general_deposit:
            warn = f"General account deposit recorded at empty Row {next_row} (Order Col set to 'LUMP-SUM'). Protected from WhatsApp orders."
            status_code = "lump_sum_appended"
            action_tag = "LUMP_SUM_AMBER"
        else:
            warn = f"Order {order_raw} not found in sheet '{tab_name}'. Appended at empty Row {next_row} and highlighted in red. Protected from WhatsApp orders."
            status_code = "appended_not_found"
            action_tag = "APPENDED_RED"

        detail = f"Row {next_row} ({status_code.upper()}) | {order_col}{next_row}={order_raw} | {primary_col}{next_row}={amount_val} | {warn}"
        _log_payment_action(action_tag, canonical_dealer, order_raw, detail)

        return {
            "status": status_code,
            "dealer": canonical_dealer,
            "order_number": order_raw,
            "row": next_row,
            "payment_cell": f"{primary_col}{next_row}",
            "amount": amount_val,
            "comments_cell": f"{comments_col}{next_row}",
            "comments": new_comment,
            "sheet_type": sheet_type,
            "is_general_deposit": is_general_deposit,
            "highlighted": True,
            "highlight_color": hl_color,
            "highlighted_red": hl_color == "red",
            "warning": warn,
        }


# ---------------------------------------------------------------------------
# 6. Push Batch Payments
# ---------------------------------------------------------------------------

def push_payments_batch(payments: list[dict[str, Any]]) -> dict[str, Any]:
    """Push multiple approved payments to Google Sheets."""
    results = []
    succeeded = 0
    appended_new = 0
    failed = 0

    for p in payments:
        try:
            res = push_payment_to_sheet(p)
            results.append({"order_number": p.get("order_number"), "success": True, "result": res})
            if res.get("status") == "appended_not_found":
                appended_new += 1
            else:
                succeeded += 1
        except Exception as e:
            failed += 1
            results.append({"order_number": p.get("order_number"), "success": False, "error": str(e)})

    return {
        "total": len(payments),
        "succeeded": succeeded,
        "appended_new": appended_new,
        "failed": failed,
        "results": results,
    }


# ---------------------------------------------------------------------------
# 7. ERP Ledger & Unified Transaction Execution (Payments & Charges)
# ---------------------------------------------------------------------------

def apply_charge_to_sheet(charge: dict[str, Any]) -> dict[str, Any]:
    """
    Apply a charge to the dealer's spreadsheet.
    Charges are entered into the sheet with amount and '<Date>-CHARGE: <Description>'.
    """
    # Uses push_payment_to_sheet with charge formatted comments
    c = dict(charge)
    desc = c.get("description") or c.get("sender") or "Charge"
    pay_date = c.get("payment_date") or datetime.now().strftime("%Y-%m-%d")
    c["sender"] = f"CHARGE: {desc}"
    return push_payment_to_sheet(c)


def execute_transaction(item: dict[str, Any]) -> dict[str, Any]:
    """
    Execute a single transaction:
    1. If post_to_erp is True: Calls ERP API to record payment or charge in the financial ledger.
    2. If push_to_sheets is True: Applies payment or charge to the dealer's Google Sheet.
    Returns combined execution status.
    """
    entry_type = str(item.get("entry_type") or ("charge" if str(item.get("type", "")).upper() == "CHARGE" else "payment")).lower()
    post_to_erp = bool(item.get("post_to_erp", True))
    push_to_sheets = bool(item.get("push_to_sheets", True))

    ord_num = str(item.get("order_number") or "").strip()
    amount = float(item.get("amount") or 0.0)
    dealer = str(item.get("dealer") or "").strip()
    company_key = item.get("company_key")
    dealer_id = item.get("dealer_id")

    is_general_lump = not ord_num or ord_num.upper() in ("LUMP-SUM", "LUMPSUM", "GENERAL", "ACCOUNT", "NONE", "UNLINKED", "BLANK")
    erp_ord_num = None if is_general_lump else ord_num

    # If dealer_id not in item, attempt to resolve
    if post_to_erp and not dealer_id:
        if not is_general_lump and ord_num:
            lookup = lookup_order_in_erp(ord_num, preferred_company=company_key)
            if lookup.get("dealer_id"):
                dealer_id = lookup["dealer_id"]
                item["dealer_id"] = dealer_id
                if not dealer:
                    dealer = lookup.get("dealer", "")
                    item["dealer"] = dealer
        elif dealer:
            # For general lump-sum deposits, try to get dealer_id from dealer info
            dinfo = dealers_manager.get_dealer_info(dealer, company_key)
            if dinfo and dinfo.get("dealer_id"):
                dealer_id = dinfo["dealer_id"]
                item["dealer_id"] = dealer_id

    erp_result = None
    sheet_result = None
    errors = []

    # 1. Post to ERP Ledger (Minus toggle is strictly for Google Sheets; ERP ledger always takes positive amount)
    if post_to_erp:
        if not dealer_id:
            errors.append(f"ERP post skipped: Dealer ID could not be resolved for '{dealer or ord_num}'")
        else:
            try:
                # Set company context
                if company_key and company_key in config.COMPANY:
                    config.set_company(company_key)

                if entry_type == "charge":
                    desc = item.get("description") or item.get("sender") or (f"Charge for {dealer}" if is_general_lump else f"Charge for {ord_num}")
                    erp_result = erp.record_dealer_charge(
                        dealer_id=int(dealer_id),
                        amount=abs(amount),
                        order_number=erp_ord_num,
                        description=desc,
                    )
                else:
                    pm = str(item.get("type") or "EFT").upper()
                    sender_desc = item.get("sender") or item.get("description") or (f"Lump-sum payment from {dealer}" if is_general_lump else f"Payment for {ord_num}")
                    erp_result = erp.record_dealer_payment(
                        dealer_id=int(dealer_id),
                        amount=abs(amount),
                        order_number=erp_ord_num,
                        payment_method=pm,
                        description=sender_desc,
                    )
            except Exception as e:
                errors.append(f"ERP API error: {e}")

    # 2. Push to Google Sheets (Supports negative amounts for deductions/refunds when is_negative is True)
    if push_to_sheets:
        try:
            if entry_type == "charge":
                sheet_result = apply_charge_to_sheet(item)
            else:
                sheet_result = push_payment_to_sheet(item)
        except Exception as e:
            errors.append(f"Google Sheet error: {e}")

    success = len(errors) == 0
    sheet_val = -abs(amount) if (item.get("is_negative") or amount < 0) else abs(amount)
    return {
        "success": success,
        "entry_type": entry_type,
        "order_number": ord_num,
        "dealer": dealer,
        "dealer_id": dealer_id,
        "amount": amount,
        "sheet_amount": sheet_val,
        "erp_amount": abs(amount),
        "post_to_erp": post_to_erp,
        "push_to_sheets": push_to_sheets,
        "erp_result": erp_result,
        "sheet_result": sheet_result,
        "errors": errors,
    }


def execute_transactions_batch(items: list[dict[str, Any]]) -> dict[str, Any]:
    """Execute batch of approved payment and charge transactions."""
    results = []
    succeeded = 0
    failed = 0

    for it in items:
        res = execute_transaction(it)
        results.append(res)
        if res.get("success"):
            succeeded += 1
        else:
            failed += 1

    return {
        "total": len(items),
        "succeeded": succeeded,
        "failed": failed,
        "results": results,
    }


def get_dealer_erp_balance_summary(dealer_id: int, company_key: str | None = None) -> dict[str, Any]:
    """Fetch live balance summary for a dealer from the ERP backend."""
    if company_key and company_key in config.COMPANY:
        config.set_company(company_key)
    return erp.get_dealer_balance_summary(dealer_id)


def get_tenant_accounts_summary(company_key: str | None = None) -> dict[str, Any]:
    """Fetch global accounts summary across all dealers for active tenant."""
    if company_key and company_key in config.COMPANY:
        config.set_company(company_key)
    return erp.get_accounts_summary()


