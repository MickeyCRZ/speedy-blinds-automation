"""
mir.py — MIR (Mobile Installer Report) Calculator  [v2]
==========================================================
Dual-ledger model:
  • Installer Payable  — what WE owe each installer
  • Dealer Receivable  — what each DEALER owes US

Flow:
  1. Multi-installer batch entry loop (menu driven)
  2. Groq extracts order# + job type + big_ladder flag from raw freeform text
  3. Preview/confirm/edit table shown per batch before committing
  4. Parallel ERP lookup for all unique order numbers across all batches
  5. Calculate dual ledger
  6. Print two terminal tables (Installer Payments | Dealer Charges)
  7. Send single combined HTML email with full per-order breakdowns

Rates:
  ┌─────────────────────┬──────────────┬────────────────┐
  │ Job Type            │ Dealer Charge│ Installer Pay  │
  ├─────────────────────┼──────────────┼────────────────┤
  │ Install             │ $4.00/blind  │ $3.00/blind    │
  │ Uninstall + Install │ $8.00/blind  │ $3.00/blind    │
  │ Uninstall Only      │ $4.00/blind  │ $3.00/blind    │
  │ Big Ladder (modifier│ +$25.00/order│ +$20.00/order  │
  └─────────────────────┴──────────────┴────────────────┘
"""

from __future__ import annotations

import re
import sys
import json
import smtplib
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

from groq import Groq
from tabulate import tabulate

import config
import erp

# ── Styling helpers ──────────────────────────────────────────────────────────
def _c(code: str, text: str) -> str:
    if sys.stdout.isatty():
        return f"\033[{code}m{text}\033[0m"
    return text

GREEN  = lambda t: _c("32", t)
YELLOW = lambda t: _c("33", t)
RED    = lambda t: _c("31", t)
BOLD   = lambda t: _c("1",  t)
CYAN   = lambda t: _c("36", t)
DIM    = lambda t: _c("2",  t)
MAGENTA = lambda t: _c("35", t)

# ── Rate constants ───────────────────────────────────────────────────────────
# What we charge dealers per blind
DEALER_RATE_BLIND    = 4.00
DEALER_BIG_LADDER    = 25.00   # flat per order (on top of blind rate)

# What we pay installers per blind
INSTALLER_RATE_INSTALL   = 3.00
INSTALLER_RATE_UNINSTALL = 2.00
INSTALLER_RATE_REWORK    = 3.00
INSTALLER_BIG_LADDER     = 20.00   # flat per order (on top of blind pay)

# ── Rework ID counter (session-scoped) ───────────────────────────────────────
_rework_counter = 0

def _next_rework_id() -> str:
    global _rework_counter
    _rework_counter += 1
    return f"REWORK-{_rework_counter:03d}"


# ── 1. Groq structured extraction ────────────────────────────────────────────

def parse_order_jobs(raw_text: str) -> list[dict]:
    """
    Send free-form text to Groq and extract structured job data.

    Returns a list of normalised dicts:
        [
            {
                "order_number":        "ORD-0121",     # or "REWORK-001" for rework
                "installs_txt":        9,              # from text, else None
                "uninstalls_txt":      5,              # from text, else None
                "reworks_txt":         0,              # from text, else None
                "dealer_text":         "Phil",         # str if dealer found in text, else None
                "big_ladder":          False,
                "assumed":             False,          # True if inferred
            },
            ...
        ]

    Rework orders without an order number get a synthetic REWORK-XXX ID.
    `blind_count_text` is extracted from text (e.g. '5 blinds') and shown
    alongside the ERP count in the preview — user picks which to use in edit mode.
    """
    client = Groq(api_key=config.GROQ_API_KEY)
    dealers = list({v for v in config.DEALER_ALIASES.values()})

    system_prompt = (
        "You are a job-sheet parser for a window blind installation company.\n\n"
        "TASK: Extract every job entry from the text. Instead of job types, extract COUNT of actions.\n\n"
        "WHAT IS A VALID ORDER NUMBER:\n"
        "- A number that identifies a specific job/order, typically 3 or more digits\n"
        "- Usually written as: ON 246, ORD-0246, order 246, #246, or just 246 (when clearly referring to a job)\n"
        "- Common prefixes: ON, ORD, order, #\n"
        "- Must be at least 3 digits. Single or two-digit numbers (e.g. 5, 10, 46) are quantities, NOT order numbers\n"
        "- Dates (e.g. 9 June, June 10) are NOT order numbers — ignore them\n\n"
        "Action rules:\n"
        "- installs: The number of blinds installed for this order.\n"
        "- uninstalls: The number of old blinds removed/uninstalled/taken down.\n"
        "- reworks: The number of blinds reworked, fixed, or corrected.\n"
        "- If an order mentions multiple actions (e.g. '9 installed, 5 removed'), extract BOTH counts for that SAME order.\n"
        "- If the text says 'remove and install', then BOTH installs and uninstalls get the count.\n"
        "- If the text just says a blind count (e.g., 'ON 246 - 5 blinds') with no specific verb, assume they are installs.\n\n"
        "Dealer rules:\n"
        "- dealer: extract the dealer name if explicitly stated next to an order "
        "(e.g. 'ON 246 VT Thomas' → 'VT Thomas', 'rework Phil' → 'Phil')\n"
        "- dealer: null if no dealer is mentioned for that entry\n"
        "- MUST be one of the known dealers or a close alias. Known dealers: "
        f"{', '.join(sorted(dealers))}\n\n"
        "Big ladder rules:\n"
        "- big_ladder = true if the text mentions big ladder, tall ladder, extra ladder, "
        "high ceiling, or ladder surcharge for that specific entry\n\n"
        "Rework without order number:\n"
        "- If a rework entry has no order number (e.g. 'rework installed 2 blinds'), "
        'set order to empty string "" and reworks to 2.\n\n'
        "Special cases:\n"
        "- If an action count is not mentioned, use null.\n"
        "- If the same order number is mentioned multiple times, merge them into ONE single JSON object with the TOTAL counts for installs, uninstalls, reworks.\n\n"
        "Return ONLY a valid JSON array. No markdown fences, no explanation.\n\n"
        "Output format (exactly):\n"
        '[{"order": "<raw order ref or empty>", "installs": <int|null>, "uninstalls": <int|null>, '
        '"reworks": <int|null>, "dealer": "<string or null>", "big_ladder": <true|false>, "assumed": <true|false>}, ...]'
    )

    try:
        response = client.chat.completions.create(
            model=config.GROQ_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": raw_text},
            ],
            temperature=0,
            max_tokens=1024,
        )
        reply = response.choices[0].message.content or ""
    except Exception as exc:
        print(RED(f"  ✗ Groq API error: {exc}"))
        return []

    # Strip markdown fences if present
    reply = re.sub(r"```(?:json)?", "", reply).strip()
    match = re.search(r"\[.*\]", reply, re.DOTALL)
    if not match:
        return []

    try:
        raw_list: list[dict] = json.loads(match.group())
    except json.JSONDecodeError:
        return []

    # Normalise and validate each entry
    result: list[dict] = []
    seen: dict[str, int] = {}  # order_number → index in result

    for item in raw_list:
        if not isinstance(item, dict):
            continue

        raw_order      = str(item.get("order", "")).strip()
        big_ladder     = bool(item.get("big_ladder", False))
        assumed        = bool(item.get("assumed", False))
        
        def _get_int(key: str) -> int | None:
            v = item.get(key)
            return int(v) if isinstance(v, (int, float)) and v > 0 else None

        installs_txt   = _get_int("installs")
        uninstalls_txt = _get_int("uninstalls")
        reworks_txt    = _get_int("reworks")

        # Resolve dealer text if present
        raw_dealer = str(item.get("dealer", "")).strip()
        dealer_text = config.DEALER_ALIASES.get(raw_dealer.lower(), raw_dealer) if raw_dealer else None
        if dealer_text and dealer_text.lower() in ("null", "none", "unknown", ""):
            dealer_text = None

        is_rework = (reworks_txt is not None and reworks_txt > 0)

        # Determine order number
        digits = re.sub(r"\D", "", raw_order)

        if is_rework and (not digits or len(digits) < 3):
            # Rework with no order number → synthetic REWORK-XXX ID
            order_number = _next_rework_id()
        elif not digits or len(digits) < 3:
            # No valid order number and not a rework → skip
            continue
        else:
            order_number = f"ORD-{int(digits):04d}"

        if order_number in seen:
            idx      = seen[order_number]
            existing = result[idx]
            if big_ladder:
                existing["big_ladder"] = True
            if not assumed:
                existing["assumed"] = False
            
            def _merge_counts(key: str, new_val: int | None):
                if new_val:
                    existing[key] = max(existing.get(key) or 0, new_val)
                    
            _merge_counts("installs_txt", installs_txt)
            _merge_counts("uninstalls_txt", uninstalls_txt)
            _merge_counts("reworks_txt", reworks_txt)

            if dealer_text and not existing.get("dealer_text"):
                existing["dealer_text"] = dealer_text
            continue

        seen[order_number] = len(result)
        result.append({
            "order_number":     order_number,
            "installs_txt":     installs_txt,
            "uninstalls_txt":   uninstalls_txt,
            "reworks_txt":      reworks_txt,
            "big_ladder":       big_ladder,
            "assumed":          assumed,
            "dealer_text":      dealer_text,
        })

    return result


# ── 2. Preview / Edit / Confirm ──────────────────────────────────────────────

def _print_batch_preview(installer: str, orders: list[dict]) -> None:
    """Print a formatted table of parsed orders for one installer."""
    rows = []
    for i, o in enumerate(orders, 1):
        ladder_str = "🔺 +$20" if o.get("big_ladder") else DIM("—")
        assumed    = " ⚠ assumed" if o.get("assumed") else ""
        is_rework  = (o.get("reworks_txt") or 0) > 0

        # Dealer: manual override → text from Groq → customer-name match → ERP pre-fetch → placeholder
        erp_preview = o.get("erp_preview") or {}
        dealer_cust_match = o.get("dealer_cust_match")
        dealer = (
            o.get("dealer_override")
            or o.get("dealer_text")
            or dealer_cust_match
            or erp_preview.get("dealer_name")
            or DIM("—")
        )
        if o.get("dealer_override"):
            dealer = BOLD(dealer) + " ✎"
        elif dealer_cust_match and not o.get("dealer_text"):
            dealer = YELLOW(dealer) + " ⚑"  # auto-assigned from customer name, needs review

        # Customer name from ERP — flag if it matches a known dealer
        cust_name = erp_preview.get("customer_name")
        dealer_match = _fuzzy_match(cust_name, list(config.DEALER_ALIASES.values())) if cust_name else None
        if dealer_match:
            resolved_dealer = _fuzzy_match(
                o.get("dealer_override") or o.get("dealer_text") or erp_preview.get("dealer_name") or "",
                list(config.DEALER_ALIASES.values())
            )
            if resolved_dealer == dealer_match:
                # Customer IS the dealer — self-install flag
                cust_str = RED(f"⚑ {cust_name}")
            else:
                # Customer name looks like a different dealer — possible wrong dealer
                cust_str = YELLOW(f"⚠ {cust_name}")
        elif cust_name:
            cust_str = DIM(cust_name)
        else:
            cust_str = DIM("—")

        # Installs: show text count, ERP count, discrepancy warning
        erp_count = (o.get("erp_preview") or {}).get("blind_count")
        txt_installs = o.get("installs_txt")
        over_installs = o.get("installs_override")

        if over_installs is not None:
            installs_str = BOLD(f"{over_installs}") + " ✎"
        elif is_rework and erp_count is None:
            # Rework with no ERP — fallback to text
            installs_str = MAGENTA(f"Txt:{txt_installs}") if txt_installs is not None else "0"
        elif txt_installs is not None and erp_count is not None and txt_installs != erp_count:
            # Discrepancy between text and ERP
            installs_str = YELLOW(f"ERP:{erp_count}") + "/" + MAGENTA(f"Txt:{txt_installs}")
        elif txt_installs is not None and erp_count is None:
            installs_str = MAGENTA(f"Txt:{txt_installs}")
        elif erp_count is not None:
            installs_str = str(erp_count)
        else:
            installs_str = "0"

        # Uninstalls
        uninstalls_txt = o.get("uninstalls_txt")
        over_uninstalls = o.get("uninstalls_override")
        if over_uninstalls is not None:
            uninstalls_str = BOLD(f"{over_uninstalls}") + " ✎"
        else:
            uninstalls_str = str(uninstalls_txt) if uninstalls_txt else "0"

        # Reworks
        reworks_txt = o.get("reworks_txt")
        over_reworks = o.get("reworks_override")
        if over_reworks is not None:
            reworks_str = BOLD(f"{over_reworks}") + " ✎"
        else:
            reworks_str = str(reworks_txt) if reworks_txt else "0"

        order_display = MAGENTA(o["order_number"]) if is_rework else o["order_number"]
        rows.append([i, order_display, dealer, cust_str, installs_str, uninstalls_str, reworks_str, ladder_str])

    print()
    print(BOLD(CYAN(f"  ┌─ {installer} — Groq Parsed Orders {'─' * 26}")))
    print(tabulate(
        rows,
        headers=["#", "Order", "Dealer", "Customer", "Installs", "Uninstalls", "Reworks", "Big Ladder"],
        tablefmt="rounded_outline",
        colalign=("center", "left", "left", "left", "center", "center", "center", "left"),
    ))
    if any(o.get("is_rework") for o in orders) or any(o.get("reworks_txt") for o in orders):
        print(DIM("    " + MAGENTA("Magenta") + " = Rework (synthetic ID, no ERP lookup)"))
    if any(
        (o.get("erp_preview") or {}).get("blind_count") is not None
        and o.get("installs_txt") is not None
        and (o.get("erp_preview") or {}).get("blind_count") != o.get("installs_txt")
        for o in orders
    ):
        print(YELLOW("    ⚠  ERP:N/Txt:M = install count discrepancy — use [E]dit to override."))
    any_flagged_cust = any(
        _fuzzy_match((o.get("erp_preview") or {}).get("customer_name", ""), list(config.DEALER_ALIASES.values()))
        for o in orders
        if (o.get("erp_preview") or {}).get("customer_name")
    )
    if any_flagged_cust:
        print(YELLOW("    ⚠  Customer") + " = customer name matches a dealer —  " +
              RED("⚑ Red") + " = self-install (same dealer), " +
              YELLOW("⚠  Yellow") + " = different dealer.")
    print()



def _edit_batch(installer: str, orders: list[dict]) -> list[dict]:
    """Interactive row-level edit mode — lets user correct any entry."""
    while True:
        _print_batch_preview(installer, orders)
        raw = input(BOLD("  Row # to edit (0 or Enter to finish): ")).strip()
        if raw in ("0", ""):
            break
        try:
            idx = int(raw) - 1
        except ValueError:
            print(RED("  Please enter a number."))
            continue
        if not (0 <= idx < len(orders)):
            print(RED(f"  Invalid row. Enter 1–{len(orders)}."))
            continue

        order = orders[idx]
        current_dealer = (
            order.get("dealer_override")
            or order.get("dealer_text")
            or order.get("dealer_cust_match")
            or (order.get("erp_preview") or {}).get("dealer_name")
            or "unknown"
        )
        erp_count = (order.get("erp_preview") or {}).get("blind_count")
        txt_count = order.get("blind_count_text")
        cur_count = order.get("blind_count_override") or erp_count or txt_count or "unknown"

        print(f"\n  Editing {BOLD(order['order_number'])}:")

        # ── Count Overrides ──────────────────────────────────────────────
        def _edit_count(label: str, erp_val: int | None, txt_val: int | None, over_val: int | None) -> int | None:
            parts = []
            if erp_val is not None: parts.append(f"ERP:{erp_val}")
            if txt_val is not None: parts.append(f"Txt:{txt_val}")
            info = " / ".join(parts) if parts else "0"
            cur = over_val if over_val is not None else (erp_val if erp_val is not None else txt_val or 0)
            print(f"    {label} (current: {CYAN(str(cur))} [{info}]):")
            new_val = input(f"    Override {label.lower()} (Enter to keep): ").strip()
            if new_val:
                try:
                    res = int(new_val)
                    print(GREEN(f"    ✓ {label} set to {res}"))
                    return res
                except ValueError:
                    print(RED(f"    Invalid number — {label.lower()} not changed."))
            return over_val

        erp_count = (order.get("erp_preview") or {}).get("blind_count")
        txt_installs = order.get("installs_txt")
        txt_uninstalls = order.get("uninstalls_txt")
        txt_reworks = order.get("reworks_txt")

        new_i = _edit_count("Installs", erp_count, txt_installs, order.get("installs_override"))
        if new_i is not None: order["installs_override"] = new_i
        
        new_u = _edit_count("Uninstalls", None, txt_uninstalls, order.get("uninstalls_override"))
        if new_u is not None: order["uninstalls_override"] = new_u
        
        new_r = _edit_count("Reworks", None, txt_reworks, order.get("reworks_override"))
        if new_r is not None: order["reworks_override"] = new_r

        # ── Dealer ──────────────────────────────────────────────────────
        print(f"    Dealer (current: {CYAN(current_dealer)}):")
        new_dealer = input("    New dealer name (Enter to keep): ").strip()
        if new_dealer:
            fuzzy = _fuzzy_match(new_dealer, list(config.DEALER_ALIASES.values()))
            if fuzzy:
                order["dealer_override"] = fuzzy
                print(GREEN(f"    ✓ Dealer set to '{fuzzy}' (matched from '{new_dealer}')"))
            else:
                order["dealer_override"] = new_dealer
                print(YELLOW(f"    ⚠ Dealer set to '{new_dealer}' (not found in known dealers list)"))

        # Clear assumed flag if they edited
        order["assumed"] = False

        # ── Big Ladder ────────────────────────────────────────────────────
        bl = input("    Big Ladder? [Y/N] (Enter to keep): ").strip().lower()
        if bl == "y":
            order["big_ladder"] = True
        elif bl == "n":
            order["big_ladder"] = False

        print(GREEN(f"  ✓ {order['order_number']} updated."))

    return orders


def confirm_batch(installer: str, orders: list[dict]) -> list[dict] | None:
    """
    Show preview and let user confirm, edit, re-paste, or discard.
    Returns confirmed order list, or None if discarded.
    """
    _print_batch_preview(installer, orders)

    while True:
        choice = input(BOLD(
            "  [Y] Confirm  [E] Edit entries  [X] Discard this batch: "
        )).strip().lower()
        if choice in ("y", "yes", ""):
            return orders
        elif choice in ("e", "edit"):
            orders = _edit_batch(installer, orders)
            # Re-print preview after edits
            _print_batch_preview(installer, orders)
        elif choice in ("x", "discard"):
            print(YELLOW(f"  Batch for {installer} discarded."))
            return None
        else:
            print(RED("  Please enter Y, E, or X."))


# ── 3. Fuzzy installer name matching ─────────────────────────────────────────

def _fuzzy_match(name: str, existing: list[str]) -> str | None:
    """
    Return an existing name that looks similar to `name`, or None.
    Priority:
      1. Exact alias key lookup in DEALER_ALIASES (e.g. 'vtt' → 'VT Thomas')
      2. Exact case-insensitive match against existing list
      3. Substring containment (one fully inside the other)
      4. Edit distance — stricter threshold for short names to avoid false positives:
           ≤4 chars: must be exact (already handled above)
           5 chars: distance ≤ 1
           6+ chars: distance ≤ 2
    """
    if not name:
        return None

    # 1. Alias table lookup first
    alias_hit = config.DEALER_ALIASES.get(name.strip().lower())
    if alias_hit and alias_hit in existing:
        return alias_hit

    nl = name.lower().strip()
    for e in existing:
        el = e.lower().strip()
        # 2. Exact case-insensitive
        if nl == el:
            return e
        # 3. Substring containment — only when the shorter word is ≥ 3 chars
        #    to avoid single-letter or two-letter false matches
        shorter, longer = (nl, el) if len(nl) <= len(el) else (el, nl)
        if len(shorter) >= 3 and shorter in longer:
            return e
        # 4. Edit distance with length-dependent threshold
        if abs(len(nl) - len(el)) > 2:
            continue
        max_len = max(len(nl), len(el))
        padded_nl = nl.ljust(max_len)
        padded_el = el.ljust(max_len)
        diffs = sum(a != b for a, b in zip(padded_nl, padded_el))
        threshold = 1 if max_len <= 5 else 2
        if diffs <= threshold:
            return e
    return None


# ── 4. ERP lookup ────────────────────────────────────────────────────────────

def fetch_order_details(order_number: str, token: str) -> Optional[dict]:
    """
    Fetch blind count + dealer name for a single order from the active ERP.
    Preserves split-blind logic: split option → qty × 2.
    Returns { order_number, blind_count, dealer_name } or None if not found.
    """
    def _norm(n: str | None) -> str:
        if not n:
            return "0"
        digits = re.sub(r"\D", "", str(n))
        return digits.lstrip("0") or "0"

    target = _norm(order_number)

    for tenant_id in config.ERP_TENANT_IDS:
        headers = {
            "Authorization":    f"Bearer {token}",
            "X-Active-Tenant-Id": str(tenant_id),
        }
        params = {"search": order_number, "per_page": 20}
        url    = f"{config.ERP_BASE_URL}/admin/orders"

        try:
            resp = erp._SESSION.get(url, headers=headers, params=params, timeout=20)
        except Exception as exc:
            print(YELLOW(f"  ⚠️  Network error fetching {order_number}: {exc}"))
            continue

        if resp.status_code == 401:
            raise RuntimeError("ERP token rejected (401). Re-run to force re-login.")
        if resp.status_code != 200:
            continue

        body   = resp.json()
        orders = body.get("data", body) if isinstance(body, dict) else body
        if not isinstance(orders, list):
            continue

        for order in orders:
            erp_num = str(order.get("order_number", "")).strip()
            if _norm(erp_num) != target:
                continue

            # Count blinds, accounting for split-blind (each split unit = 2 installs)
            lines       = order.get("lines", [])
            blind_count = 0
            for line in (lines if isinstance(lines, list) else []):
                qty      = int(line.get("quantity") or 0)
                is_split = False
                for attr in (line.get("attributes") or []):
                    if isinstance(attr, dict):
                        attr_name = str(
                            attr.get("field_key") or attr.get("field_label") or ""
                        ).lower().replace("_", " ")
                        attr_val = str(
                            attr.get("value") or attr.get("label") or ""
                        ).strip().lower()
                        if "split" in attr_name and attr_val in ("yes", "true", "1"):
                            is_split = True
                            break
                blind_count += qty * 2 if is_split else qty

            dealer_raw  = order.get("dealer") or {}
            dealer_name = (
                dealer_raw.get("name", "Unknown")
                if isinstance(dealer_raw, dict)
                else str(dealer_raw)
            )
            customer_raw  = order.get("job") or {}
            customer_name = (
                customer_raw.get("customer_name", "")
                if isinstance(customer_raw, dict)
                else str(customer_raw)
            ) or ""
            return {
                "order_number":  erp_num,
                "blind_count":   blind_count,
                "dealer_name":   dealer_name,
                "customer_name": customer_name,
            }

    return None  # not found in any tenant


def fetch_all_batch_orders(batches: list[dict]) -> dict[str, dict | None]:
    """
    Collect all unique order numbers across all batches, fetch in parallel.
    Each order is fetched exactly once even if it appears in multiple batches.
    Returns { "ORD-0121": {details} or None, ... }
    """
    unique_numbers: list[str] = []
    seen: set[str] = set()
    for batch in batches:
        for o in batch["orders"]:
            num = o["order_number"]
            if num not in seen:
                seen.add(num)
                unique_numbers.append(num)

    if not unique_numbers:
        return {}

    token   = erp.get_token()
    results: dict[str, dict | None] = {}

    with ThreadPoolExecutor(max_workers=min(10, len(unique_numbers))) as pool:
        future_to_num = {
            pool.submit(fetch_order_details, num, token): num
            for num in unique_numbers
        }
        for future in as_completed(future_to_num):
            num = future_to_num[future]
            try:
                results[num] = future.result()
            except RuntimeError as exc:
                print(RED(f"  ✗ ERP error for {num}: {exc}"))
                results[num] = None

    return results


def _pre_fetch_dealers(order_numbers: list[str]) -> dict[str, dict | None]:
    """
    Quick parallel ERP lookup called immediately after a batch is confirmed,
    so dealer names and blind counts are visible in the preview before calculate.
    REWORK-* orders are skipped (they have no ERP record).
    Failures are silently swallowed — a None entry shows '—' in preview.
    """
    # Filter out synthetic REWORK-* IDs — they don't exist in ERP
    erp_numbers = [n for n in order_numbers if not n.startswith("REWORK-")]
    results: dict[str, dict | None] = {
        n: None for n in order_numbers if n.startswith("REWORK-")
    }
    if not erp_numbers:
        return results
    try:
        token = erp.get_token()
        with ThreadPoolExecutor(max_workers=min(10, len(erp_numbers))) as pool:
            future_to_num = {
                pool.submit(fetch_order_details, num, token): num
                for num in erp_numbers
            }
            for future in as_completed(future_to_num):
                num = future_to_num[future]
                try:
                    results[num] = future.result()
                except Exception:
                    results[num] = None
        return results
    except Exception:
        results.update({n: None for n in erp_numbers})
        return results


# ── 5. Dual Ledger Calculation ───────────────────────────────────────────────

def calculate_dual_ledger(
    batches: list[dict],
    erp_results: dict[str, dict | None],
) -> tuple[dict, dict, list[str]]:
    """
    Build two financial ledgers from all batch entries:

    installer_ledger:
        {
          "John": {
            "total_pay": 146.00,
            "orders": [
              { order_number, dealer, blinds, type, big_ladder,
                installer_pay, dealer_charge }
            ]
          }
        }

    dealer_ledger:
        {
          "VT Thomas": {
            "total_charge": 80.00,
            "orders": [
              { order_number, installer, blinds, type, big_ladder, charge }
            ]
          }
        }

    not_found: list of order numbers not found in ERP (excluded from calc)
    """
    installer_ledger: dict     = {}
    dealer_ledger:    dict     = {}
    not_found:        list[str] = []

    # Track which batches reference each order (for duplicate detection)
    order_to_installers: dict[str, list[str]] = {}

    for batch in batches:
        # Normalize installer name through aliases so 'Michael' and 'Mike' merge
        installer = (
            config.DEALER_ALIASES.get(batch["installer"].strip().lower())
            or batch["installer"]
        )
        if installer not in installer_ledger:
            installer_ledger[installer] = {"total_pay": 0.0, "orders": []}

        for o in batch["orders"]:
            order_num  = o["order_number"]
            big_ladder = o.get("big_ladder", False)

            # Track for duplicate warnings
            order_to_installers.setdefault(order_num, []).append(installer)

            erp_data   = erp_results.get(order_num)
            is_rework  = o.get("is_rework", False)

            # ── Resolve blind count actions ─────────────────────────────────
            erp_count = erp_data.get("blind_count") if erp_data else None

            i_txt = o.get("installs_txt")
            u_txt = o.get("uninstalls_txt")
            r_txt = o.get("reworks_txt")

            if erp_count is not None:
                if i_txt or u_txt or r_txt:
                    i_base = erp_count if i_txt else 0
                    u_base = erp_count if u_txt else 0
                    r_base = erp_count if r_txt else 0
                else:
                    i_base = erp_count
                    u_base = 0
                    r_base = 0
            else:
                i_base = i_txt or 0
                u_base = u_txt or 0
                r_base = r_txt or 0

            # But wait, if they have NO ERP data, and NO text data, they are skipped.
            if i_base == 0 and u_base == 0 and r_base == 0:
                if not o.get("installs_override") and not o.get("uninstalls_override") and not o.get("reworks_override"):
                    if not big_ladder:
                        if erp_data is None and order_num not in not_found and not o.get("is_rework"):
                            not_found.append(order_num)
                        continue

            installs = o.get("installs_override") if o.get("installs_override") is not None else i_base
            uninstalls = o.get("uninstalls_override") if o.get("uninstalls_override") is not None else u_base
            reworks = o.get("reworks_override") if o.get("reworks_override") is not None else r_base

            total_actions = installs + uninstalls + reworks

            if total_actions == 0 and not big_ladder:
                if erp_data is None and order_num not in not_found and not o.get("is_rework"):
                    not_found.append(order_num)
                continue

            # ── Resolve dealer ──────────────────────────────────────────────
            dealer_name = (
                o.get("dealer_override")
                or o.get("dealer_text")
                or o.get("dealer_cust_match")
                or (erp_data["dealer_name"] if erp_data else None)
                or "Unknown"
            )

            # Financial computation
            installer_pay = (
                installs * INSTALLER_RATE_INSTALL
                + uninstalls * INSTALLER_RATE_UNINSTALL
                + reworks * INSTALLER_RATE_REWORK
                + (INSTALLER_BIG_LADDER if big_ladder else 0)
            )
            dealer_charge = (
                total_actions * DEALER_RATE_BLIND
                + (DEALER_BIG_LADDER if big_ladder else 0)
            )

            fuzzy_dealer = _fuzzy_match(dealer_name, list(config.DEALER_ALIASES.values())) or dealer_name
            fuzzy_installer = _fuzzy_match(installer, list(config.DEALER_ALIASES.values()))
            
            is_self_install = False
            if fuzzy_installer and fuzzy_installer == fuzzy_dealer:
                is_self_install = True
                installer_pay = 0.0

            # Installer ledger entry
            installer_ledger[installer]["total_pay"] += installer_pay
            installer_ledger[installer]["orders"].append({
                "order_number":  order_num,
                "dealer":        fuzzy_dealer,
                "installs":      installs,
                "uninstalls":    uninstalls,
                "reworks":       reworks,
                "big_ladder":    big_ladder,
                "installer_pay": installer_pay,
                "dealer_charge": dealer_charge,
            })

            # Dealer ledger entry — use fuzzy_dealer as key so aliases merge
            if fuzzy_dealer not in dealer_ledger:
                dealer_ledger[fuzzy_dealer] = {
                    "total_charge": 0.0,
                    "orders":       [],   # chargeable orders
                    "self_orders":  [],   # self-installs — shown separately, excluded from total
                }
            entry = {
                "order_number": order_num,
                "installer":    installer,
                "installs":     installs,
                "uninstalls":   uninstalls,
                "reworks":      reworks,
                "big_ladder":   big_ladder,
                "charge":       dealer_charge,
            }
            if is_self_install:
                dealer_ledger[fuzzy_dealer]["self_orders"].append(entry)
            else:
                dealer_ledger[fuzzy_dealer]["total_charge"] += dealer_charge
                dealer_ledger[fuzzy_dealer]["orders"].append(entry)

    # Warn about orders appearing in multiple installer batches
    duplicates = {k: v for k, v in order_to_installers.items() if len(v) > 1}
    if duplicates:
        print()
        print(YELLOW("  ⚠️  The following orders appear in multiple installer batches:"))
        for num, installers in sorted(duplicates.items()):
            print(YELLOW(f"    • {num} — batches: {', '.join(installers)}"))
        print(YELLOW("    Each appearance is counted separately in the ledgers."))

    return installer_ledger, dealer_ledger, not_found


# ── 6. Terminal Display ──────────────────────────────────────────────────────

def display_dual_summary(
    installer_ledger: dict,
    dealer_ledger: dict,
    not_found: list[str],
) -> None:
    """Print Installer Payments and Dealer Charges tables to the terminal."""

    # ── Installer Payments ───────────────────────────────────────────────────
    print()
    sep = "═" * 44
    print(BOLD(CYAN(f"  ╔══ 💼 INSTALLER PAYMENTS {sep}")))
    print()

    grand_total_pay = 0.0
    for installer, data in sorted(installer_ledger.items()):
        rows = []
        for o in data["orders"]:
            ladder_str = "🔺 +$20" if o["big_ladder"] else "—"
            rows.append([
                o["order_number"],
                o["dealer"] + (" (Self)" if o.get("self_install") else ""),
                str(o["installs"]) if o["installs"] else "—",
                str(o["uninstalls"]) if o["uninstalls"] else "—",
                str(o["reworks"]) if o["reworks"] else "—",
                ladder_str,
                f"${o['installer_pay']:.2f}",
            ])
        rows.append(["", "", "", "", "", BOLD("SUBTOTAL"), BOLD(f"${data['total_pay']:.2f}")])

        print(BOLD(f"  {installer}"))
        print(tabulate(
            rows,
            headers=["Order", "Dealer", "Inst", "Uninst", "Rwks", "Big Ladder", "Pay"],
            tablefmt="rounded_outline",
            colalign=("left", "left", "center", "center", "center", "left", "right")
        ))
        print()
        grand_total_pay += data["total_pay"]

    print(BOLD(GREEN(f"  ══ GRAND TOTAL TO PAY INSTALLERS: ${grand_total_pay:.2f} ══")))

    # ── Dealer Charges ───────────────────────────────────────────────────────
    print()
    print(BOLD(CYAN(f"  ╔══ 🏪 DEALER CHARGES {sep}")))
    print()

    grand_total_charge = 0.0
    for dealer, data in sorted(dealer_ledger.items()):
        rows = []
        for o in data["orders"]:
            ladder_str = "🔺 +$25" if o["big_ladder"] else "—"
            rows.append([
                o["order_number"],
                o["installer"],
                str(o["installs"]) if o["installs"] else "—",
                str(o["uninstalls"]) if o["uninstalls"] else "—",
                str(o["reworks"]) if o["reworks"] else "—",
                ladder_str,
                f"${o['charge']:.2f}",
            ])
        rows.append(["", "", "", "", "", BOLD("SUBTOTAL"), BOLD(f"${data['total_charge']:.2f}")])

        print(BOLD(f"  {dealer}"))
        print(tabulate(
            rows,
            headers=["Order", "Installer", "Inst", "Uninst", "Rwks", "Big Ladder", "Charge"],
            tablefmt="rounded_outline",
            colalign=("left", "left", "center", "center", "center", "left", "right")
        ))

        # Self-install orders — shown separately, not included in total
        self_orders = data.get("self_orders", [])
        if self_orders:
            self_rows = []
            for o in self_orders:
                ladder_str = "🔺 +$25" if o["big_ladder"] else "—"
                self_rows.append([
                    o["order_number"],
                    o["installer"],
                    str(o["installs"]) if o["installs"] else "—",
                    str(o["uninstalls"]) if o["uninstalls"] else "—",
                    str(o["reworks"]) if o["reworks"] else "—",
                    ladder_str,
                    DIM(f"${o['charge']:.2f} ✕"),
                ])
            self_total = sum(o["charge"] for o in self_orders)
            self_rows.append(["", "", "", "", "", DIM("Self-installs"), DIM(f"${self_total:.2f} (not charged)")])
            print(DIM("  🔄 Self-installs (excluded from total):"))
            print(tabulate(
                self_rows,
                headers=["Order", "Installer", "Inst", "Uninst", "Rwks", "Big Ladder", "Charge"],
                tablefmt="rounded_outline",
                colalign=("left", "left", "center", "center", "center", "left", "right")
            ))

        print()
        grand_total_charge += data["total_charge"]

    print(BOLD(GREEN(f"  ══ GRAND TOTAL TO COLLECT FROM DEALERS: ${grand_total_charge:.2f} ══")))

    # ── Not found ────────────────────────────────────────────────────────────
    if not_found:
        print()
        print(YELLOW(f"  ⚠️  {len(not_found)} order(s) NOT found in ERP:"))
        for num in sorted(not_found):
            print(YELLOW(f"    • {num}"))

    print()


# ── 7. Email Report ──────────────────────────────────────────────────────────

def send_mir_email(
    installer_ledger: dict,
    dealer_ledger: dict,
    not_found: list[str],
) -> None:
    """Send a single combined HTML MIR report via Gmail SMTP."""
    from config import NOTIFY_EMAIL_FROM, NOTIFY_EMAIL_TO, NOTIFY_EMAIL_APP_PASSWORD

    if not all([NOTIFY_EMAIL_FROM, NOTIFY_EMAIL_TO, NOTIFY_EMAIL_APP_PASSWORD]):
        print(YELLOW("  ⚠️  Email skipped — NOTIFY_EMAIL_* not configured in .env"))
        return

    today        = datetime.now().strftime("%B %-d, %Y")
    n_installers = len(installer_ledger)
    n_dealers    = len(dealer_ledger)
    subject = (
        f"[{config.ACTIVE_COMPANY_LABEL}] MIR Report — {today} — "
        f"{n_installers} installer{'s' if n_installers != 1 else ''}, "
        f"{n_dealers} dealer{'s' if n_dealers != 1 else ''}"
    )

    html  = _build_mir_html(installer_ledger, dealer_ledger, not_found, today)
    plain = _build_mir_plain(installer_ledger, dealer_ledger, not_found, today)

    msg            = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = NOTIFY_EMAIL_FROM
    msg["To"]      = NOTIFY_EMAIL_TO
    msg.attach(MIMEText(plain, "plain"))
    msg.attach(MIMEText(html,  "html"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=15) as server:
            server.login(NOTIFY_EMAIL_FROM, NOTIFY_EMAIL_APP_PASSWORD)
            server.sendmail(NOTIFY_EMAIL_FROM, NOTIFY_EMAIL_TO, msg.as_string())
        print(GREEN(f"  📧 MIR email sent to {NOTIFY_EMAIL_TO}"))
    except Exception as exc:
        print(RED(f"  ✗ Email send failed: {exc}"))


def _build_mir_plain(
    installer_ledger: dict,
    dealer_ledger: dict,
    not_found: list[str],
    today: str,
) -> str:
    lines = [
        f"MIR Report — {config.ACTIVE_COMPANY_LABEL}",
        f"Date: {today}",
        "",
        "=" * 54,
        "INSTALLER PAYMENTS",
        "=" * 54,
    ]
    grand_pay = 0.0
    for installer, data in sorted(installer_ledger.items()):
        lines += ["", f"  {installer}:"]
        for o in data["orders"]:
            ladder = f" + Big Ladder (${INSTALLER_BIG_LADDER:.0f})" if o["big_ladder"] else ""
            counts = f"{o['installs']}I, {o['uninstalls']}U, {o['reworks']}R"
            lines.append(
                f"    {o['order_number']}  [{counts}]  "
                f"→  Pay: ${o['installer_pay']:.2f}{ladder}"
                f"  (Dealer: {o['dealer']})"
            )
        lines.append(f"  Subtotal Pay: ${data['total_pay']:.2f}")
        grand_pay += data["total_pay"]
    lines += ["", f"GRAND TOTAL TO PAY INSTALLERS: ${grand_pay:.2f}", ""]

    lines += [
        "=" * 54,
        "DEALER CHARGES",
        "=" * 54,
    ]
    grand_charge = 0.0
    for dealer, data in sorted(dealer_ledger.items()):
        lines += ["", f"  {dealer}:"]
        for o in data["orders"]:
            ladder = f" + Big Ladder (${DEALER_BIG_LADDER:.0f})" if o["big_ladder"] else ""
            counts = f"{o['installs']}I, {o['uninstalls']}U, {o['reworks']}R"
            lines.append(
                f"    {o['order_number']}  [{counts}]  "
                f"→  Charge: ${o['charge']:.2f}{ladder}"
                f"  (Installer: {o['installer']})"
            )
        lines.append(f"  Subtotal Charge: ${data['total_charge']:.2f}")
        grand_charge += data["total_charge"]
    lines += ["", f"GRAND TOTAL TO COLLECT FROM DEALERS: ${grand_charge:.2f}", ""]

    if not_found:
        lines += ["⚠ Orders Not Found in ERP:"]
        for num in sorted(not_found):
            lines.append(f"  • {num}")

    return "\n".join(lines)


def _val_html(val: int) -> str:
    """Return styled value for HTML table, gray dash if 0."""
    return str(val) if val else '<span style="opacity:0.3;">—</span>'


def _build_mir_html(
    installer_ledger: dict,
    dealer_ledger: dict,
    not_found: list[str],
    today: str,
) -> str:
    grand_pay    = sum(d["total_pay"]    for d in installer_ledger.values())
    grand_charge = sum(d["total_charge"] for d in dealer_ledger.values())

    # Common table header style
    TH = (
        "padding:6px 12px;text-align:left;border-bottom:1px solid rgba(255,255,255,0.15);"
        "font-size:11px;text-transform:uppercase;letter-spacing:0.05em;opacity:0.7;"
    )
    TH_R = TH.replace("text-align:left", "text-align:right")
    TH_C = TH.replace("text-align:left", "text-align:center")

    # ── Installer blocks ─────────────────────────────────────────────────────
    installer_html = ""
    for installer, data in sorted(installer_ledger.items()):
        order_rows = ""
        for o in data["orders"]:
            ladder_html = (
                '<span style="color:#ffb300;font-weight:700;">🔺 +$20</span>'
                if o["big_ladder"] else
                '<span style="color:rgba(255,255,255,0.35);">—</span>'
            )
            order_rows += f"""
              <tr>
                <td style="padding:7px 12px;border-bottom:1px solid rgba(255,255,255,0.08);">{o['order_number']}</td>
                <td style="padding:7px 12px;border-bottom:1px solid rgba(255,255,255,0.08);">{o['dealer']}</td>
                <td style="padding:7px 12px;text-align:center;border-bottom:1px solid rgba(255,255,255,0.08);">{_val_html(o['installs'])}</td>
                <td style="padding:7px 12px;text-align:center;border-bottom:1px solid rgba(255,255,255,0.08);">{_val_html(o['uninstalls'])}</td>
                <td style="padding:7px 12px;text-align:center;border-bottom:1px solid rgba(255,255,255,0.08);">{_val_html(o['reworks'])}</td>
                <td style="padding:7px 12px;text-align:center;border-bottom:1px solid rgba(255,255,255,0.08);">{ladder_html}</td>
                <td style="padding:7px 12px;text-align:right;font-weight:700;border-bottom:1px solid rgba(255,255,255,0.08);">${o['installer_pay']:.2f}</td>
              </tr>"""

        installer_html += f"""
        <div style="margin-bottom:16px;border-bottom:1px solid rgba(255,255,255,0.12);padding-bottom:16px;">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;">
            <span style="font-size:15px;font-weight:700;">👤 {installer}</span>
            <span style="font-size:15px;font-weight:700;color:#a5d6a7;background:rgba(255,255,255,0.08);
                         padding:4px 12px;border-radius:20px;">Pay: ${data['total_pay']:.2f}</span>
          </div>
          <table style="width:100%;border-collapse:collapse;font-size:13px;color:#fff;">
            <thead>
              <tr>
                <th style="{TH}">Order</th>
                <th style="{TH}">Dealer</th>
                <th style="{TH_C}">Inst</th>
                <th style="{TH_C}">Uninst</th>
                <th style="{TH_C}">Rwks</th>
                <th style="{TH_C}">Big Ladder</th>
                <th style="{TH_R}">Pay</th>
              </tr>
            </thead>
            <tbody>{order_rows}
            </tbody>
          </table>
        </div>"""

    # ── Dealer blocks ────────────────────────────────────────────────────────
    TH_D  = "padding:6px 12px;text-align:left;border-bottom:1px solid #e8e8e8;font-size:11px;text-transform:uppercase;letter-spacing:0.05em;color:#888;"
    TH_DR = TH_D.replace("text-align:left", "text-align:right")
    TH_DC = TH_D.replace("text-align:left", "text-align:center")

    dealer_html = ""
    for dealer, data in sorted(dealer_ledger.items()):
        order_rows = ""
        for o in data["orders"]:
            ladder_html = (
                '<span style="color:#e65100;font-weight:700;">🔺 +$25</span>'
                if o["big_ladder"] else
                '<span style="color:#ccc;">—</span>'
            )
            order_rows += f"""
              <tr>
                <td style="padding:7px 12px;border-bottom:1px solid #f0f0f0;">{o['order_number']}</td>
                <td style="padding:7px 12px;border-bottom:1px solid #f0f0f0;">{o['installer']}</td>
                <td style="padding:7px 12px;text-align:center;border-bottom:1px solid #f0f0f0;">{_val_html(o['installs'])}</td>
                <td style="padding:7px 12px;text-align:center;border-bottom:1px solid #f0f0f0;">{_val_html(o['uninstalls'])}</td>
                <td style="padding:7px 12px;text-align:center;border-bottom:1px solid #f0f0f0;">{_val_html(o['reworks'])}</td>
                <td style="padding:7px 12px;text-align:center;border-bottom:1px solid #f0f0f0;">{ladder_html}</td>
                <td style="padding:7px 12px;text-align:right;font-weight:700;border-bottom:1px solid #f0f0f0;color:#1b5e3b;">${o['charge']:.2f}</td>
              </tr>"""

        dealer_html += f"""
        <div style="background:#fff;border:1px solid #e0e0e0;border-radius:10px;
                    overflow:hidden;margin-bottom:14px;">
          <div style="display:flex;justify-content:space-between;align-items:center;
                      padding:12px 16px;background:#f7f7f7;border-bottom:1px solid #e0e0e0;">
            <span style="font-size:15px;font-weight:700;">🏪 {dealer}</span>
            <span style="font-size:15px;font-weight:700;color:#fff;background:#1b5e3b;
                         padding:4px 14px;border-radius:20px;">Charge: ${data['total_charge']:.2f}</span>
          </div>
          <table style="width:100%;border-collapse:collapse;font-size:13px;">
            <thead>
              <tr style="background:#fafafa;">
                <th style="{TH_D}">Order</th>
                <th style="{TH_D}">Installer</th>
                <th style="{TH_DC}">Inst</th>
                <th style="{TH_DC}">Uninst</th>
                <th style="{TH_DC}">Rwks</th>
                <th style="{TH_DC}">Big Ladder</th>
                <th style="{TH_DR}">Charge</th>
              </tr>
            </thead>
            <tbody>{order_rows}
            </tbody>
          </table>
        </div>"""

    # ── Not found ────────────────────────────────────────────────────────────
    not_found_html = ""
    if not_found:
        items = "".join(
            f'<li style="padding:3px 0;">'
            f'<code style="background:#fff3e0;padding:2px 6px;border-radius:4px;">{n}</code>'
            f'</li>'
            for n in sorted(not_found)
        )
        not_found_html = f"""
        <div style="margin-top:24px;padding:16px 18px;background:#fff8e1;
                    border-left:4px solid #ff8f00;border-radius:6px;">
          <h3 style="margin:0 0 6px;color:#e65100;font-size:14px;">⚠ Orders Not Found in ERP</h3>
          <p style="color:#666;font-size:12px;margin:0 0 8px;">
            Verify these order numbers manually — they are excluded from all calculations:
          </p>
          <ul style="margin:0;padding-left:20px;font-size:13px;">{items}</ul>
        </div>"""

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="font-family:Arial,sans-serif;max-width:760px;margin:0 auto;padding:24px;
             color:#333;background:#f2f4f6;">

  <!-- Header -->
  <div style="background:#1b5e3b;border-radius:14px 14px 0 0;padding:22px 24px;color:#fff;">
    <h2 style="margin:0 0 4px;font-size:20px;">🪟 {config.ACTIVE_COMPANY_LABEL} — MIR Report</h2>
    <p style="margin:0;opacity:0.75;font-size:13px;">
      {today} &nbsp;|&nbsp; {len(installer_ledger)} installer{'s' if len(installer_ledger) != 1 else ''}
      &nbsp;|&nbsp; {len(dealer_ledger)} dealer{'s' if len(dealer_ledger) != 1 else ''}
    </p>
  </div>

  <!-- Summary Cards -->
  <div style="display:flex;gap:0;margin-bottom:28px;">
    <div style="flex:1;background:#2e7d52;color:#fff;padding:18px 20px;
                border-radius:0 0 0 14px;text-align:center;">
      <div style="font-size:11px;opacity:0.8;text-transform:uppercase;letter-spacing:0.08em;">
        Total Pay to Installers
      </div>
      <div style="font-size:30px;font-weight:700;margin-top:6px;">${grand_pay:.2f}</div>
    </div>
    <div style="flex:1;background:#0d47a1;color:#fff;padding:18px 20px;
                border-radius:0 0 14px 0;text-align:center;">
      <div style="font-size:11px;opacity:0.8;text-transform:uppercase;letter-spacing:0.08em;">
        Total Collect from Dealers
      </div>
      <div style="font-size:30px;font-weight:700;margin-top:6px;">${grand_charge:.2f}</div>
    </div>
  </div>

  <!-- Installer Section -->
  <div style="background:#1b3a2b;border-radius:14px;padding:20px 22px;
              margin-bottom:28px;color:#fff;">
    <h3 style="margin:0 0 16px;font-size:16px;
               border-bottom:1px solid rgba(255,255,255,0.2);padding-bottom:10px;">
      💼 Installer Payments
    </h3>
    {installer_html}
  </div>

  <!-- Dealer Section -->
  <div style="margin-bottom:24px;">
    <h3 style="font-size:16px;margin:0 0 14px;color:#0d47a1;font-weight:700;">🏪 Dealer Charges</h3>
    {dealer_html}
  </div>

  {not_found_html}

  <p style="color:#aaa;font-size:11px;margin-top:32px;text-align:center;border-top:1px solid #ddd;padding-top:16px;">
    Sent by {config.ACTIVE_COMPANY_LABEL} Order Automation — MIR Calculator v2
  </p>
</body>
</html>"""


# ── 8. Main entry point ──────────────────────────────────────────────────────

def run_mir(dry_run: bool = False) -> None:
    """Top-level function called by main.py for Mode 2 — MIR Calculate."""

    print(BOLD(CYAN("""
╬══════════════════════════════════════════════════╪
║   MIR Calculator  v2  —  Dual Ledger               ║
║   Installer Pay  |  Dealer Charges                 ║
╚══════════════════════════════════════════════════╨
""")))

    batches: list[dict] = []  # [{installer, orders: [{order_number, type, big_ladder, assumed}]}]

    # ── Main batch entry loop ─────────────────────────────────────────────────
    while True:
        print()
        if batches:
            print(BOLD(f"  Batches entered so far: {len(batches)}"))
            for b in batches:
                n = len(b["orders"])
                print(DIM(f"    • {b['installer']} — {n} order{'s' if n != 1 else ''}"))
        else:
            print(DIM("  No batches entered yet."))

        print()
        print("  [1] Add installer batch")
        print("  [2] Review all entered batches")
        print("  [3] Calculate & generate report")
        print("  [Q] Quit")
        print()

        choice = input(BOLD("  Select: ")).strip().lower()

        # ── Quit ─────────────────────────────────────────────────────────────
        if choice in ("q", "quit", "exit"):
            if batches:
                confirm = input(
                    YELLOW(f"  You have {len(batches)} batch(es). Quit anyway? [Y/N]: ")
                ).strip().lower()
                if confirm not in ("y", "yes"):
                    continue
            print(YELLOW("  Exiting MIR Calculator."))
            return

        # ── Add installer batch ───────────────────────────────────────────────
        elif choice == "1":
            # Prompt installer name
            while True:
                installer_name = input(BOLD("\n  Enter installer name: ")).strip()
                if installer_name:
                    break
                print(RED("  Installer name cannot be empty."))

            # ── Name resolution ───────────────────────────────────────────
            existing_names = [b["installer"] for b in batches]
            similar = _fuzzy_match(installer_name, existing_names)

            if similar and similar.lower() == installer_name.lower():
                # Exact match (same name already entered) — offer to merge
                existing_batch = next(b for b in batches if b["installer"] == similar)
                existing_count = len(existing_batch["orders"])
                print(YELLOW(
                    f"\n  ⚠️  {similar} already has {existing_count} order(s) in a batch."
                ))
                merge_ans = input(BOLD(
                    f'  [A] Add more orders to {similar}\'s batch  '
                    f'[N] Keep as separate batch: '
                )).strip().lower()
                if merge_ans in ("a", "add", "y", "yes"):
                    installer_name = similar
                    merge_mode = True
                else:
                    merge_mode = False
            elif similar and similar.lower() != installer_name.lower():
                # Similar but different name — "did you mean?" prompt
                ans = input(YELLOW(
                    f'  ⚠️  Did you mean "{similar}"? '
                    f'[Y] Use "{similar}"  [N] Keep "{installer_name}": '
                )).strip().lower()
                if ans in ("y", "yes"):
                    installer_name = similar
                merge_mode = False
            else:
                merge_mode = False

            # Prompt raw text
            print(YELLOW(
                f"\n  Paste orders for {BOLD(installer_name)} "
                "(any format — press Enter twice when done):"
            ))
            lines_buf: list[str] = []
            try:
                while True:
                    line = input()
                    if line == "" and lines_buf and lines_buf[-1] == "":
                        break
                    lines_buf.append(line)
            except EOFError:
                pass

            raw_text = "\n".join(lines_buf).strip()
            if not raw_text:
                print(RED("  No text entered. Batch skipped."))
                continue

            # Parse with Groq
            print(YELLOW("\n  ⏳ Parsing with Groq AI..."))
            lines = raw_text.splitlines()
            chunk_size = 20
            parsed_orders = []
            
            for i in range(0, len(lines), chunk_size):
                chunk = "\n".join(lines[i:i+chunk_size]).strip()
                if not chunk:
                    continue
                if len(lines) > chunk_size:
                    print(DIM(f"     [Processing lines {i+1} to {min(i+chunk_size, len(lines))}]"))
                parsed_orders.extend(parse_order_jobs(chunk))

            if not parsed_orders:
                print(RED("  ✗ No valid orders found in the pasted text."))
                print(RED("    Make sure your text contains order or ON numbers."))
                continue

            print(GREEN(f"  ✓ Found {len(parsed_orders)} order(s)."))

            # Pre-fetch ERP dealer names BEFORE review so the user sees them while editing.
            # Raw-text values (dealer_text, installs_txt, uninstalls_txt, reworks_txt)
            # are NEVER overwritten by ERP data — ERP only fills in gaps.
            print(YELLOW("  🔍 Looking up dealer names from ERP..."))
            nums_to_fetch = [o["order_number"] for o in parsed_orders]
            preview_data  = _pre_fetch_dealers(nums_to_fetch)
            for o in parsed_orders:
                o["erp_preview"] = preview_data.get(o["order_number"])
                # Auto-assign dealer from customer name if no raw text dealer exists.
                # This is flagged for review (shown with ⚑ in preview) and never
                # overwrites a dealer that came from the raw text.
                if not o.get("dealer_text") and not o.get("dealer_override"):
                    cust = (o["erp_preview"] or {}).get("customer_name", "") or ""
                    cust_dealer = _fuzzy_match(cust, list(config.DEALER_ALIASES.values()))
                    if cust_dealer:
                        # Only auto-assign if it differs from the ERP dealer field
                        erp_dealer = (o["erp_preview"] or {}).get("dealer_name", "")
                        erp_dealer_fuzzy = _fuzzy_match(erp_dealer, list(config.DEALER_ALIASES.values()))
                        if erp_dealer_fuzzy != cust_dealer:
                            o["dealer_cust_match"] = cust_dealer
                        else:
                            o["dealer_cust_match"] = None
                    else:
                        o["dealer_cust_match"] = None

            found_dealers = sum(1 for v in preview_data.values() if v is not None)
            if found_dealers:
                print(GREEN(f"  ✓ Dealer names resolved: {found_dealers}/{len(nums_to_fetch)}"))
            else:
                print(YELLOW("  ⚠️  Dealer names unavailable (ERP unreachable) — will retry at calculate."))

            # Confirm / edit AFTER ERP data is available so the user sees real dealer names
            confirmed = confirm_batch(installer_name, parsed_orders)
            if confirmed is None:
                continue  # user discarded the batch

            if merge_mode:
                # Merge into the existing batch for this installer
                target = next(b for b in batches if b["installer"] == installer_name)
                target["orders"].extend(confirmed)
                total = len(target["orders"])
                print(GREEN(
                    f"\n  ✓ Added {len(confirmed)} order(s) to {installer_name}'s batch "
                    f"(now {total} total)."
                ))
            else:
                batches.append({"installer": installer_name, "orders": confirmed})
                print(GREEN(f"\n  ✓ Batch for {installer_name} saved ({len(confirmed)} order(s))."))

        # ── Review all batches ────────────────────────────────────────────────
        elif choice == "2":
            if not batches:
                print(YELLOW("  No batches entered yet."))
                continue
            
            while True:
                for batch in batches:
                    _print_batch_preview(batch["installer"], batch["orders"])
                
                edit_choice = input(YELLOW("\n  Enter installer name to edit their batch (or press Enter to return): ")).strip()
                if not edit_choice:
                    break
                    
                existing_names = [b["installer"] for b in batches]
                similar = _fuzzy_match(edit_choice, existing_names)
                if similar:
                    target = next(b for b in batches if b["installer"] == similar)
                    target["orders"] = _edit_batch(target["installer"], target["orders"])
                    print(GREEN(f"\n  ✓ Edits to {similar}'s batch saved."))
                else:
                    print(RED(f"  ✗ Installer '{edit_choice}' not found."))

        # ── Calculate & report ────────────────────────────────────────────────
        elif choice == "3":
            if not batches:
                print(RED("  No batches to calculate. Add at least one installer batch first."))
                continue

            total_orders = sum(len(b["orders"]) for b in batches)
            print(YELLOW(
                f"\n  ⏳ Fetching {total_orders} order(s) from "
                f"{config.ACTIVE_COMPANY_LABEL} ERP..."
            ))

            try:
                erp_results = fetch_all_batch_orders(batches)
            except RuntimeError as exc:
                print(RED(f"  ✗ ERP lookup failed: {exc}"))
                continue

            unique_total = len(erp_results)
            found   = sum(1 for v in erp_results.values() if v is not None)
            missing = unique_total - found
            print(GREEN(
                f"  ✓ ERP results: {found} found, {missing} not found "
                f"({unique_total} unique order(s) queried)"
            ))

            # Build dual ledger
            installer_ledger, dealer_ledger, not_found = calculate_dual_ledger(
                batches, erp_results
            )

            # Terminal display
            display_dual_summary(installer_ledger, dealer_ledger, not_found)

            if dry_run:
                print(BOLD(YELLOW("  ⚠️  DRY-RUN mode — email not sent.\n")))
                return

            # Send email
            print(YELLOW("  📣 Sending MIR report email..."))
            send_mir_email(installer_ledger, dealer_ledger, not_found)
            return

        else:
            print(RED("  Invalid choice. Please enter 1, 2, 3, or Q."))
