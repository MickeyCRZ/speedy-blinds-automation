"""
erp.py — Speedy Blinds / Inspira Blinds ERP REST API client
============================================================
Authenticates against the active company's ERP and fetches order prices
(total_price) by order number, searching across all configured tenants
in parallel.

The active company (base URL, tenant IDs, token cache keys) is set by
calling config.set_company() in main.py before this module is used.

Token is cached in .env under the company-specific key names and reused
for up to 30 days before triggering a fresh login.
"""

from __future__ import annotations

import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

import config

# Path to project-root .env (one level above this file)
_ENV_PATH = Path(__file__).parent.parent / ".env"
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

_SESSION = requests.Session()
_SESSION.verify = False   # ERP SSL cert is expired — disable verification
_SESSION.headers.update({"Accept": "application/json", "Content-Type": "application/json"})


# ---------------------------------------------------------------------------
# Token management
# ---------------------------------------------------------------------------

def _read_cached_token() -> tuple[str, str]:
    """Read the active company's token and expiry from the .env file."""
    import config
    token_key   = config.ERP_TOKEN_KEY
    expires_key = config.ERP_TOKEN_EXPIRES_KEY
    token = ""
    expires_at = ""
    if _ENV_PATH.exists():
        text = _ENV_PATH.read_text(encoding="utf-8")
        for line in text.splitlines():
            if line.startswith(f"{token_key}="):
                token = line[len(f"{token_key}="):].strip()
            elif line.startswith(f"{expires_key}="):
                expires_at = line[len(f"{expires_key}="):].strip()
    return token, expires_at


def _write_token_to_env(token: str, expires_at: str) -> None:
    """Upsert the active company's token and expiry in the .env file."""
    import config
    token_key   = config.ERP_TOKEN_KEY
    expires_key = config.ERP_TOKEN_EXPIRES_KEY

    if not _ENV_PATH.exists():
        _ENV_PATH.write_text("", encoding="utf-8")

    text = _ENV_PATH.read_text(encoding="utf-8")
    lines = text.splitlines()

    def _upsert(lines: list[str], key: str, value: str) -> list[str]:
        pattern = re.compile(rf"^{re.escape(key)}=")
        replaced = False
        result = []
        for line in lines:
            if pattern.match(line):
                result.append(f"{key}={value}")
                replaced = True
            else:
                result.append(line)
        if not replaced:
            result.append(f"{key}={value}")
        return result

    lines = _upsert(lines, token_key, token)
    lines = _upsert(lines, expires_key, expires_at)
    _ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _is_token_valid(token: str, expires_at: str) -> bool:
    """Return True if token is non-empty and has not yet expired."""
    if not token or not expires_at:
        return False
    try:
        expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        # Add a 5-minute buffer so we don't use a token that's about to expire
        return datetime.now(timezone.utc) < expiry
    except ValueError:
        return False


def _login() -> str:
    """
    Authenticate against the active company's ERP and return a fresh Bearer token.
    Also persists the token + expiry to .env under the company-specific key names.
    Handles session_conflict by retrying with confirm_logout=True.
    """
    if not config.ERP_EMAIL or not config.ERP_PASSWORD:
        raise RuntimeError(
            "ERP_EMAIL and ERP_PASSWORD must be set in your .env file."
        )

    url = f"{config.ERP_BASE_URL}/auth/login"

    def _attempt(extra: dict) -> requests.Response:
        payload = {
            "email":    config.ERP_EMAIL,
            "password": config.ERP_PASSWORD,
            **extra,
        }
        resp = _SESSION.post(url, json=payload, timeout=30)
        return resp

    # First attempt — no special flags
    resp = _attempt({})

    # 409 = session conflict (already signed in on another device)
    if resp.status_code not in (200, 409):
        raise RuntimeError(
            f"ERP login failed ({resp.status_code}): {resp.text[:300]}"
        )

    body = resp.json()

    # If session conflict (409 or body flag), retry forcing logout
    if resp.status_code == 409 or body.get("session_conflict"):
        print("  ⚠️  ERP session conflict — forcing logout of existing session...")
        resp = _attempt({"force_login": True})
        if resp.status_code != 200:
            raise RuntimeError(
                f"ERP login (force) failed ({resp.status_code}): {resp.text[:300]}"
            )
        body = resp.json()
        if body.get("session_conflict"):
            raise RuntimeError(
                "ERP login rejected — session conflict persists. "
                "Please log out of the ERP browser session manually and retry."
            )

    data  = body.get("data", {})
    token = data.get("access_token", "")
    expires_at = data.get("expires_at", "")

    if not token:
        raise RuntimeError(f"ERP login response contained no access_token: {resp.text[:300]}")

    _write_token_to_env(token, expires_at)
    return token


def get_token() -> str:
    """
    Return a valid Bearer token for the active company, using cache if available.
    Logs in fresh if the cached token is missing or expired.
    """
    import config
    token_key   = config.ERP_TOKEN_KEY
    expires_key = config.ERP_TOKEN_EXPIRES_KEY

    # First priority: value already loaded in process env (same run)
    token      = os.getenv(token_key, "")
    expires_at = os.getenv(expires_key, "")

    # Second priority: read from .env file on disk
    if not _is_token_valid(token, expires_at):
        token, expires_at = _read_cached_token()

    if _is_token_valid(token, expires_at):
        return token

    # Cache miss or expired — re-authenticate
    print(f"  🔑 ERP token missing/expired — logging in to {config.ERP_BASE_URL}...")
    return _login()


# ---------------------------------------------------------------------------
# Price lookup
# ---------------------------------------------------------------------------

def _search_order_in_tenant(
    order_number: str,
    token: str,
    tenant_id: int,
) -> Optional[float]:
    """
    Search for `order_number` within a single tenant scope.
    Returns total_price (float) if found, else None.
    """
    import config
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Active-Tenant-Id": str(tenant_id),
    }
    params = {
        "search": order_number,
        "per_page": 20,
    }
    url = f"{config.ERP_BASE_URL}/admin/orders"

    try:
        resp = _SESSION.get(url, headers=headers, params=params, timeout=20)
    except requests.RequestException as exc:
        # Network error — don't crash the whole run
        print(f"  ⚠️  ERP request error (tenant {tenant_id}): {exc}")
        return None

    if resp.status_code == 401:
        raise RuntimeError("ERP token rejected (401 Unauthorized). Re-run to force re-login.")
    if resp.status_code != 200:
        print(f"  ⚠️  ERP returned {resp.status_code} for tenant {tenant_id}: {resp.text[:200]}")
        return None

    orders = resp.json().get("data", [])
    if not isinstance(orders, list):
        return None

    # Find the exact order number match (search is fuzzy on the server side).
    # ERP stores numbers as "ORD-0246"; the WhatsApp message gives just "0246".
    # Normalise both sides by stripping "ORD-" and leading zeros for comparison.
    def _norm(n: str | None) -> str:
        if not n:
            return "0"
        digits = re.sub(r"\D", "", str(n))
        return digits.lstrip("0") or "0"

    target = _norm(order_number)
    for order in orders:
        erp_num = str(order.get("order_number", "")).strip()
        if _norm(erp_num) == target:
            price = order.get("total_price")
            if price is not None:
                return float(price)

    return None


def fetch_price(order_number: str, token: str) -> Optional[float]:
    """
    Look up the total_price for `order_number` across all configured tenants
    for the active company in parallel. Returns the first non-None price found,
    or None if not found in any tenant.
    """
    import config
    tenant_ids = config.ERP_TENANT_IDS
    if not tenant_ids:
        raise RuntimeError("ERP_TENANT_IDS is empty. Add tenant IDs to config.py.")

    with ThreadPoolExecutor(max_workers=len(tenant_ids)) as pool:
        futures = {
            pool.submit(_search_order_in_tenant, order_number, token, tid): tid
            for tid in tenant_ids
        }
        for future in as_completed(futures):
            result = future.result()
            if result is not None:
                return result

    return None


def enrich_orders(orders: list[dict]) -> list[dict]:
    """
    Add a 'price' key to every order dict by looking it up from the active
    company's ERP.

    Fetches the token once, then looks up each order in parallel across
    all tenants. If all lookups return 401 (stale token), automatically
    re-authenticates and retries once. Orders where no price is found get price=None.

    Returns the same list (mutated in-place) for convenience.
    """
    import config
    if not orders:
        return orders

    def _run_lookups(token: str) -> tuple[dict, list[Exception]]:
        """Fan out lookups, return (order→price map, list of 401 errors)."""
        prices: dict = {}
        errors: list = []
        with ThreadPoolExecutor(max_workers=min(10, len(orders))) as pool:
            future_to_order = {
                pool.submit(fetch_price, order.get("order_number", ""), token): order
                for order in orders
            }
            for future in as_completed(future_to_order):
                order = future_to_order[future]
                try:
                    prices[id(order)] = future.result()
                except RuntimeError as exc:
                    if "401" in str(exc):
                        errors.append(exc)
                    else:
                        print(f"  ⚠️  Price lookup failed for {order.get('order_number')}: {exc}")
                    prices[id(order)] = None
        return prices, errors

    token = get_token()
    print(f"  🔍 Looking up prices for {len(orders)} order(s) across "
          f"{len(config.ERP_TENANT_IDS)} tenant(s) on {config.ERP_BASE_URL}...")

    prices, errors = _run_lookups(token)

    # If ALL lookups returned 401 (stale cached token), force fresh login & retry
    if errors and len(errors) == len(orders):
        print("  🔄 Token rejected (401) — forcing fresh login and retrying...")
        # Clear cached token to force _login()
        _write_token_to_env("", "")
        token = _login()
        prices, _ = _run_lookups(token)

    for order in orders:
        order["price"] = prices.get(id(order))

    found = sum(1 for o in orders if o.get("price") is not None)
    missing = len(orders) - found
    print(f"  ✓ Prices resolved: {found} found, {missing} not found in ERP")

    return orders
