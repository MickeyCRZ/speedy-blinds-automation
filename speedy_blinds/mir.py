"""
mir.py — MIR (Mobile Installer Report) Calculator
===================================================
Calculates what each dealer owes an installer at $3.00 per blind.

Flow:
  1. Prompt installer name
  2. Paste raw order list (any format — Groq normalises it)
  3. ERP lookup: blind count = len(order["lines"]), dealer = order["dealer"]["name"]
  4. Group by dealer → subtotal blinds × $3
  5. Print terminal summary + send HTML email
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

# ── Styling helpers ─────────────────────────────────────────────────────────
def _c(code: str, text: str) -> str:
    if sys.stdout.isatty():
        return f"\033[{code}m{text}\033[0m"
    return text

GREEN  = lambda t: _c("32", t)
YELLOW = lambda t: _c("33", t)
RED    = lambda t: _c("31", t)
BOLD   = lambda t: _c("1",  t)
CYAN   = lambda t: _c("36", t)

MIR_RATE = 3.00   # dollars per blind

# ── 1. Parse order numbers from raw text ────────────────────────────────────

def parse_order_numbers(raw_text: str) -> list[str]:
    """
    Send free-form text to Groq and extract a clean list of order numbers.
    Accepts any format: ON 121, ORD-0121, 121, comma/newline separated, etc.
    Returns normalised list: ["ORD-0121", "ORD-0305", ...]
    """
    client = Groq(api_key=config.GROQ_API_KEY)

    system_prompt = (
        "You are a data-extraction assistant. "
        "Extract ALL order or ON numbers from the text below. "
        "Return ONLY a JSON array of strings, nothing else. "
        "Each number should be returned exactly as it appears (e.g. 'ON 121', 'ORD-0412', '305'). "
        "Example output: [\"ON 121\", \"ORD-0412\", \"305\"]"
    )

    response = client.chat.completions.create(
        model=config.GROQ_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": raw_text},
        ],
        temperature=0,
        max_tokens=512,
    )
    reply = response.choices[0].message.content or ""

    # Extract JSON array
    reply = re.sub(r"```(?:json)?", "", reply).strip()
    match = re.search(r"\[.*\]", reply, re.DOTALL)
    if not match:
        return []

    raw_numbers: list[str] = json.loads(match.group())

    # Normalise to ORD-XXXX
    normalised: list[str] = []
    seen: set[str] = set()
    for raw in raw_numbers:
        digits = re.sub(r"\D", "", str(raw))
        if not digits:
            continue
        normalised_num = f"ORD-{int(digits):04d}"
        if normalised_num not in seen:
            seen.add(normalised_num)
            normalised.append(normalised_num)

    return normalised


# ── 2. ERP lookup ────────────────────────────────────────────────────────────

def fetch_order_details(order_number: str, token: str) -> Optional[dict]:
    """
    Fetch blind count + dealer name for a single order from the active ERP.
    Returns:
        {
            "order_number": "ORD-0121",
            "blind_count":  5,
            "dealer_name":  "VT Thomas",
        }
    or None if not found.
    """
    def _norm(n: str | None) -> str:
        if not n:
            return "0"
        digits = re.sub(r"\D", "", str(n))
        return digits.lstrip("0") or "0"

    target = _norm(order_number)

    for tenant_id in config.ERP_TENANT_IDS:
        headers = {
            "Authorization": f"Bearer {token}",
            "X-Active-Tenant-Id": str(tenant_id),
        }
        params = {"search": order_number, "per_page": 20}
        url = f"{config.ERP_BASE_URL}/admin/orders"

        try:
            resp = erp._SESSION.get(url, headers=headers, params=params, timeout=20)
        except Exception as exc:
            print(YELLOW(f"  ⚠️  Network error fetching {order_number}: {exc}"))
            continue

        if resp.status_code == 401:
            raise RuntimeError("ERP token rejected (401). Re-run to force re-login.")
        if resp.status_code != 200:
            continue

        body = resp.json()
        orders = body.get("data", body) if isinstance(body, dict) else body
        if not isinstance(orders, list):
            continue

        for order in orders:
            erp_num = str(order.get("order_number", "")).strip()
            if _norm(erp_num) == target:
                lines      = order.get("lines", [])
                blind_count = len(lines) if isinstance(lines, list) else 0
                dealer_raw  = order.get("dealer") or {}
                dealer_name = dealer_raw.get("name", "Unknown") if isinstance(dealer_raw, dict) else str(dealer_raw)
                return {
                    "order_number": erp_num,
                    "blind_count":  blind_count,
                    "dealer_name":  dealer_name,
                }

    return None  # not found in any tenant


def fetch_all_orders(order_numbers: list[str]) -> dict[str, dict | None]:
    """
    Parallel ERP lookup for all order numbers.
    Returns { "ORD-0121": {details} or None, ... }
    """
    token = erp.get_token()
    results: dict[str, dict | None] = {}

    with ThreadPoolExecutor(max_workers=min(10, len(order_numbers))) as pool:
        future_to_num = {
            pool.submit(fetch_order_details, num, token): num
            for num in order_numbers
        }
        for future in as_completed(future_to_num):
            num = future_to_num[future]
            try:
                results[num] = future.result()
            except RuntimeError as exc:
                print(RED(f"  ✗ ERP error for {num}: {exc}"))
                results[num] = None

    return results


# ── 3. Calculate & display ───────────────────────────────────────────────────

def calculate_mir(
    installer: str,
    erp_results: dict[str, dict | None],
) -> tuple[dict, list[str]]:
    """
    Group by dealer, compute $3/blind charge.
    Returns:
        summary  : { dealer_name: {"blinds": int, "charge": float, "orders": list[str]} }
        not_found: list of order numbers not found in ERP
    """
    summary: dict[str, dict] = {}
    not_found: list[str] = []

    for order_num, details in erp_results.items():
        if details is None:
            not_found.append(order_num)
            continue

        dealer = details["dealer_name"]
        blinds = details["blind_count"]

        if dealer not in summary:
            summary[dealer] = {"blinds": 0, "charge": 0.0, "orders": []}

        summary[dealer]["blinds"] += blinds
        summary[dealer]["charge"] += blinds * MIR_RATE
        summary[dealer]["orders"].append(
            f"{order_num}({blinds}b)"
        )

    return summary, not_found


def display_mir_summary(
    installer: str,
    summary: dict,
    not_found: list[str],
    erp_results: dict,
) -> None:
    """Print a rich terminal table of the MIR summary."""
    print()
    print(BOLD(CYAN(f"  MIR Summary — Installer: {installer}  |  Rate: ${MIR_RATE:.2f}/blind")))
    print()

    if not summary:
        print(YELLOW("  No orders were found in the ERP. Nothing to charge."))
    else:
        rows = []
        total_blinds = 0
        total_charge = 0.0
        for dealer, data in sorted(summary.items()):
            rows.append([
                dealer,
                data["blinds"],
                f"${data['charge']:.2f}",
                ", ".join(data["orders"]),
            ])
            total_blinds += data["blinds"]
            total_charge += data["charge"]

        # Totals row
        rows.append(["─" * 12, "─" * 6, "─" * 10, ""])
        rows.append([BOLD("TOTAL"), BOLD(str(total_blinds)), BOLD(f"${total_charge:.2f}"), ""])

        print(tabulate(
            rows,
            headers=["Dealer", "Blinds", "Charge", "Orders"],
            tablefmt="rounded_outline",
        ))

    if not_found:
        print()
        print(YELLOW(f"  ⚠️  {len(not_found)} order(s) NOT found in ERP:"))
        for num in not_found:
            print(YELLOW(f"    • {num}"))

    print()


# ── 4. Email report ──────────────────────────────────────────────────────────

def send_mir_email(
    installer: str,
    summary: dict,
    not_found: list[str],
) -> None:
    """Send a formatted HTML MIR report via Gmail SMTP."""
    from config import NOTIFY_EMAIL_FROM, NOTIFY_EMAIL_TO, NOTIFY_EMAIL_APP_PASSWORD

    if not all([NOTIFY_EMAIL_FROM, NOTIFY_EMAIL_TO, NOTIFY_EMAIL_APP_PASSWORD]):
        print(YELLOW("  ⚠️  Email skipped — NOTIFY_EMAIL_* not configured in .env"))
        return

    today = datetime.now().strftime("%B %-d, %Y")
    subject = (
        f"[{config.ACTIVE_COMPANY_LABEL}] MIR — {installer} — {today}"
    )

    html = _build_mir_html(installer, summary, not_found, today)
    plain = _build_mir_plain(installer, summary, not_found, today)

    msg = MIMEMultipart("alternative")
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
    installer: str,
    summary: dict,
    not_found: list[str],
    today: str,
) -> str:
    lines = [
        f"MIR — {config.ACTIVE_COMPANY_LABEL}",
        f"Installer : {installer}",
        f"Date      : {today}",
        f"Rate      : ${MIR_RATE:.2f} per blind",
        "",
        "── Dealer Breakdown ──",
    ]
    total_blinds = 0
    total_charge = 0.0
    for dealer, data in sorted(summary.items()):
        lines.append(
            f"  {dealer}: {data['blinds']} blinds  →  ${data['charge']:.2f}"
        )
        lines.append(f"    Orders: {', '.join(data['orders'])}")
        total_blinds += data["blinds"]
        total_charge += data["charge"]

    lines += ["", f"TOTAL: {total_blinds} blinds  →  ${total_charge:.2f}", ""]

    if not_found:
        lines += ["── ⚠ Not Found in ERP ──"]
        for num in not_found:
            lines.append(f"  • {num}")

    return "\n".join(lines)


def _build_mir_html(
    installer: str,
    summary: dict,
    not_found: list[str],
    today: str,
) -> str:
    total_blinds = sum(d["blinds"] for d in summary.values())
    total_charge = sum(d["charge"] for d in summary.values())

    dealer_rows_html = ""
    for dealer, data in sorted(summary.items()):
        orders_str = ", ".join(data["orders"])
        dealer_rows_html += f"""
        <tr>
          <td style="padding:8px 12px;border-bottom:1px solid rgba(255,255,255,0.12);">
            <strong>{dealer}</strong>
          </td>
          <td style="padding:8px 12px;text-align:center;border-bottom:1px solid rgba(255,255,255,0.12);">
            {data['blinds']}
          </td>
          <td style="padding:8px 12px;text-align:right;border-bottom:1px solid rgba(255,255,255,0.12);font-weight:700;">
            ${data['charge']:.2f}
          </td>
          <td style="padding:8px 12px;font-size:12px;opacity:0.75;border-bottom:1px solid rgba(255,255,255,0.12);">
            {orders_str}
          </td>
        </tr>"""

    not_found_html = ""
    if not_found:
        items = "".join(
            f'<li style="padding:2px 0;"><code>{n}</code></li>'
            for n in not_found
        )
        not_found_html = f"""
        <div style="margin-top:20px;">
          <h3 style="color:#e65100;">⚠ Not Found in ERP</h3>
          <p style="color:#555;font-size:13px;">Verify these order numbers manually.</p>
          <ul>{items}</ul>
        </div>"""

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="font-family:Arial,sans-serif;max-width:680px;margin:0 auto;padding:24px;color:#333;">

  <h2 style="margin-bottom:4px;">🪟 {config.ACTIVE_COMPANY_LABEL} — MIR Report</h2>
  <p style="color:#888;font-size:13px;margin-top:0;">
    Installer: <strong>{installer}</strong> &nbsp;|&nbsp; {today}
    &nbsp;|&nbsp; Rate: <strong>${MIR_RATE:.2f}/blind</strong>
  </p>

  <div style="background:#1b5e3b;border-radius:14px;padding:16px 18px;margin:16px 0;color:#fff;">
    <div style="font-weight:700;font-size:16px;margin-bottom:14px;">
      📋 Dealer Breakdown
    </div>
    <table style="width:100%;border-collapse:collapse;">
      <thead>
        <tr style="opacity:0.65;font-size:12px;text-transform:uppercase;letter-spacing:0.05em;">
          <th style="padding:4px 12px;text-align:left;">Dealer</th>
          <th style="padding:4px 12px;text-align:center;">Blinds</th>
          <th style="padding:4px 12px;text-align:right;">Charge</th>
          <th style="padding:4px 12px;text-align:left;">Orders</th>
        </tr>
      </thead>
      <tbody>
        {dealer_rows_html}
      </tbody>
    </table>

    <div style="border-top:2px solid rgba(255,255,255,0.35);margin-top:14px;padding-top:12px;font-size:14px;">
      <strong>Total Blinds: {total_blinds}</strong>
      &nbsp;&nbsp;&nbsp;
      <strong>Total Charge: ${total_charge:.2f}</strong>
    </div>
  </div>

  {not_found_html}

  <p style="color:#bbb;font-size:11px;margin-top:32px;">
    Sent by {config.ACTIVE_COMPANY_LABEL} Order Automation — MIR Calculator
  </p>
</body>
</html>"""


# ── 5. Main entry point ──────────────────────────────────────────────────────

def run_mir(dry_run: bool = False) -> None:
    """Top-level function called by main.py for Mode 2 — MIR Calculate."""

    print(BOLD(CYAN("""
╬══════════════════════════════════════════════════╪
║   MIR Calculator                                   ║
╚══════════════════════════════════════════════════╨
""")))

    # Prompt installer name
    while True:
        installer = input(BOLD("Enter installer name: ")).strip()
        if installer:
            break
        print(RED("  Installer name cannot be empty."))

    # Prompt raw order list
    print(YELLOW("\nPaste order numbers below (any format). Press Enter twice when done:"))
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
        print(RED("No orders entered. Exiting."))
        return

    # Parse order numbers with Groq
    print(YELLOW("\n⏳ Parsing order numbers..."))
    order_numbers = parse_order_numbers(raw_text)
    if not order_numbers:
        print(RED("✗ No valid order numbers found in the text. Please try again."))
        return
    print(GREEN(f"✓ Found {len(order_numbers)} order number(s): {', '.join(order_numbers)}"))

    # ERP lookup
    print(YELLOW(f"\n⏳ Fetching order details from {config.ACTIVE_COMPANY_LABEL} ERP..."))
    try:
        erp_results = fetch_all_orders(order_numbers)
    except RuntimeError as e:
        print(RED(f"✗ ERP lookup failed: {e}"))
        return

    found    = sum(1 for v in erp_results.values() if v is not None)
    missing  = len(order_numbers) - found
    print(GREEN(f"✓ ERP results: {found} found, {missing} not found"))

    # Calculate
    summary, not_found = calculate_mir(installer, erp_results)

    # Display terminal summary
    display_mir_summary(installer, summary, not_found, erp_results)

    if dry_run:
        print(BOLD(YELLOW("⚠️  DRY-RUN mode — email not sent.\n")))
        return

    # Send email
    print(YELLOW("📣 Sending MIR email..."))
    send_mir_email(installer, summary, not_found)
