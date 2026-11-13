"""
web_services.py — Non-interactive API services for the Web Application
======================================================================
Wraps all automation logic (WhatsApp orders, MIR calculation, Bulk Complete,
Price Checking, Price Reviewing, Reworks) into clean, JSON-serializable
functions suitable for web endpoints.
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import smtplib
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any, Optional

import requests

import config
import erp
import mir
import notifier
import parser as order_parser
import sheets

# Path to project root
_PROJECT_ROOT = Path(__file__).parent.parent
_ENV_PATH = _PROJECT_ROOT / ".env"
_PATCH_LOG_PATH = _PROJECT_ROOT / "erp_updates.log"

# Dedicated session for read-only checks so we never monkeypatch or block erp._SESSION
_READONLY_SESSION = requests.Session()
_READONLY_SESSION.verify = False
_READONLY_SESSION.headers.update({"Accept": "application/json", "Content-Type": "application/json"})


def to_float(val: Any, default: float = 0.0) -> float:
    try:
        return float(val) if val is not None else default
    except (ValueError, TypeError):
        return default


def round_price(val: Any) -> float:
    return float(Decimal(str(val)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


# ---------------------------------------------------------------------------
# Company and System Status
# ---------------------------------------------------------------------------

def get_companies() -> list[dict[str, Any]]:
    """Return all configured companies with labels and keys."""
    res = []
    current_key = getattr(config, "ACTIVE_COMPANY_KEY", "speedy")
    for key, cfg in config.COMPANY.items():
        res.append({
            "key": key,
            "label": cfg.get("label", key),
            "base_url": cfg.get("base_url", ""),
            "tenant_ids": cfg.get("tenant_ids", []),
            "is_active": (key == current_key)
        })
    return res


def set_active_company(key: str) -> dict[str, Any]:
    """Switch active company context across the system."""
    if key not in config.COMPANY:
        raise ValueError(f"Unknown company: {key}. Allowed: {list(config.COMPANY.keys())}")
    config.set_company(key)
    config.ACTIVE_COMPANY_KEY = key
    return {
        "success": True,
        "company_key": key,
        "label": config.ACTIVE_COMPANY_LABEL,
        "base_url": config.ERP_BASE_URL,
        "tenant_ids": config.ERP_TENANT_IDS,
    }


def get_system_status() -> dict[str, Any]:
    """Inspect environment, API keys, credentials, and token validity."""
    creds_path = _PROJECT_ROOT / "credentials.json"
    groq_configured = bool(config.GROQ_API_KEY)
    sheets_configured = creds_path.exists()
    email_configured = bool(
        config.NOTIFY_EMAIL_FROM and config.NOTIFY_EMAIL_TO and config.NOTIFY_EMAIL_APP_PASSWORD
    )

    # Token statuses per company
    tokens_status = {}
    for ckey, ccfg in config.COMPANY.items():
        tkey = ccfg["token_key"]
        ekey = ccfg["token_expires_key"]
        token = os.getenv(tkey, "")
        expires = os.getenv(ekey, "")
        is_valid = erp._is_token_valid(token, expires)
        tokens_status[ckey] = {
            "label": ccfg["label"],
            "has_token": bool(token),
            "expires_at": expires,
            "is_valid": is_valid,
        }

    active_key = getattr(config, "ACTIVE_COMPANY_KEY", "speedy")

    return {
        "active_company": {
            "key": active_key,
            "label": config.ACTIVE_COMPANY_LABEL,
            "base_url": config.ERP_BASE_URL,
            "tenant_ids": config.ERP_TENANT_IDS,
        },
        "integrations": {
            "groq_api": {"ok": groq_configured, "model": config.GROQ_MODEL},
            "google_sheets": {"ok": sheets_configured, "file": str(creds_path.name)},
            "gmail_notifications": {
                "ok": email_configured,
                "from": config.NOTIFY_EMAIL_FROM or "Not set",
                "to": config.NOTIFY_EMAIL_TO or "Not set",
            },
        },
        "tokens": tokens_status,
        "total_dealers": len(config.DEALER_SHEETS),
    }


def get_audit_logs(limit: int = 150) -> list[dict[str, Any]]:
    """Return recent ERP audit log entries (newest first)."""
    if not _PATCH_LOG_PATH.exists():
        return []

    lines = []
    try:
        with open(_PATCH_LOG_PATH, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return []

    results = []
    for line in reversed(lines[-limit:]):
        parts = [p.strip() for p in line.split("|")]
        if len(parts) >= 6:
            results.append({
                "timestamp": parts[0],
                "action": parts[1],
                "company": parts[2],
                "order_number": parts[3],
                "erp_id": parts[4],
                "http_status": parts[5],
                "detail": parts[6] if len(parts) > 6 else "",
            })
        else:
            results.append({"raw": line.strip()})
    return results


# ---------------------------------------------------------------------------
# Mode 1: WhatsApp Orders Processing
# ---------------------------------------------------------------------------

def parse_and_enrich_orders(raw_text: str, company_key: str | None = None) -> dict[str, Any]:
    """
    Parse WhatsApp text via Groq and enrich orders with ERP prices.
    Returns orders list + summary metrics.
    """
    if company_key:
        set_active_company(company_key)

    if not raw_text.strip():
        raise ValueError("No text provided to parse.")

    orders = order_parser.parse_orders(raw_text)
    if not orders:
        return {"orders": [], "summary": {"total": 0, "regular": 0, "reworks": 0, "prices_found": 0, "missing_prices": 0}}

    erp_failed = False
    try:
        erp.enrich_orders(orders)
    except Exception as exc:
        erp_failed = True
        for o in orders:
            o.setdefault("price", None)

    regular_count = sum(1 for o in orders if o.get("type") != "rework")
    rework_count = sum(1 for o in orders if o.get("type") == "rework")
    prices_found = sum(1 for o in orders if (o.get("amount") is not None or o.get("price") is not None))
    missing_prices = sum(1 for o in orders if (o.get("amount") is None and o.get("price") is None))

    # Decorate with dealer validity
    for o in orders:
        dealer = o.get("dealer")
        canonical = config.DEALER_ALIASES.get(str(dealer).strip().lower(), dealer) if dealer else None
        o["sheet_configured"] = bool(canonical and canonical in config.DEALER_SHEETS)

    return {
        "orders": orders,
        "summary": {
            "total": len(orders),
            "regular": regular_count,
            "reworks": rework_count,
            "prices_found": prices_found,
            "missing_prices": missing_prices,
            "erp_failed": erp_failed,
        },
    }


def push_orders_to_sheets(
    orders: list[dict[str, Any]],
    company_key: str | None = None,
    send_email: bool = True,
) -> dict[str, Any]:
    """
    Push selected orders to Google Sheets and dispatch HTML summary email.
    """
    if company_key:
        set_active_company(company_key)

    report = notifier.RunReport()
    report.orders_extracted = len(orders)
    report.orders_selected = len(orders)

    written = []
    unmatched = []
    errors = []

    for order in orders:
        dealer = order.get("dealer", "UNKNOWN")
        on_num = order.get("order_number", "?")

        # Track missing price for summary reporting (only if neither text amount nor ERP price exists)
        final_val = order.get("amount") if order.get("amount") is not None else order.get("price")
        if final_val is None and order.get("type") != "rework":
            report.prices_not_found.append(on_num)

        try:
            result = sheets.write_order(order)
            updated_range = result.get("updates", {}).get("updatedRange", "OK")
            written_item = {
                **order,
                "order_number": on_num,
                "dealer": dealer,
                "range": updated_range,
                "status": "success",
            }
            written.append(written_item)
            report.written.append(written_item)
        except ValueError as exc:
            err_msg = str(exc)
            if "No spreadsheet ID configured" in err_msg:
                unmatched.append({
                    "order_number": on_num,
                    "dealer": dealer,
                    "error": err_msg,
                })
                report.unmatched_dealers.append(order)
            else:
                errors.append({
                    "order_number": on_num,
                    "dealer": dealer,
                    "error": err_msg,
                })
                report.errors.append({
                    "dealer": dealer,
                    "order_number": on_num,
                    "error": err_msg,
                })
        except Exception as exc:
            err_msg = str(exc)
            errors.append({
                "order_number": on_num,
                "dealer": dealer,
                "error": err_msg,
            })
            report.errors.append({
                "dealer": dealer,
                "order_number": on_num,
                "error": err_msg,
            })

    # Save unmatched to disk if any
    if report.unmatched_dealers:
        report.save_unmatched(report.unmatched_dealers)

    has_failures = bool(errors or unmatched)
    email_sent = False
    email_error = None
    prompt_summary_email = False
    email_to = getattr(config, "NOTIFY_EMAIL_TO", "")

    if send_email and (written or errors or unmatched):
        if not has_failures and written:
            # 100% success — dispatch immediately
            try:
                dispatch_res = report.dispatch()
                email_sent = dispatch_res.get("email_sent", False)
                email_error = dispatch_res.get("email_error")
                email_to = dispatch_res.get("email_to", email_to)
            except Exception as exc:
                email_error = str(exc)
        else:
            # When any order fails to push to sheets: hold email and prompt user
            prompt_summary_email = True

    return {
        "written": written,
        "unmatched": unmatched,
        "errors": errors,
        "total_written": len(written),
        "total_unmatched": len(unmatched),
        "total_errors": len(errors),
        "email_sent": email_sent,
        "prompt_summary_email": prompt_summary_email,
        "email_to": email_to,
        "email_error": email_error,
    }


def send_orders_summary_email(
    written_orders: list[dict[str, Any]],
    failed_orders: list[dict[str, Any]] | None = None,
    company_key: str | None = None,
) -> dict[str, Any]:
    """
    Send or re-send the daily HTML summary email for specified orders
    without writing or modifying anything in Google Sheets.
    Failed orders can be included so their blinds and revenue are counted in the daily totals.
    """
    if company_key:
        set_active_company(company_key)

    report = notifier.RunReport()
    all_summary_orders = []

    for w in (written_orders or []):
        all_summary_orders.append(w)
        report.written.append(w)

    for f in (failed_orders or []):
        f_copy = {**f, "_unpushed": True}
        all_summary_orders.append(f_copy)
        report.written.append(f_copy)
        err = f.get("_sheet_error") or f.get("error") or "Failed to push to sheet"
        report.errors.append({
            "dealer": f.get("dealer", "?"),
            "order_number": f.get("order_number", "?"),
            "error": err,
        })

    report.orders_extracted = len(all_summary_orders)
    report.orders_selected = len(all_summary_orders)

    dispatch_res = report.dispatch()

    return {
        "success": True,
        "email_sent": dispatch_res.get("email_sent", False),
        "email_to": dispatch_res.get("email_to", getattr(config, "NOTIFY_EMAIL_TO", "")),
        "email_error": dispatch_res.get("email_error"),
        "total_orders": len(all_summary_orders),
        "written_count": len(written_orders or []),
        "failed_count": len(failed_orders or []),
    }


def mark_orders_completed_in_erp(orders: list[dict[str, Any]], company_key: str | None = None) -> dict[str, Any]:
    """
    Mark given orders as 'completed' in ERP.
    """
    if company_key:
        set_active_company(company_key)

    token = erp.get_token()
    results = []
    ok_count = 0
    fail_count = 0

    for o in orders:
        eid = o.get("erp_id")
        onum = o.get("order_number", "?")
        if not eid:
            results.append({"order_number": onum, "success": False, "error": "No ERP ID"})
            fail_count += 1
            continue

        success = erp.mark_order_completed(eid, onum, token)
        if success:
            ok_count += 1
            results.append({"order_number": onum, "erp_id": eid, "success": True})
        else:
            fail_count += 1
            results.append({"order_number": onum, "erp_id": eid, "success": False, "error": "PATCH failed"})

    return {
        "results": results,
        "completed_count": ok_count,
        "failed_count": fail_count,
    }


# ---------------------------------------------------------------------------
# Mode 2: MIR Calculator
# ---------------------------------------------------------------------------

def mir_parse_batch(installer_name: str, raw_text: str, company_key: str | None = None) -> dict[str, Any]:
    """
    Parse a single installer's raw text orders using Groq and pre-fetch dealer names.
    """
    if company_key:
        set_active_company(company_key)

    if not raw_text.strip():
        raise ValueError("No text provided.")

    lines = raw_text.splitlines()
    chunk_size = 20
    parsed_orders = []

    for i in range(0, len(lines), chunk_size):
        chunk = "\n".join(lines[i:i + chunk_size]).strip()
        if not chunk:
            continue
        parsed_orders.extend(mir.parse_order_jobs(chunk))

    if not parsed_orders:
        return {"installer": installer_name, "orders": [], "dealer_count": 0}

    # Pre-fetch ERP dealers
    nums_to_fetch = [o["order_number"] for o in parsed_orders]
    preview_data = mir._pre_fetch_dealers(nums_to_fetch)

    for o in parsed_orders:
        o["erp_preview"] = preview_data.get(o["order_number"])
        if not o.get("dealer_text") and not o.get("dealer_override"):
            cust = (o["erp_preview"] or {}).get("customer_name", "") or ""
            cust_dealer = mir._fuzzy_match(cust, list(config.DEALER_ALIASES.values()))
            if cust_dealer:
                erp_dealer = (o["erp_preview"] or {}).get("dealer_name", "")
                erp_dealer_fuzzy = mir._fuzzy_match(erp_dealer, list(config.DEALER_ALIASES.values()))
                o["dealer_cust_match"] = cust_dealer if erp_dealer_fuzzy != cust_dealer else None
            else:
                o["dealer_cust_match"] = None

    found_dealers = sum(1 for v in preview_data.values() if v is not None)

    return {
        "installer": installer_name,
        "orders": parsed_orders,
        "dealer_count": found_dealers,
        "total_parsed": len(parsed_orders),
    }


def mir_calculate(batches: list[dict[str, Any]], company_key: str | None = None) -> dict[str, Any]:
    """
    Calculate Dual Ledger across all batches.
    """
    if company_key:
        set_active_company(company_key)

    if not batches:
        raise ValueError("No installer batches to calculate.")

    erp_results = mir.fetch_all_batch_orders(batches)
    installer_ledger, dealer_ledger, not_found = mir.calculate_dual_ledger(batches, erp_results)

    # Format installer ledger for JSON
    installer_data = []
    grand_installer_total = 0.0
    for inst, data in sorted(installer_ledger.items()):
        total = data.get("total_pay", 0.0)
        grand_installer_total += total
        installer_data.append({
            "installer": inst,
            "orders": data.get("orders", []),
            "total": round_price(total),
        })

    # Format dealer ledger for JSON
    dealer_data = []
    grand_dealer_total = 0.0
    for dealer, ddata in sorted(dealer_ledger.items()):
        subtotal = ddata.get("total_charge", 0.0)
        grand_dealer_total += subtotal
        dealer_data.append({
            "dealer": dealer,
            "orders": ddata.get("orders", []),
            "self_orders": ddata.get("self_orders", []),
            "subtotal": round_price(subtotal),
        })

    unique_orders = len(erp_results)
    found_orders = sum(1 for v in erp_results.values() if v is not None)

    return {
        "installer_ledger": installer_data,
        "dealer_ledger": dealer_data,
        "not_found": not_found,
        "grand_installer_total": round_price(grand_installer_total),
        "grand_dealer_total": round_price(grand_dealer_total),
        "stats": {
            "unique_orders": unique_orders,
            "found_orders": found_orders,
            "missing_orders": len(not_found),
        },
    }


def mir_send_email_report(batches: list[dict[str, Any]], company_key: str | None = None) -> dict[str, Any]:
    """
    Calculate ledger and dispatch the official MIR report email.
    """
    if company_key:
        set_active_company(company_key)

    if not config.NOTIFY_EMAIL_APP_PASSWORD or not config.NOTIFY_EMAIL_FROM or not config.NOTIFY_EMAIL_TO:
        raise ValueError("Gmail notification settings (NOTIFY_EMAIL_*) not configured in .env.")

    erp_results = mir.fetch_all_batch_orders(batches)
    installer_ledger, dealer_ledger, not_found = mir.calculate_dual_ledger(batches, erp_results)

    mir.send_mir_email(installer_ledger, dealer_ledger, not_found)

    return {
        "success": True,
        "recipient": config.NOTIFY_EMAIL_TO,
        "not_found_count": len(not_found),
    }


# ---------------------------------------------------------------------------
# Mode 3: Bulk Mark Orders Completed
# ---------------------------------------------------------------------------

def bulk_lookup_orders(raw_text: str, company_key: str | None = None) -> dict[str, Any]:
    """
    Normalise raw text into order numbers and inspect ERP completion status.
    """
    if company_key:
        set_active_company(company_key)

    if not raw_text.strip():
        raise ValueError("No order numbers entered.")

    # Try Groq normalisation, fallback to regex
    ord_nums = []
    if config.GROQ_API_KEY:
        try:
            import bulk_complete
            ord_nums = bulk_complete._normalise_order_numbers(raw_text)
        except Exception:
            ord_nums = []

    if not ord_nums:
        # Regex extraction fallback
        raw_tokens = re.split(r"[,;\s\n]+", raw_text)
        seen = set()
        for t in raw_tokens:
            t = t.strip().upper()
            if not t:
                continue
            digits = re.sub(r"\D", "", t)
            if digits:
                ord_str = f"ORD-{int(digits):04d}"
                if ord_str not in seen:
                    seen.add(ord_str)
                    ord_nums.append(ord_str)

    if not ord_nums:
        return {"eligible": [], "already_completed": [], "not_found": []}

    token = erp.get_token()

    def _lookup_single(num: str):
        import bulk_complete
        order = bulk_complete._lookup_order(num, token)
        return num, order

    eligible = []
    already_completed = []
    not_found = []

    with ThreadPoolExecutor(max_workers=8) as pool:
        future_map = {pool.submit(_lookup_single, num): num for num in ord_nums}
        for future in as_completed(future_map):
            num, order = future.result()
            if not order:
                not_found.append(num)
            else:
                status = str(order.get("status") or "").lower()
                dealer = order.get("dealer", {})
                dname = dealer.get("name") if isinstance(dealer, dict) else str(dealer or "Unknown")
                cname = order.get("customer_name") or "—"
                item = {
                    "order_number": order.get("order_number", num),
                    "erp_id": order.get("id"),
                    "status": status,
                    "dealer": dname,
                    "customer_name": cname,
                    "total_price": order.get("total_price"),
                }
                if status == "completed":
                    already_completed.append(item)
                else:
                    eligible.append(item)

    return {
        "eligible": sorted(eligible, key=lambda x: x["order_number"]),
        "already_completed": sorted(already_completed, key=lambda x: x["order_number"]),
        "not_found": sorted(not_found),
        "total_scanned": len(ord_nums),
    }


def bulk_execute_complete(orders: list[dict[str, Any]], company_key: str | None = None) -> dict[str, Any]:
    """
    Execute PATCH to mark selected orders completed in ERP.
    """
    return mark_orders_completed_in_erp(orders, company_key)


# ---------------------------------------------------------------------------
# Mode 4: Price Checker (Cost vs Quote Analysis)
# ---------------------------------------------------------------------------

def run_price_check(raw_text: str, company_key: str | None = None) -> dict[str, Any]:
    """
    Look up order metrics (cost, quote, sqft, rates) in ERP and calculate margins.
    """
    if company_key:
        set_active_company(company_key)

    import price_checker
    ord_nums = price_checker.extract_order_numbers(raw_text)
    if not ord_nums:
        raise ValueError("No order numbers found in input.")

    token = erp.get_token()

    def _check_one(num: str):
        order, status = price_checker.lookup_order_in_erp(num, token)
        if order:
            m = price_checker.parse_order_metrics(order)
            cost = m["cost"]
            quote = m["quote"]
            markup = quote - cost
            markup_pct = (markup / cost * 100) if cost > 0 else 0.0
            return {
                "order_number": str(order.get("order_number") or num),
                "erp_id": order.get("id"),
                "status": "OK",
                "cost": round_price(cost),
                "quote": round_price(quote),
                "sqft": round_price(m["sqft"]),
                "rate_str": m["rate_str"],
                "markup": round_price(markup),
                "markup_pct": round_price(markup_pct),
            }
        return {
            "order_number": num,
            "status": status,
            "cost": 0.0,
            "quote": 0.0,
            "sqft": 0.0,
            "rate_str": "—",
            "markup": 0.0,
            "markup_pct": 0.0,
        }

    rows = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        future_map = {pool.submit(_check_one, num): num for num in ord_nums}
        for future in as_completed(future_map):
            rows.append(future.result())

    # Sort to preserve roughly original or order_number order
    rows.sort(key=lambda r: r["order_number"])

    total_cost = sum(r["cost"] for r in rows if r["status"] == "OK")
    total_quote = sum(r["quote"] for r in rows if r["status"] == "OK")
    found_count = sum(1 for r in rows if r["status"] == "OK")
    not_found_count = len(rows) - found_count
    total_markup = total_quote - total_cost
    avg_margin_pct = (total_markup / total_cost * 100) if total_cost > 0 else 0.0

    return {
        "rows": rows,
        "summary": {
            "total_orders": len(rows),
            "found_count": found_count,
            "not_found_count": not_found_count,
            "total_cost": round_price(total_cost),
            "total_quote": round_price(total_quote),
            "total_markup": round_price(total_markup),
            "avg_margin_pct": round_price(avg_margin_pct),
        },
    }


# ---------------------------------------------------------------------------
# Mode 5: Price Reviewer (Schedule A Recalculator)
# ---------------------------------------------------------------------------

def run_price_review(raw_text: str, company_key: str | None = None) -> dict[str, Any]:
    """
    Fetch order lines and recalculate expected prices against Schedule A.
    """
    if company_key:
        set_active_company(company_key)

    token = erp.get_token()
    tenant_id = config.ERP_TENANT_IDS[0] if config.ERP_TENANT_IDS else 1
    headers = {"Authorization": f"Bearer {token}", "X-Active-Tenant-Id": str(tenant_id)}

    ord_nums = [n.strip() for n in raw_text.replace(",", " ").split() if n.strip()]
    if not ord_nums:
        raise ValueError("No order numbers entered.")

    # 1. Fetch metadata
    sch_resp = _READONLY_SESSION.get(f"{config.ERP_BASE_URL}/admin/schedules", headers=headers).json().get("data", [])
    pt_resp = _READONLY_SESSION.get(f"{config.ERP_BASE_URL}/admin/product-types", headers=headers).json().get("data", [])
    pr_resp = _READONLY_SESSION.get(f"{config.ERP_BASE_URL}/admin/pricing-rates", headers=headers, params={"per_page": 1000}).json().get("data", [])

    schedule_a_id = next((s["id"] for s in sch_resp if str(s["name"]).strip().lower() == "schedule a"), None)
    pt_map = {str(pt["name"]).lower(): pt for pt in pt_resp}

    # 2. Fetch valid orders
    orders_data = []
    grand_orig = 0.0
    grand_sch_a = 0.0

    for ord_num in ord_nums:
        url = f"{config.ERP_BASE_URL}/admin/orders"
        resp = _READONLY_SESSION.get(url, headers=headers, params={"search": ord_num}, timeout=20)
        if resp.status_code != 200:
            continue

        data = resp.json().get("data", [])
        target = "".join(filter(str.isdigit, ord_num)).lstrip("0")
        target_order = None
        for o in data:
            o_num = "".join(filter(str.isdigit, str(o.get("order_number", "")))).lstrip("0")
            if o_num == target:
                target_order = o
                break

        if not target_order:
            orders_data.append({
                "order_number": ord_num,
                "found": False,
                "lines": [],
                "original_total": 0.0,
                "new_total": 0.0,
                "difference": 0.0,
            })
            continue

        lines = target_order.get("lines", [])
        order_original_total = to_float(target_order.get("total_price", 0))
        new_total_price = 0.0
        line_items = []

        for i, line in enumerate(lines, 1):
            product_name = str(line.get("product") or "Unknown").lower()
            fabric = "None"
            color = "None"
            for attr in line.get("attributes", []):
                fk = str(attr.get("field_key", "")).lower()
                if "fabric" in fk:
                    fabric = attr.get("value", "")
                elif fk in ("color", "colour"):
                    if color == "None":
                        color = attr.get("value", "")

            schedule = line.get("schedule", "Unknown")
            w = to_float(line.get("width", 0))
            h = to_float(line.get("height", 0))
            qty = int(to_float(line.get("quantity", 1)))
            current_price = to_float(line.get("total_price", 0))
            current_rate = to_float(line.get("rate_per_sqft", 0))

            pt_info = pt_map.get(product_name)
            min_sqft = to_float(pt_info.get("min_sqft", 10.0)) if pt_info else 10.0

            calculated_sqft_per_blind = max((w / 12) * (h / 12), min_sqft)
            total_calculated_sqft = round(calculated_sqft_per_blind * qty, 4)
            expected_base = round_price(total_calculated_sqft * current_rate)

            pt_id = pt_info["id"] if pt_info else None
            sch_a_rate = next((to_float(pr["rate_per_sqft"]) for pr in pr_resp if pr["product_type_id"] == pt_id and pr["schedule_id"] == schedule_a_id), None)
            if sch_a_rate is None:
                sch_a_rate = current_rate

            new_base = round_price(total_calculated_sqft * sch_a_rate)
            modifiers = current_price - expected_base
            new_line_price = round_price(new_base + modifiers)
            new_total_price += new_line_price

            line_items.append({
                "line_num": i,
                "dimensions": f"{w}x{h}",
                "qty": qty,
                "product": line.get("product", "Unknown"),
                "fabric": fabric,
                "color": color,
                "schedule": schedule,
                "current_price": round_price(current_price),
                "sch_a_price": round_price(new_line_price),
                "variance": round_price(new_line_price - current_price),
            })

        diff = round_price(new_total_price - order_original_total)
        grand_orig += order_original_total
        grand_sch_a += new_total_price

        orders_data.append({
            "order_number": target_order.get("order_number", ord_num),
            "found": True,
            "lines": line_items,
            "original_total": round_price(order_original_total),
            "new_total": round_price(new_total_price),
            "difference": diff,
        })

    return {
        "orders": orders_data,
        "summary": {
            "total_orders": len(orders_data),
            "grand_original_total": round_price(grand_orig),
            "grand_schedule_a_total": round_price(grand_sch_a),
            "grand_difference": round_price(grand_sch_a - grand_orig),
        },
    }


# ---------------------------------------------------------------------------
# Mode 6: Reworks Processor
# ---------------------------------------------------------------------------

def process_reworks_range(range_text: str, company_key: str | None = None) -> dict[str, Any]:
    """
    Fetch rework requests from ERP and calculate costs.
    """
    if company_key:
        set_active_company(company_key)

    import rework_processor
    req_ids = rework_processor.parse_range_input(range_text)
    if not req_ids:
        raise ValueError("No valid rework IDs parsed from input.")

    token = erp.get_token()
    tenant_id = config.ERP_TENANT_IDS[0] if config.ERP_TENANT_IDS else 1
    headers = {"Authorization": f"Bearer {token}", "X-Active-Tenant-Id": str(tenant_id)}

    rows = []
    orders_cache = {}

    for req_id in req_ids:
        url = f"{config.ERP_BASE_URL}/admin/requests/{req_id}"
        resp = _READONLY_SESSION.get(url, headers=headers)
        if resp.status_code != 200:
            continue

        data = resp.json()
        req = data.get("data", data)
        if req.get("type_slug") != "rework":
            continue

        field_data = req.get("field_data", {})
        rework_type = field_data.get("rework_type", "basic").lower()
        rework_lines = field_data.get("lines", [])

        linked = req.get("linked_entities", [])
        order_le = next((le for le in linked if le["entity"] == "order"), None)
        if not order_le:
            continue

        order_id = order_le["id"]
        order_number = order_le.get("meta", {}).get("order_number", "Unknown")
        dealer_name = req.get("raised_by", {}).get("name", "Unknown")

        if order_id not in orders_cache:
            o_url = f"{config.ERP_BASE_URL}/admin/orders/{order_id}"
            o_resp = _READONLY_SESSION.get(o_url, headers=headers)
            if o_resp.status_code == 200:
                orders_cache[order_id] = o_resp.json().get("data", o_resp.json())

        order_data = orders_cache.get(order_id)
        if order_data and order_data.get("dealer"):
            dealer_obj = order_data["dealer"]
            dealer_name = dealer_obj.get("name", dealer_name) if isinstance(dealer_obj, dict) else str(dealer_obj)

        lines_data = order_data.get("lines", []) if order_data else []

        for rl in rework_lines:
            line_id = rl.get("id")
            line_num = rl.get("line_number", "?")
            line_notes = rl.get("description", "")
            target_line = next((l for l in lines_data if l["id"] == line_id), None)

            original_cost = 0.0
            rework_cost = 15.00
            room = "Unknown"
            attributes_str = "No attributes found"

            if target_line:
                line_price = float(target_line.get("total_price", 0))
                original_cost = line_price
                if rework_type == "fabric":
                    rework_cost = round_price(line_price * 0.70)
                else:
                    rework_cost = 15.00

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

            rows.append({
                "rw_id": f"RW #{req_id}",
                "dealer": dealer_name,
                "order_number": order_number,
                "type": rework_type.title(),
                "line": f"Line {line_num}",
                "room": room,
                "line_notes": line_notes,
                "attributes": attributes_str,
                "original_cost": round_price(original_cost),
                "rework_cost": round_price(rework_cost),
            })

    total_rework_cost = sum(r["rework_cost"] for r in rows)
    total_original_cost = sum(r["original_cost"] for r in rows)

    return {
        "rows": rows,
        "summary": {
            "total_items": len(rows),
            "total_rework_cost": round_price(total_rework_cost),
            "total_original_cost": round_price(total_original_cost),
        },
    }


def send_reworks_email_report(rows: list[dict[str, Any]], company_key: str | None = None) -> dict[str, Any]:
    """
    Format reworks CSV and email to configured recipient.
    """
    if company_key:
        set_active_company(company_key)

    if not config.NOTIFY_EMAIL_APP_PASSWORD or not config.NOTIFY_EMAIL_FROM or not config.NOTIFY_EMAIL_TO:
        raise ValueError("Gmail notification settings (NOTIFY_EMAIL_*) not configured in .env.")

    if not rows:
        raise ValueError("No reworks to send.")

    # Generate CSV in memory
    csv_rows = [["RW#", "Dealer", "Order #", "Type", "Line", "Room", "Line Notes", "Attributes", "Original Cost", "Rework Cost"]]
    for r in rows:
        csv_rows.append([
            r.get("rw_id", ""),
            r.get("dealer", ""),
            r.get("order_number", ""),
            r.get("type", ""),
            r.get("line", ""),
            r.get("room", ""),
            r.get("line_notes", ""),
            r.get("attributes", ""),
            f"${r.get('original_cost', 0):.2f}",
            f"${r.get('rework_cost', 0):.2f}",
        ])

    csv_buffer = io.StringIO()
    writer = csv.writer(csv_buffer)
    writer.writerows(csv_rows)
    csv_content = csv_buffer.getvalue()

    date_str = datetime.now().strftime("%B %d, %Y")
    subject = f"[{config.ACTIVE_COMPANY_LABEL}] Reworks Summary — {date_str}"

    msg = MIMEMultipart()
    msg["Subject"] = subject
    msg["From"] = config.NOTIFY_EMAIL_FROM
    msg["To"] = config.NOTIFY_EMAIL_TO

    body = (
        f"Hello,\n\n"
        f"Attached is the reworks summary report for {len(rows)} processed rework line(s).\n\n"
        f"Best,\n"
        f"{config.ACTIVE_COMPANY_LABEL} Automation"
    )
    msg.attach(MIMEText(body, "plain"))

    attachment = MIMEBase("text", "csv")
    attachment.set_payload(csv_content.encode("utf-8"))
    encoders.encode_base64(attachment)
    attachment.add_header(
        "Content-Disposition",
        f"attachment; filename=reworks_summary_{datetime.now().strftime('%Y%m%d')}.csv"
    )
    msg.attach(attachment)

    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=15) as server:
        server.login(config.NOTIFY_EMAIL_FROM, config.NOTIFY_EMAIL_APP_PASSWORD)
        server.sendmail(config.NOTIFY_EMAIL_FROM, config.NOTIFY_EMAIL_TO, msg.as_string())

    return {
        "success": True,
        "recipient": config.NOTIFY_EMAIL_TO,
        "count": len(rows),
    }


# ---------------------------------------------------------------------------
# Credentials & Environment Management
# ---------------------------------------------------------------------------

def get_credentials_info() -> dict[str, Any]:
    """Retrieve all environment and API credentials for inspection."""
    from dotenv import load_dotenv
    load_dotenv(override=True)

    groq_key = os.getenv("GROQ_API_KEY", "")
    creds_path = _PROJECT_ROOT / "credentials.json"
    
    sheets_info: dict[str, Any] = {
        "exists": creds_path.exists(),
        "file_name": creds_path.name,
        "type": None,
        "project_id": None,
        "client_email": None,
        "tab_name": config.SHEET_TAB,
        "columns": f"{config.FIRST_COLUMN}:{chr(ord(config.FIRST_COLUMN) + config.WRITE_COLUMNS - 1)}",
        "dealers_count": len(config.DEALER_SHEETS),
    }

    if creds_path.exists():
        try:
            with open(creds_path, "r", encoding="utf-8") as f:
                cdata = json.load(f)
                sheets_info["type"] = cdata.get("type", "service_account")
                sheets_info["project_id"] = cdata.get("project_id", "")
                sheets_info["client_email"] = cdata.get("client_email", "")
        except Exception as e:
            sheets_info["error"] = str(e)

    # ERP info for all 3 companies
    erp_info = {}
    for ckey, ccfg in config.COMPANY.items():
        if ckey == "speedy":
            email = os.getenv("ERP_EMAIL", "")
            password = os.getenv("ERP_PASSWORD", "")
        elif ckey == "inspira":
            email = os.getenv("INSPIRA_ERP_EMAIL", "")
            password = os.getenv("INSPIRA_ERP_PASSWORD", "")
        elif ckey == "houston":
            email = os.getenv("HOUSTON_ERP_EMAIL", "")
            password = os.getenv("HOUSTON_ERP_PASSWORD", "")
        else:
            email = ""
            password = ""

        token_key = ccfg["token_key"]
        expires_key = ccfg["token_expires_key"]
        token = os.getenv(token_key, "")
        expires = os.getenv(expires_key, "")
        is_valid = erp._is_token_valid(token, expires)

        erp_info[ckey] = {
            "label": ccfg["label"],
            "base_url": ccfg["base_url"],
            "tenant_ids": ccfg["tenant_ids"],
            "email": email,
            "has_password": bool(password),
            "password_raw": password,
            "has_token": bool(token),
            "token_masked": (token[:12] + "..." + token[-6:]) if len(token) > 18 else token,
            "token_raw": token,
            "expires_at": expires,
            "is_valid": is_valid,
        }

    email_info = {
        "from": os.getenv("NOTIFY_EMAIL_FROM", ""),
        "to": os.getenv("NOTIFY_EMAIL_TO", ""),
        "has_app_password": bool(os.getenv("NOTIFY_EMAIL_APP_PASSWORD", "")),
        "app_password_raw": os.getenv("NOTIFY_EMAIL_APP_PASSWORD", ""),
    }

    callmebot_info = {
        "phone": os.getenv("CALLMEBOT_PHONE", ""),
        "has_api_key": bool(os.getenv("CALLMEBOT_API_KEY", "")),
        "api_key_raw": os.getenv("CALLMEBOT_API_KEY", ""),
    }

    env_content = ""
    if _ENV_PATH.exists():
        try:
            env_content = _ENV_PATH.read_text(encoding="utf-8")
        except Exception:
            pass

    return {
        "groq": {
            "api_key_raw": groq_key,
            "api_key_masked": (groq_key[:9] + "••••••••" + groq_key[-4:]) if len(groq_key) > 13 else groq_key,
            "model": config.GROQ_MODEL,
            "configured": bool(groq_key),
        },
        "sheets": sheets_info,
        "erp": erp_info,
        "email": email_info,
        "whatsapp": callmebot_info,
        "env_content": env_content,
    }


def save_env_file(content: str) -> dict[str, Any]:
    """Save content to .env file and reload runtime configuration."""
    _ENV_PATH.write_text(content, encoding="utf-8")
    from dotenv import load_dotenv
    load_dotenv(override=True)

    # Update runtime config module
    config.GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
    config.NOTIFY_EMAIL_FROM = os.getenv("NOTIFY_EMAIL_FROM", "")
    config.NOTIFY_EMAIL_TO = os.getenv("NOTIFY_EMAIL_TO", "")
    config.NOTIFY_EMAIL_APP_PASSWORD = os.getenv("NOTIFY_EMAIL_APP_PASSWORD", "")
    config.SPEEDY_ERP_EMAIL = os.getenv("ERP_EMAIL", "")
    config.SPEEDY_ERP_PASSWORD = os.getenv("ERP_PASSWORD", "")
    config.INSPIRA_ERP_EMAIL = os.getenv("INSPIRA_ERP_EMAIL", "")
    config.INSPIRA_ERP_PASSWORD = os.getenv("INSPIRA_ERP_PASSWORD", "")
    config.HOUSTON_ERP_EMAIL = os.getenv("HOUSTON_ERP_EMAIL", "")
    config.HOUSTON_ERP_PASSWORD = os.getenv("HOUSTON_ERP_PASSWORD", "")

    # Re-init parser client with new key
    if config.GROQ_API_KEY:
        try:
            import parser as order_parser
            from groq import Groq
            order_parser._client = Groq(api_key=config.GROQ_API_KEY)
        except Exception:
            pass

    return {"success": True}


def test_groq_connection(api_key: str | None = None) -> dict[str, Any]:
    """Test Groq API connection and return latency."""
    import time
    from groq import Groq
    key = api_key or config.GROQ_API_KEY or os.getenv("GROQ_API_KEY", "")
    if not key:
        raise ValueError("GROQ_API_KEY is not set.")

    t0 = time.time()
    client = Groq(api_key=key)
    resp = client.chat.completions.create(
        model=config.GROQ_MODEL,
        messages=[{"role": "user", "content": "respond with OK"}],
        max_tokens=5,
    )
    latency_ms = int((time.time() - t0) * 1000)
    return {
        "success": True,
        "latency_ms": latency_ms,
        "reply": (resp.choices[0].message.content or "").strip(),
        "model": config.GROQ_MODEL,
    }


def test_sheets_connection() -> dict[str, Any]:
    """Test Google Sheets Service Account API connection."""
    import sheets
    service = sheets._get_service()
    first_dealer = next(iter(config.DEALER_SHEETS.keys()), None)
    first_id = config.DEALER_SHEETS.get(first_dealer)
    if not first_id:
        raise ValueError("No dealer sheet ID configured.")

    meta = service.spreadsheets().get(spreadsheetId=first_id, fields="properties.title").execute()
    sheet_title = meta.get("properties", {}).get("title", "Unknown")

    return {
        "success": True,
        "dealer": first_dealer,
        "spreadsheet_id": first_id,
        "spreadsheet_title": sheet_title,
    }


def test_erp_connection(company_key: str) -> dict[str, Any]:
    """Authenticate with ERP for company_key and refresh Bearer token."""
    set_active_company(company_key)
    fresh_token = erp._login()
    _, expires = erp._read_cached_token()
    return {
        "success": True,
        "company": config.ACTIVE_COMPANY_LABEL,
        "token_preview": (fresh_token[:12] + "..." + fresh_token[-6:]) if len(fresh_token) > 18 else fresh_token,
        "expires_at": expires,
    }


def test_email_connection(to_email: str | None = None) -> dict[str, Any]:
    """Send a test email notification via Gmail SMTP."""
    from_addr = config.NOTIFY_EMAIL_FROM or os.getenv("NOTIFY_EMAIL_FROM", "")
    to_addr = to_email or config.NOTIFY_EMAIL_TO or os.getenv("NOTIFY_EMAIL_TO", "")
    app_pwd = config.NOTIFY_EMAIL_APP_PASSWORD or os.getenv("NOTIFY_EMAIL_APP_PASSWORD", "")

    if not from_addr or not to_addr or not app_pwd:
        raise ValueError("Gmail notification settings (NOTIFY_EMAIL_*) not fully set in .env.")

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    msg = MIMEText(
        f"Hello,\n\nThis is a test notification from Dealer Order Automation.\n\nTime: {now_str}\nActive Company: {config.ACTIVE_COMPANY_LABEL}\n\nYour Gmail App Password and notification credentials are functioning correctly!",
        "plain"
    )
    msg["Subject"] = f"[{config.ACTIVE_COMPANY_LABEL}] Credentials Test Notification"
    msg["From"] = from_addr
    msg["To"] = to_addr

    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=15) as server:
        server.login(from_addr, app_pwd)
        server.sendmail(from_addr, to_addr, msg.as_string())

    return {
        "success": True,
        "recipient": to_addr,
        "sender": from_addr,
    }


# ---------------------------------------------------------------------------
# Dealer Configuration APIs
# ---------------------------------------------------------------------------

def get_dealers_list(company_filter: str | None = None) -> list[dict[str, Any]]:
    """Return all configured dealers across companies with metadata and aliases."""
    import dealers_manager
    return dealers_manager.get_all_dealers_flat(company_filter=company_filter)


def save_dealer_config(
    company_key: str,
    dealer_name: str,
    sheet_url_or_id: str,
    aliases: list[str] | str,
    original_name: str | None = None,
    sheet_type: str | None = None,
) -> dict[str, Any]:
    """Create or update a dealer record."""
    from speedy_blinds import dealers_manager
    return dealers_manager.add_or_update_dealer(
        company_key=company_key,
        dealer_name=dealer_name,
        sheet_url_or_id=sheet_url_or_id,
        aliases=aliases,
        original_name=original_name,
        sheet_type=sheet_type,
    )


def delete_dealer_config(company_key: str, dealer_name: str) -> dict[str, Any]:
    """Delete a dealer record."""
    import dealers_manager
    return dealers_manager.delete_dealer(company_key=company_key, dealer_name=dealer_name)


def test_dealer_sheet_access(sheet_url_or_id: str) -> dict[str, Any]:
    """Test if Google Sheets Service Account can access the specified sheet."""
    import dealers_manager
    import sheets
    sheet_id = dealers_manager.extract_spreadsheet_id(sheet_url_or_id)
    if not sheet_id:
        raise ValueError("Please provide a valid Google Sheet URL or Spreadsheet ID.")

    service = sheets._get_service()
    meta = service.spreadsheets().get(spreadsheetId=sheet_id, fields="properties.title").execute()
    title = meta.get("properties", {}).get("title", "Untitled Spreadsheet")
    return {
        "success": True,
        "sheet_id": sheet_id,
        "title": title,
    }


