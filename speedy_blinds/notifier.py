"""
notifier.py — Run summary notifications
========================================
Sends a macOS system notification (instant, no setup) and a Gmail
summary email after every script run.

Gmail setup (one-time):
  1. Go to https://myaccount.google.com/apppasswords
  2. Create an App Password for "Mail" on "Mac"
  3. Add to .env:
       NOTIFY_EMAIL_FROM=you@gmail.com
       NOTIFY_EMAIL_TO=you@gmail.com        # can be same address
       NOTIFY_EMAIL_APP_PASSWORD=xxxx xxxx xxxx xxxx
"""

from __future__ import annotations

import json
import os
import smtplib
import subprocess
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import config
from config import (
    NOTIFY_EMAIL_APP_PASSWORD,
    NOTIFY_EMAIL_FROM,
    NOTIFY_EMAIL_TO,
)


# ---------------------------------------------------------------------------
# Data class — run report
# ---------------------------------------------------------------------------

class RunReport:
    """Accumulates results during a script run, then formats them for output."""

    def __init__(self) -> None:
        self.started_at: datetime = datetime.now()
        self.orders_extracted: int = 0
        self.orders_selected: int = 0
        self.written: list[dict] = []        # full order dicts + 'range' key
        self.skipped_user: list[dict] = []   # user said 'n' at confirmation
        self.errors: list[dict] = []         # sheet write errors
        self.unmatched_dealers: list[dict] = []  # unknown dealer, saved to file
        self.prices_not_found: list[str] = []    # order numbers with no ERP price
        self.erp_failed: bool = False

    # ── helpers ──────────────────────────────────────────────────────────────

    @property
    def has_issues(self) -> bool:
        return bool(
            self.errors
            or self.unmatched_dealers
            or self.prices_not_found
            or self.erp_failed
        )

    def _duration(self) -> str:
        secs = int((datetime.now() - self.started_at).total_seconds())
        return f"{secs}s"

    @staticmethod
    def _accessories_str(order: dict) -> str:
        """Return a bracketed accessory string, e.g. '(2 motors, 1 remote)' or ''."""
        parts = []
        for key, singular, plural in [
            ("motors",   "motor",   "motors"),
            ("remotes",  "remote",  "remotes"),
            ("solars",   "solar",   "solars"),
            ("chargers", "charger", "chargers"),
        ]:
            val = order.get(key)
            if val and int(val) > 0:
                parts.append(f"{val} {singular if int(val) == 1 else plural}")
        return f" ({', '.join(parts)})" if parts else ""

    def _daily_groups(self) -> list[tuple[str, list[dict]]]:
        """Return written orders grouped and sorted by date."""
        from collections import defaultdict
        groups: dict[str, list[dict]] = defaultdict(list)
        for order in self.written:
            date_key = order.get("date") or "Unknown Date"
            groups[date_key].append(order)
        return sorted(groups.items())  # chronological

    # ── macOS notification ────────────────────────────────────────────────────

    def send_macos_notification(self) -> None:
        """Fire a macOS Notification Centre alert via osascript."""
        title = f"{config.ACTIVE_COMPANY_LABEL} — Run Complete"
        if self.has_issues:
            subtitle = f"✓ {len(self.written)} written  ⚠ Issues found"
        else:
            subtitle = f"✓ {len(self.written)} order(s) written successfully"

        issues = []
        if self.unmatched_dealers:
            issues.append(f"{len(self.unmatched_dealers)} unknown dealer(s) saved to file")
        if self.prices_not_found:
            issues.append(f"{len(self.prices_not_found)} price(s) not found in ERP")
        if self.errors:
            issues.append(f"{len(self.errors)} sheet write error(s)")
        if self.erp_failed:
            issues.append("ERP connection failed")

        body = " | ".join(issues) if issues else f"Run time: {self._duration()}"

        script = (
            f'display notification "{body}" '
            f'with title "{title}" '
            f'subtitle "{subtitle}" '
            f'sound name "Glass"'
        )
        try:
            subprocess.run(
                ["osascript", "-e", script],
                check=True,
                capture_output=True,
            )
        except Exception as exc:
            print(f"  ⚠️  macOS notification failed: {exc}")

    # ── Gmail email ───────────────────────────────────────────────────────────

    def send_email(self) -> None:
        """Send a formatted HTML summary email via Gmail SMTP."""
        if not all([NOTIFY_EMAIL_FROM, NOTIFY_EMAIL_TO, NOTIFY_EMAIL_APP_PASSWORD]):
            print("  ⚠️  Email notification skipped — NOTIFY_EMAIL_* not set in .env")
            return

        subject = (
            f"[{config.ACTIVE_COMPANY_LABEL}] Run complete — "
            f"{len(self.written)} written"
            + (" ⚠ Issues" if self.has_issues else " ✓ All good")
        )

        html = self._build_html()
        plain = self._build_plain()

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = NOTIFY_EMAIL_FROM
        msg["To"] = NOTIFY_EMAIL_TO
        msg.attach(MIMEText(plain, "plain"))
        msg.attach(MIMEText(html, "html"))

        try:
            with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=15) as server:
                server.login(NOTIFY_EMAIL_FROM, NOTIFY_EMAIL_APP_PASSWORD)
                server.sendmail(NOTIFY_EMAIL_FROM, NOTIFY_EMAIL_TO, msg.as_string())
            print(f"  📧 Summary email sent to {NOTIFY_EMAIL_TO}")
        except Exception as exc:
            print(f"  ⚠️  Email send failed: {exc}")

    # ── formatters ────────────────────────────────────────────────────────────

    def _build_plain(self) -> str:
        lines = [
            f"{config.ACTIVE_COMPANY_LABEL} — Automation Run Summary",
            f"Started : {self.started_at.strftime('%Y-%m-%d %H:%M:%S')}",
            f"Duration: {self._duration()}",
            "",
            f"Orders extracted : {self.orders_extracted}",
            f"Orders selected  : {self.orders_selected}",
            f"Written to sheets: {len(self.written)}",
            f"Skipped by user  : {len(self.skipped_user)}",
            f"Sheet errors     : {len(self.errors)}",
            "",
        ]

        if self.written:
            lines.append("── Daily Breakdown ──")
            for date_key, day_orders in self._daily_groups():
                # Format date nicely
                try:
                    from datetime import datetime as dt
                    d = dt.strptime(date_key, "%Y-%m-%d")
                    pretty_date = d.strftime("%B %-d, %Y")
                except ValueError:
                    pretty_date = date_key

                lines.append(f"\n{pretty_date}")
                day_qty = 0
                day_revenue = 0.0
                day_acc: dict[str, int] = {}
                for o in day_orders:
                    on  = (o.get("order_number") or "?").split("-")[-1].lstrip("0") or "0"
                    dealer = o.get("dealer", "?")
                    customer = o.get("customer_name", "?")
                    qty = o.get("qty") or 0
                    amt = o.get("price") or 0.0
                    acc = self._accessories_str(o)
                    lines.append(f"  {on} | {dealer} | {customer}: {qty}{acc}  Revenue: ${amt:,.2f}")
                    day_qty += qty
                    day_revenue += float(amt)
                    for key in ("motors", "remotes", "solars", "chargers"):
                        v = o.get(key)
                        if v:
                            day_acc[key] = day_acc.get(key, 0) + v

                acc_parts = [f"{v} {k.rstrip('s') if v == 1 else k}" for k, v in day_acc.items() if v]
                acc_str = f" ({', '.join(acc_parts)})" if acc_parts else ""
                lines.append(f"  Total Blinds: {day_qty}{acc_str}")
                lines.append(f"  Daily Revenue: ${day_revenue:,.2f}")
            lines.append("")

        if self.unmatched_dealers:
            lines.append("── ⚠ Unknown Dealers (saved to unmatched_orders.json) ──")
            for u in self.unmatched_dealers:
                lines.append(
                    f"  • {u.get('order_number','?')}  dealer='{u.get('dealer','?')}'"
                )
            lines.append("")

        if self.prices_not_found:
            lines.append("── ⚠ Prices Not Found in ERP ──")
            for on in self.prices_not_found:
                lines.append(f"  • {on}")
            lines.append("")

        if self.errors:
            lines.append("── ✗ Sheet Write Errors ──")
            for e in self.errors:
                lines.append(f"  ✗ [{e['dealer']}] {e['order_number']}: {e['error']}")
            lines.append("")

        if self.erp_failed:
            lines.append("── ✗ ERP Connection Failed ──")
            lines.append("  All prices left blank. Check ERP credentials in .env.")
            lines.append("")

        return "\n".join(lines)

    def _build_html(self) -> str:
        def row(label: str, value: str, colour: str = "#333") -> str:
            return (
                f'<tr><td style="padding:4px 12px 4px 0;color:#666;">{label}</td>'
                f'<td style="padding:4px 0;color:{colour};font-weight:600;">{value}</td></tr>'
            )

        status_colour = "#e53935" if self.has_issues else "#43a047"
        status_text = "⚠ Issues found — review below" if self.has_issues else "✓ All good"

        stats_rows = (
            row("Orders extracted", str(self.orders_extracted))
            + row("Orders selected", str(self.orders_selected))
            + row("Written to sheets", str(len(self.written)), "#43a047")
            + row("Skipped by user", str(len(self.skipped_user)), "#f9a825")
            + row("Sheet errors", str(len(self.errors)), "#e53935" if self.errors else "#333")
        )

        written_html = ""
        if self.written:
            items = "".join(
                f'<li style="padding:2px 0;">✓ <strong>[{w["dealer"]}]</strong> '
                f'{w["order_number"]} → {w.get("range","")}</li>'
                for w in self.written
            )
            written_html = f'<h3 style="color:#43a047;">Written</h3><ul>{items}</ul>'

        unmatched_html = ""
        if self.unmatched_dealers:
            items = "".join(
                f'<li style="padding:2px 0;">Order <strong>{u.get("order_number","?")}</strong> '
                f'— dealer <code>{u.get("dealer","?")}</code> not in config. '
                f'Saved to <code>unmatched_orders.json</code>.</li>'
                for u in self.unmatched_dealers
            )
            unmatched_html = (
                f'<h3 style="color:#e65100;">⚠ Unknown Dealers</h3>'
                f'<p style="color:#555;font-size:13px;">These orders were saved locally. '
                f'Add the dealer + spreadsheet ID to <code>config.py</code> and re-run.</p>'
                f'<ul>{items}</ul>'
            )

        prices_html = ""
        if self.prices_not_found:
            items = "".join(
                f'<li style="padding:2px 0;"><code>{on}</code></li>'
                for on in self.prices_not_found
            )
            prices_html = (
                f'<h3 style="color:#e65100;">⚠ Prices Not Found in ERP</h3>'
                f'<p style="color:#555;font-size:13px;">Column G left blank for these orders. '
                f'Check the order number format or search the ERP manually.</p>'
                f'<ul>{items}</ul>'
            )

        errors_html = ""
        if self.errors:
            items = "".join(
                f'<li style="padding:2px 0;">✗ <strong>[{e["dealer"]}]</strong> '
                f'{e["order_number"]}: {e["error"]}</li>'
                for e in self.errors
            )
            errors_html = f'<h3 style="color:#e53935;">✗ Sheet Write Errors</h3><ul>{items}</ul>'

        erp_html = ""
        if self.erp_failed:
            erp_html = (
                '<h3 style="color:#e53935;">✗ ERP Connection Failed</h3>'
                '<p style="color:#555;">All prices were left blank. '
                'Check <code>ERP_EMAIL</code> / <code>ERP_PASSWORD</code> in your <code>.env</code> file.</p>'
            )

        daily_html = self._build_daily_breakdown_html()

        return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="font-family:Arial,sans-serif;max-width:640px;margin:0 auto;padding:24px;color:#333;">
  <h2 style="margin-bottom:4px;">🪟 {config.ACTIVE_COMPANY_LABEL} — Run Report</h2>
  <p style="color:#888;font-size:13px;margin-top:0;">
    {self.started_at.strftime('%Y-%m-%d')} &nbsp;|&nbsp; Duration: {self._duration()}
  </p>

  {daily_html}

  <hr style="border:none;border-top:1px solid #eee;margin:20px 0;">
  <p style="color:{status_colour};font-weight:600;margin:0 0 8px;">{status_text}</p>
  <table>{stats_rows}</table>
  <hr style="border:none;border-top:1px solid #eee;margin:16px 0;">
  {unmatched_html}
  {prices_html}
  {errors_html}
  {erp_html}
  <p style="color:#bbb;font-size:11px;margin-top:32px;">
    Sent by {config.ACTIVE_COMPANY_LABEL} Order Automation
  </p>
</body>
</html>"""
    # ── daily breakdown HTML ──────────────────────────────────────────────────

    def _build_daily_breakdown_html(self) -> str:
        """
        Build a WhatsApp-style dark green summary card for each date.
        Uses Decimal arithmetic throughout to guarantee exact totals.
        Since the script runs once per day there is normally one card.
        """
        if not self.written:
            return ""

        from datetime import datetime as dt
        from decimal import Decimal, ROUND_HALF_UP

        TWO = Decimal("0.01")  # rounding target

        def _to_dec(v) -> Decimal:
            """Safely convert any value to Decimal, defaulting to 0."""
            try:
                return Decimal(str(v)) if v is not None else Decimal(0)
            except Exception:
                return Decimal(0)

        cards = []
        for date_key, day_orders in self._daily_groups():
            try:
                pretty_date = dt.strptime(date_key, "%Y-%m-%d").strftime("%B %-d, %Y")
            except ValueError:
                pretty_date = date_key

            day_qty     = 0
            day_revenue = Decimal(0)
            day_acc: dict[str, int] = {}   # accessory totals
            order_rows: list[str] = []

            for o in day_orders:
                # Shorten order number: ORD-0188 → 0188, ON9265 → ON9265
                raw_on   = (o.get("order_number") or "?").strip()
                short_on = raw_on.split("-")[-1] if "-" in raw_on else raw_on

                dealer   = o.get("dealer", "?")
                customer = o.get("customer_name", "?")
                qty      = int(o.get("qty") or 0)
                amt      = _to_dec(o.get("price")).quantize(TWO, rounding=ROUND_HALF_UP)
                acc_str  = self._accessories_str(o)

                order_rows.append(
                    f'<div style="padding:3px 0;font-size:13px;">'
                    f'<span style="opacity:0.72;">{short_on}</span>'
                    f' <span style="opacity:0.5;">|</span> '
                    f'<strong>{dealer}</strong>'
                    f' <span style="opacity:0.5;">|</span> '
                    f'{customer}: {qty}{acc_str}'
                    f'&nbsp;&nbsp;<span style="opacity:0.82;">Revenue: ${amt:,}</span>'
                    f'</div>'
                )

                day_qty     += qty
                day_revenue += amt

                for key in ("motors", "remotes", "solars", "chargers"):
                    v = o.get(key)
                    if v and int(v) > 0:
                        day_acc[key] = day_acc.get(key, 0) + int(v)

            # Accessory totals string for the footer
            acc_parts = []
            for key, singular, plural in [
                ("motors",   "motor",   "motors"),
                ("remotes",  "remote",  "remotes"),
                ("solars",   "solar",   "solars"),
                ("chargers", "charger", "chargers"),
            ]:
                v = day_acc.get(key, 0)
                if v:
                    acc_parts.append(f"{v} {singular if v == 1 else plural}")
            acc_footer = f" ({', '.join(acc_parts)})" if acc_parts else ""

            rows_html = "\n".join(order_rows)
            cards.append(
                f'<div style="background:#1b5e3b;border-radius:14px;'
                f'padding:16px 18px;margin:10px 0;color:#fff;">'
                f'<div style="font-weight:700;font-size:16px;margin-bottom:12px;">'
                f'{pretty_date}</div>'
                f'{rows_html}'
                f'<div style="border-top:1px solid rgba(255,255,255,0.25);'
                f'margin-top:14px;padding-top:12px;font-size:13px;line-height:1.9;">'
                f'<strong>Total Blinds: {day_qty}{acc_footer}</strong><br>'
                f'<strong>Daily Revenue: ${day_revenue:,}</strong>'
                f'</div></div>'
            )

        return (
            '<h3 style="color:#1b5e3b;margin-top:0;margin-bottom:4px;">'
            '\U0001f4c5 Daily Summary</h3>'
            + "\n".join(cards)
            + '<div style="margin-bottom:8px;"></div>'
        )

    # ── save unmatched orders ─────────────────────────────────────────────────


    def save_unmatched(self, orders: list[dict]) -> None:
        """
        Append unknown-dealer orders to unmatched_orders.json in the project root.
        Existing entries are preserved so nothing is ever lost.
        """
        if not orders:
            return

        out_path = Path(__file__).parent / "unmatched_orders.json"

        existing: list[dict] = []
        if out_path.exists():
            try:
                existing = json.loads(out_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                existing = []

        timestamp = datetime.now().isoformat(timespec="seconds")
        for order in orders:
            existing.append({**order, "_saved_at": timestamp})

        out_path.write_text(
            json.dumps(existing, indent=2, default=str),
            encoding="utf-8",
        )
        print(f"  💾 {len(orders)} unmatched order(s) saved → {out_path.name}")

    # ── send all ──────────────────────────────────────────────────────────────

    def dispatch(self) -> None:
        """Fire both the macOS notification and the email."""
        self.send_macos_notification()
        self.send_email()
