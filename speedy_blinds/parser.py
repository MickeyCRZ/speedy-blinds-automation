"""
parser.py — Groq-powered WhatsApp order parser
================================================
Sends raw text to Groq (Llama 3.3 70B) and returns a list of structured order dicts.
"""

from __future__ import annotations

import json
import re
import textwrap

from groq import Groq

import config
from config import GROQ_API_KEY, GROQ_MODEL

# Initialise once
_client = Groq(api_key=GROQ_API_KEY)


def _get_system_prompt() -> str:
    dealers = list({v for v in config.DEALER_ALIASES.values()})
    return textwrap.dedent(f"""
        You are a data-extraction assistant for a window-blinds distribution company.

        Extract EVERY completed order AND every rework entry from the WhatsApp message text.
        Return ONLY a valid JSON array — no markdown, no explanation, no trailing text.

        Each element in the array must be a JSON object with EXACTLY these keys:
        {{
            "type":          "order" or "rework",
            "dealer":        "<one of: {', '.join(sorted(dealers))}>",
            "date":          "<date in YYYY-MM-DD format, e.g. 2026-05-03>",
            "order_number":  "<see rules below>",
            "customer_name": "<customer name, or null for reworks>",
            "qty":           <integer total number of blinds, or null for reworks>,
            "amount":        <numeric dollar amount without $ symbol, e.g. 140.49>,
            "motors":        <integer count of motorised/electric blinds, or null>,
            "remotes":       <integer count of remotes included, or null>,
            "solars":        <integer count of solar panels included, or null>,
            "chargers":      <integer count of chargers included, or null>
        }}

        Rules for ORDERS (type="order"):
        - order_number: format strictly as ORD-XXXX (e.g. "ON 121" → "ORD-0121").
        - amount: the dollar amount stated in the message for this order.
        - customer_name, qty, motors, remotes, solars, chargers as seen in message.

        Rules for REWORKS (type="rework"):
        - Reworks look like: RW133-Phil-$50  or  RW 45 - Alen - 30  or similar variations.
        - order_number: keep as-is with RW prefix, e.g. "RW133". Do NOT convert to ORD-.
        - dealer: resolve from the name after the first dash.
        - amount: the dollar amount after the second dash (strip $ symbol).
        - customer_name: null.
        - qty: null.
        - motors, remotes, solars, chargers: null.

        General rules:
        - If a field cannot be determined, use null.
        - Dates must always be YYYY-MM-DD. If only day and month given (e.g. "9 June"), assume year 2026.
        - Dealer name must exactly match one of the allowed values (case-sensitive).
        - If the dealer name in the message is a known alias/abbreviation (e.g. "vtt" → "VT Thomas"), resolve it.
        - Strip any currency symbols from amount.
        - Do NOT include payment entries, totals, or summary rows — only per-order/per-rework rows.
        - "Total blinds" lines and similar summaries must be ignored.
    """).strip()


def _resolve_dealer(raw: str | None) -> str | None:
    """Map whatever the LLM returned to a canonical dealer name."""
    if raw is None:
        return None
    return config.DEALER_ALIASES.get(raw.strip().lower(), raw.strip())


def _parse_response(text: str) -> list[dict]:
    """Extract JSON array from response, tolerating minor formatting noise."""
    # Strip markdown code fences if present
    text = re.sub(r"```(?:json)?", "", text).strip()
    # Find the first [ ... ] block
    match = re.search(r"\[.*\]", text, re.DOTALL)
    if not match:
        raise ValueError(f"No JSON array found in response:\n{text[:500]}")
    return json.loads(match.group())


def parse_orders(raw_text: str) -> list[dict]:
    """
    Send raw WhatsApp text to Groq and return a list of order dicts.

    Each dict has keys:
        dealer, date, order_number, customer_name, qty, amount,
        motors, remotes, solars, chargers
    (price is added later by erp.enrich_orders)
    """
    if not GROQ_API_KEY:
        raise RuntimeError(
            "GROQ_API_KEY is not set. Add it to your .env file."
        )

    messages = [
        {"role": "system", "content": _get_system_prompt()},
        {"role": "user",   "content": raw_text},
    ]

    # First attempt
    response = _client.chat.completions.create(
        model=GROQ_MODEL,
        messages=messages,
        temperature=0,      # deterministic output
        max_tokens=4096,
    )
    reply = response.choices[0].message.content or ""

    try:
        orders = _parse_response(reply)
    except (ValueError, json.JSONDecodeError):
        # Retry with a stricter nudge
        messages.append({"role": "assistant", "content": reply})
        messages.append({
            "role": "user",
            "content": (
                "Your previous response was not valid JSON. "
                "Return ONLY the raw JSON array, nothing else."
            ),
        })
        response = _client.chat.completions.create(
            model=GROQ_MODEL,
            messages=messages,
            temperature=0,
            max_tokens=4096,
        )
        reply = response.choices[0].message.content or ""
        orders = _parse_response(reply)

    # Normalise each entry
    normalised = []
    for order in orders:
        is_rework = str(order.get("type") or "").lower() == "rework"
        order["type"] = "rework" if is_rework else "order"

        order["dealer"] = _resolve_dealer(order.get("dealer"))
        if order["dealer"] not in config.DEALER_SHEETS and config.ACTIVE_COMPANY_LABEL == "Inspira Blinds":
            order["original_dealer"] = order["dealer"] or "Unknown"
            order["dealer"] = "Harvinder"
            order["unknown_dealer_fallback"] = True

        o_num = str(order.get("order_number") or "").strip()
        if is_rework:
            # Keep RW prefix as-is — do NOT normalise to ORD-
            if o_num and not o_num.upper().startswith("RW"):
                digits = re.sub(r"\D", "", o_num)
                order["order_number"] = f"RW{digits}" if digits else o_num
            # Rework price comes from the text (stored in amount) — copy to price
            order["price"] = order.get("amount")
        else:
            # Regular order: normalise to ORD-XXXX
            if o_num and o_num.lower() != "none":
                digits = re.sub(r"\D", "", o_num)
                if digits:
                    order["order_number"] = f"ORD-{int(digits):04d}"

        normalised.append(order)

    return normalised
