"""
bulk_complete.py — Mode 3: Bulk Mark Orders Completed
======================================================
Flow:
  1. User pastes any raw order number text (e.g. "ON 121 122 ORD-0123 124")
  2. Groq normalises them to ORD-XXXX format
  3. ERP GET fetches each order's id + current status
  4. Already-completed orders are shown and skipped
  5. User types 'yes' to PATCH remaining ones → completed
  6. Every PATCH logged to erp_updates.log
"""

from __future__ import annotations

import json
import re
import sys

from groq import Groq
from tabulate import tabulate

import erp
import config
from config import GROQ_API_KEY, GROQ_MODEL


# ── ANSI colours ─────────────────────────────────────────────────────────────
def _c(code: str, text: str) -> str:
    if sys.stdout.isatty():
        return f"\033[{code}m{text}\033[0m"
    return text

GREEN  = lambda t: _c("32", t)
YELLOW = lambda t: _c("33", t)
RED    = lambda t: _c("31", t)
BOLD   = lambda t: _c("1",  t)
CYAN   = lambda t: _c("36", t)


# ── Groq normalisation ────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """
You are an order-number extraction assistant.
Extract EVERY order number from the user's text and normalise each to ORD-XXXX format.

Rules:
- A number like "121", "ON121", "ON 121", "ORD-121", "ORD-0121" → "ORD-0121"
- Pad the numeric part to at least 4 digits with leading zeros.
- Return ONLY a valid JSON array of strings, e.g. ["ORD-0121","ORD-0122"]
- No markdown, no explanation, nothing else.
""".strip()


def _normalise_order_numbers(raw_text: str) -> list[str]:
    """Send raw text to Groq; returns list of normalised ORD-XXXX strings."""
    client = Groq(api_key=GROQ_API_KEY)
    resp = client.chat.completions.create(
        model=GROQ_MODEL,
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user",   "content": raw_text},
        ],
        temperature=0,
        max_tokens=512,
    )
    reply = (resp.choices[0].message.content or "").strip()

    # Strip markdown fences if present
    reply = re.sub(r"```(?:json)?", "", reply).strip()

    # Find JSON array
    match = re.search(r"\[.*\]", reply, re.DOTALL)
    if not match:
        print(RED(f"  ✗ Groq returned unexpected output: {reply[:200]}"))
        return []

    try:
        numbers = json.loads(match.group())
    except json.JSONDecodeError as e:
        print(RED(f"  ✗ JSON parse error: {e}"))
        return []

    # Deduplicate preserving order
    seen: set[str] = set()
    result: list[str] = []
    for n in numbers:
        n = str(n).strip().upper()
        if n and n not in seen:
            seen.add(n)
            result.append(n)

    return result


# ── ERP single-order lookup ───────────────────────────────────────────────────

def _lookup_order(order_number: str, token: str) -> dict | None:
    """
    GET /admin/orders?search=<order_number> and return the matching order dict,
    or None if not found.
    """
    import config as _cfg
    tid = _cfg.ERP_TENANT_IDS[0] if _cfg.ERP_TENANT_IDS else 1
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Active-Tenant-Id": str(tid),
    }
    try:
        resp = erp._SESSION.get(
            f"{_cfg.ERP_BASE_URL}/admin/orders",
            headers=headers,
            params={"search": order_number, "per_page": 20},
            timeout=20,
        )
    except Exception as exc:
        print(RED(f"  ✗ Network error looking up {order_number}: {exc}"))
        return None

    if resp.status_code != 200:
        print(RED(f"  ✗ ERP returned {resp.status_code} for {order_number}"))
        return None

    def _norm(n: str | None) -> str:
        digits = re.sub(r"\D", "", str(n or ""))
        return digits.lstrip("0") or "0"

    target = _norm(order_number)
    for o in resp.json().get("data", []):
        if _norm(o.get("order_number", "")) == target:
            return o

    return None


# ── Main entry point ──────────────────────────────────────────────────────────

def run_bulk_complete() -> None:
    print(BOLD("\n═══ Bulk Mark Orders Completed ══════════════════════"))
    print(YELLOW(f"  Company : {config.ACTIVE_COMPANY_LABEL}"))
    print(YELLOW(f"  ERP URL : {config.ERP_BASE_URL}\n"))

    # ── 1. Read raw order numbers ─────────────────────────────────────────────
    print(YELLOW("Paste order numbers below (any format). Press Enter twice when done:"))
    lines: list[str] = []
    try:
        while True:
            line = input()
            if line == "" and lines and lines[-1] == "":
                break
            lines.append(line)
    except EOFError:
        pass
    raw_text = "\n".join(lines).strip()

    if not raw_text:
        print(RED("  No input provided. Exiting."))
        return

    # ── 2. Normalise via Groq ─────────────────────────────────────────────────
    print(YELLOW("\n  ⏳ Sending to Groq for order number normalisation..."))
    normalised = _normalise_order_numbers(raw_text)

    if not normalised:
        print(RED("  ✗ No valid order numbers found."))
        return

    print(GREEN(f"  ✓ {len(normalised)} unique order number(s) extracted: {', '.join(normalised)}"))

    # ── 3. ERP lookup for each order ─────────────────────────────────────────
    print(YELLOW(f"\n  ⏳ Fetching {len(normalised)} order(s) from ERP..."))
    token = erp.get_token()

    found:   list[dict] = []   # (order dict from ERP)
    missing: list[str]  = []   # order numbers not found in ERP

    for onum in normalised:
        order = _lookup_order(onum, token)
        if order:
            order["_input_number"] = onum   # keep the normalised number handy
            found.append(order)
        else:
            missing.append(onum)
            print(YELLOW(f"  ⚠  {onum}  not found in ERP"))

    if not found:
        print(RED("  ✗ None of the orders were found in ERP. Exiting."))
        return

    # ── 4. Split: already completed vs pending ────────────────────────────────
    already_completed = [o for o in found if str(o.get("status") or "").lower() == "completed"]
    pending           = [o for o in found if str(o.get("status") or "").lower() != "completed"]

    # ── 5. Show summary table ─────────────────────────────────────────────────
    print()
    rows = []
    for o in already_completed:
        rows.append([
            o.get("order_number", "?"),
            o.get("id", "?"),
            CYAN("completed"),
            o.get("customer_name", "?"),
            f"${float(o.get('total_price') or 0):,.2f}",
            "✓ SKIP",
        ])
    for o in pending:
        rows.append([
            o.get("order_number", "?"),
            o.get("id", "?"),
            YELLOW(str(o.get("status", "?"))),
            o.get("customer_name", "?"),
            f"${float(o.get('total_price') or 0):,.2f}",
            GREEN("→ MARK"),
        ])
    for onum in missing:
        rows.append([onum, "—", RED("NOT FOUND"), "—", "—", RED("SKIP")])

    print(tabulate(
        rows,
        headers=["Order #", "ERP ID", "Current Status", "Customer", "Price", "Action"],
        tablefmt="rounded_outline",
    ))
    print()

    if not pending:
        print(GREEN("  ✓ All orders are already completed — nothing to update."))
        return

    # ── 6. Confirmation ───────────────────────────────────────────────────────
    print(BOLD("─" * 55))
    print(YELLOW(f"  ⚠️  {len(pending)} order(s) will be marked COMPLETED in the ERP."))
    if already_completed:
        print(YELLOW(f"  {len(already_completed)} already completed (skipped)."))
    if missing:
        print(YELLOW(f"  {len(missing)} not found in ERP (skipped)."))
    print(BOLD("─" * 55))
    ans = input(f"\n  Type  yes  to proceed, or press Enter to abort: ").strip().lower()

    if ans != "yes":
        print(YELLOW("  Aborted — no changes made."))
        return

    # ── 7. PATCH one by one ───────────────────────────────────────────────────
    print()
    ok   = 0
    fail = 0

    for o in pending:
        erp_id    = o.get("id")
        order_num = o.get("order_number", o.get("_input_number", "?"))

        if not isinstance(erp_id, int) or erp_id <= 0:
            print(RED(f"  ✗ {order_num}  skipped — invalid ERP id ({erp_id!r})"))
            fail += 1
            continue

        success = erp.mark_order_completed(erp_id, order_num, token)
        if success:
            print(GREEN(f"  ✓ {order_num:12s}  marked completed  (id={erp_id})"))
            ok += 1
        else:
            print(RED(f"  ✗ {order_num:12s}  FAILED            (id={erp_id}) — see erp_updates.log"))
            fail += 1

    # ── 8. Final tally ────────────────────────────────────────────────────────
    print()
    print(BOLD("─── Bulk Complete Summary ──────────────────────────"))
    print(GREEN(f"  ✓ Marked completed   : {ok}"))
    if fail:
        print(RED(f"  ✗ Failed             : {fail}"))
    if already_completed:
        print(CYAN(f"  ↷ Already completed  : {len(already_completed)} (skipped)"))
    if missing:
        print(YELLOW(f"  ? Not found in ERP   : {len(missing)} (skipped)"))
    print(BOLD("───────────────────────────────────────────────────"))
    print(YELLOW("  All PATCH attempts logged to erp_updates.log\n"))
