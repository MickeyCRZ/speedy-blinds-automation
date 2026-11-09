"""
dealers_manager.py — Dynamic Dealer & Sheet Configuration Manager
=================================================================
Manages dealer names, Google Sheet URLs/IDs, and aliases across all companies
(Speedy Blinds, Inspira Blinds, Houston Blinds). Persists data to dealers.json
so changes survive restarts and update the runtime config in real time.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Optional

_ROOT_DIR = Path(__file__).parent.parent.resolve()
_DEALERS_JSON_PATH = _ROOT_DIR / "dealers.json"

# Default dealers if dealers.json does not exist yet
_DEFAULT_DEALERS: dict[str, dict[str, Any]] = {
    "speedy": {
        "dealers": {
            "VT Thomas": {"sheet_id": "1KaauO3BEkZF3A_hW_3zgH3VE6jBME21UY0fF5GIoiN4", "aliases": ["thomas", "vtt"], "sheet_type": "new"},
            "Alen": {"sheet_id": "1pkyF_19FWWSbjxvu8Ybht1u9kKu7Eyec5VhqDdcdF1A", "aliases": ["allen"], "sheet_type": "old"},
            "Mike": {"sheet_id": "1zT3LGa38Khg3CPH6BMk3N7THlshms8w5GpGGULxqTGU", "aliases": ["michael"], "sheet_type": "new"},
            "Ayush": {"sheet_id": "1PJ3Um_RUf95dHw_imbDS4IKR6HLN9AgdOolE5TKmXHA", "aliases": [], "sheet_type": "new"},
            "Phil": {"sheet_id": "1hNaD9gixkWfQdrQM4Lnc-YtjAfT6GiJBhNUnRw1klWY", "aliases": ["philip", "philemon", "philemon chacko", "chacko"], "sheet_type": "old"},
            "Han": {"sheet_id": "1pr7_aXotzMTPxQYXcCGwyc2dhODZpcxOLa7ez2Bn5Hc", "aliases": [], "sheet_type": "new"},
            "Jubin": {"sheet_id": "1ZsTPaFqyDtMITPlwICmIrKolRv_F6ALvimZsOGjydYE", "aliases": [], "sheet_type": "new"},
            "Komal": {"sheet_id": "1T3WNko2mYnG6uUBUtHkKlu1lAglnHU6wiI8Xg7DQfxM", "aliases": ["kamal", "kamalpreet", "kamalpreet singh", "kamal preet", "komalpreet", "komalpreet singh", "komal preet"], "sheet_type": "new"},
            "Aman": {"sheet_id": "1xVXiuPsymDtC_Mtm8KikX8r4aigFxdkJVApYJeCzDVc", "aliases": [], "sheet_type": "new"},
            "Tom": {"sheet_id": "10M5u4CKllu69fVCXH9V2iZ2RFUZuc1X8CCGf1ueIFeo", "aliases": [], "sheet_type": "new"},
            "Shawn": {"sheet_id": "1GFouQwTS5MR6z5beZxEeGUz-I_0icLPX9KVw0YgKgrI", "aliases": [], "sheet_type": "new"},
            "Mathew": {"sheet_id": "1oBayeMSr6VcroVqKSBLCe9bUqwoR1Peem9JEbpAeYeo", "aliases": ["matthew", "sneha"], "sheet_type": "new"},
            "Akash": {"sheet_id": "1fZh-58DzPYMiGvXdPfw2TYPoWPPrsJhJZlqnHH8f3D4", "aliases": [], "sheet_type": "new"},
            "Joseph": {"sheet_id": "12f1_iP6O_Rnp9Ze4bKbjF1zBMYeWxJFutvNc1OzZXb0", "aliases": [], "sheet_type": "new"},
            "Nithin": {"sheet_id": "1kstbKMPhgnf3vPuZ7EEpL8cJrgaLQdChEu5eJ5AGGNA", "aliases": [], "sheet_type": "new"},
            "Jeff": {"sheet_id": "1o8DwXb-yN_xjLp0L8xIA29swgFE9aYzUuD6gq3BeibU", "aliases": ["jeffrin", "jeffrin francis", "francis"], "sheet_type": "new"},
            "Karanveer": {"sheet_id": "1ZNVGZWBxTYxHjQL-nh5bcMfZCni5Xh3dD60fYhHo8X4", "aliases": [], "sheet_type": "new"},
            "Hammad": {"sheet_id": "1pbI7cMEbZL1zX_ddrE0cLpU3KdGdsiHDt21EfXSW3WY", "aliases": [], "sheet_type": "new"},
            "Pawanpreet": {"sheet_id": "1532d1eFLwxrOM03tAxNN0W1l3uFTNNddrJ0eLoBTP3E", "aliases": ["pawan", "pavan", "pawanpreet singh", "pawan singh"], "sheet_type": "new"},
        }
    },
    "inspira": {
        "dealers": {
            "Aman": {"sheet_id": "1IuOEqWFBS5McgDtgfu1DHsh_xaS9Q0zD77BLNkowa10", "aliases": ["haraman", "amandeep"], "sheet_type": "new"},
            "Harvinder": {"sheet_id": "1WGxd4xrNOR4JJQG2KJRHZ60xWVM6VaGl06id1sO9e5k", "aliases": [], "sheet_type": "new"},
            "Heman": {"sheet_id": "1hVqK0SlfXZKhrDiSLOwaw3eAZ9GEqegBiAhYSwwCpIk", "aliases": ["heman walia", "walia"], "sheet_type": "new"},
            "Jerin": {"sheet_id": "1UY4Nv9IXUYGfea4873dyHpfz6GKLLDnaWNgvC8p_iQ4", "aliases": [], "sheet_type": "new"},
            "Rahul": {"sheet_id": "1UCCKwchD1GTwh-2B3frixbMqz7kTGwnxhaZnLzN3BAE", "aliases": ["rahul bansal", "bansal"], "sheet_type": "new"},
            "Surinder": {"sheet_id": "1fTMRPODsdmyXPbjgRRTr_2oOOtv_WWlS-N9fwisvv8Y", "aliases": [], "sheet_type": "new"},
            "AJ": {"sheet_id": "1pM0QWHUHF2DQ0STfjUflOaEz_-pxmA-RRS_kJzUjVlA", "aliases": [], "sheet_type": "new"},
            "Sidhu": {"sheet_id": "1qb1y18Z8H3zYLkDCeJ161mxr0EG9Vxhn5MT94jJVOEQ", "aliases": [], "sheet_type": "new"},
            "Navtej": {"sheet_id": "1Blk9GJYIIgbCTBUUEJCPIhBJd9ZSuQvTtCQZyQdDfOo", "aliases": [], "sheet_type": "new"},
            "Jaspreet": {"sheet_id": "1meA8yAZtkEWChPOg3gOaJS6kRH9_Wx9dZiLQLJLGwro", "aliases": [], "sheet_type": "new"},
            "Chirag": {"sheet_id": "1svAPPOB8AGd05Q8lVJYr3RGY8OozIKNXFtT7O1_c4jw", "aliases": [], "sheet_type": "new"},
            "Sarbjeet": {"sheet_id": "1bi5NOnmdR1VCfKEg3wi5CO6onyqPA6Z3TNk_Khq17Ds", "aliases": [], "sheet_type": "new"},
            "Qazi": {"sheet_id": "1M-4B9l9-iXiaxskcBssWRF_RpO7Rvr5oucz93MsNvXU", "aliases": [], "sheet_type": "new"},
        }
    },
    "houston": {
        "dealers": {
            "Rmit": {"sheet_id": "1vf8lvNBBFLCeTJlUIwQK5i6Qb0ay9KbyCNGnfRFEiP8", "aliases": [], "sheet_type": "new"},
            "Mike": {"sheet_id": "1rxUmGnhzi26iKpAK5n8TcsvGui6pcHQAGanCtB0K9XQ", "aliases": [], "sheet_type": "new"},
            "Jubin": {"sheet_id": "1JcD6cjxxD7h9JtvrtBQCgU0oVDCq79ispfGRD3FyhsM", "aliases": [], "sheet_type": "new"},
            "Victor": {"sheet_id": "1QqRYShFpZFO9kLhNSjc3VtoI9rP-aIZ6l-Fu592YQAs", "aliases": [], "sheet_type": "new"},
        }
    },
    "sheet_configs": {
        "old": {
            "name": "Old Sheet Format",
            "tab_name": "2026",
            "month_col": "A",
            "date_col": "C",
            "order_col": "D",
            "customer_col": "E",
            "qty_col": "F",
            "price_col": "G",
            "eft_col": "H",
            "eft_alt_col": "J",
            "cash_col": "I",
            "cash_alt_col": "L",
            "alt_col": "J",
            "comments_col": "K",
        },
        "new": {
            "name": "New Sheet Format",
            "tab_name": "2026",
            "month_col": "B",
            "date_col": "C",
            "order_col": "D",
            "customer_col": "E",
            "qty_col": "F",
            "price_col": "G",
            "eft_col": "H",
            "eft_alt_col": "J",
            "cash_col": "I",
            "cash_alt_col": "L",
            "alt_col": "J",
            "comments_col": "K",
        },
    },
}


def extract_spreadsheet_id(url_or_id: str) -> str:
    """Extract Google Spreadsheet ID from a full URL or validate bare ID."""
    raw = (url_or_id or "").strip()
    match = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", raw)
    if match:
        return match.group(1)
    # If already a bare ID
    cleaned = re.sub(r"[^a-zA-Z0-9-_]", "", raw)
    return cleaned or raw


def load_dealers_data() -> dict[str, dict[str, Any]]:
    """Load dealers from dealers.json or create it from defaults."""
    if _DEALERS_JSON_PATH.exists():
        try:
            with open(_DEALERS_JSON_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                # Ensure all 3 companies exist
                for k in ("speedy", "inspira", "houston"):
                    if k not in data or "dealers" not in data[k]:
                        data[k] = _DEFAULT_DEALERS.get(k, {"dealers": {}})
                
                # Ensure sheet_configs exists
                if "sheet_configs" not in data:
                    data["sheet_configs"] = _DEFAULT_DEALERS["sheet_configs"]
                
                # Ensure every dealer has sheet_type
                changed = False
                for k in ("speedy", "inspira", "houston"):
                    for dname, dinfo in data[k]["dealers"].items():
                        if "sheet_type" not in dinfo:
                            dinfo["sheet_type"] = "old" if dname in ["Phil", "Alen"] else "new"
                            changed = True
                if changed:
                    save_dealers_data(data)
                return data
        except Exception:
            pass

    # Save defaults
    save_dealers_data(_DEFAULT_DEALERS)
    return _DEFAULT_DEALERS


def save_dealers_data(data: dict[str, Any]) -> None:
    """Save dealers data to dealers.json."""
    with open(_DEALERS_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def sync_to_config_module() -> None:
    """Sync dealers from dealers.json into config module variables."""
    try:
        import config
    except ImportError:
        return

    data = load_dealers_data()

    def build_maps(corp_key: str):
        corp_dealers = data.get(corp_key, {}).get("dealers", {})
        sheets_map = {}
        aliases_map = {}
        for dname, dinfo in corp_dealers.items():
            sid = dinfo.get("sheet_id", "")
            sheets_map[dname] = sid
            # Canonical name is always an alias for itself
            aliases_map[dname.strip().lower()] = dname
            for a in dinfo.get("aliases", []):
                a_clean = a.strip().lower()
                if a_clean:
                    aliases_map[a_clean] = dname
        return sheets_map, aliases_map

    speedy_sheets, speedy_aliases = build_maps("speedy")
    inspira_sheets, inspira_aliases = build_maps("inspira")
    houston_sheets, houston_aliases = build_maps("houston")

    config.SPEEDY_DEALER_SHEETS = speedy_sheets
    config.SPEEDY_DEALER_ALIASES = speedy_aliases
    config.INSPIRA_DEALER_SHEETS = inspira_sheets
    config.INSPIRA_DEALER_ALIASES = inspira_aliases
    config.HOUSTON_DEALER_SHEETS = houston_sheets
    config.HOUSTON_DEALER_ALIASES = houston_aliases

    # Also update active company pointers
    active_key = getattr(config, "ACTIVE_COMPANY_KEY", "speedy")
    if active_key == "inspira":
        config.DEALER_SHEETS = inspira_sheets
        config.DEALER_ALIASES = inspira_aliases
    elif active_key == "houston":
        config.DEALER_SHEETS = houston_sheets
        config.DEALER_ALIASES = houston_aliases
    else:
        config.DEALER_SHEETS = speedy_sheets
        config.DEALER_ALIASES = speedy_aliases


def get_all_dealers_flat(company_filter: str | None = None) -> list[dict[str, Any]]:
    """Return flat list of all dealers with metadata, sheet URLs, and aliases."""
    data = load_dealers_data()
    company_labels = {
        "speedy": "Speedy Blinds",
        "inspira": "Inspira Blinds",
        "houston": "Houston Blinds",
    }
    dealers_list = []

    companies = [company_filter] if company_filter and company_filter in data else ["speedy", "inspira", "houston"]

    for ckey in companies:
        corp = data.get(ckey, {})
        dealers = corp.get("dealers", {})
        for dname, dinfo in sorted(dealers.items()):
            sid = dinfo.get("sheet_id", "")
            aliases = dinfo.get("aliases", [])
            sheet_type = dinfo.get("sheet_type", "new")
            sheet_url = f"https://docs.google.com/spreadsheets/d/{sid}/edit" if sid else ""
            dealers_list.append({
                "company_key": ckey,
                "company_label": company_labels.get(ckey, ckey.title()),
                "dealer_name": dname,
                "sheet_id": sid,
                "sheet_url": sheet_url,
                "sheet_type": sheet_type,
                "aliases": aliases,
                "aliases_str": ", ".join(aliases),
            })

    return dealers_list


def add_or_update_dealer(
    company_key: str,
    dealer_name: str,
    sheet_url_or_id: str,
    aliases: list[str] | str,
    original_name: str | None = None,
    sheet_type: str | None = None,
) -> dict[str, Any]:
    """Add a new dealer or update an existing dealer."""
    company_key = company_key.strip().lower()
    data = load_dealers_data()
    if company_key not in data:
        raise ValueError(f"Invalid company '{company_key}'. Allowed: speedy, inspira, houston")

    clean_name = dealer_name.strip()
    if not clean_name:
        raise ValueError("Dealer name cannot be empty.")

    sheet_id = extract_spreadsheet_id(sheet_url_or_id)
    if not sheet_id:
        raise ValueError("Valid Google Sheet URL or Spreadsheet ID is required.")

    # Parse aliases
    if isinstance(aliases, str):
        alias_list = [a.strip().lower() for a in aliases.split(",") if a.strip()]
    else:
        alias_list = [str(a).strip().lower() for a in aliases if str(a).strip()]

    # Deduplicate aliases and avoid matching canonical name
    seen = set()
    final_aliases = []
    canonical_lower = clean_name.lower()
    for a in alias_list:
        if a and a != canonical_lower and a not in seen:
            seen.add(a)
            final_aliases.append(a)

    corp_dealers = data[company_key]["dealers"]

    # Determine sheet_type: passed arg -> existing -> default
    existing_st = "new"
    if original_name and original_name in corp_dealers:
        existing_st = corp_dealers[original_name].get("sheet_type", "new")
    elif clean_name in corp_dealers:
        existing_st = corp_dealers[clean_name].get("sheet_type", "new")
    final_st = sheet_type if sheet_type in ("old", "new") else existing_st

    # If renaming an existing dealer, remove old name first
    if original_name and original_name in corp_dealers and original_name != clean_name:
        del corp_dealers[original_name]

    corp_dealers[clean_name] = {
        "sheet_id": sheet_id,
        "aliases": final_aliases,
        "sheet_type": final_st,
    }

    save_dealers_data(data)
    sync_to_config_module()

    return {
        "success": True,
        "company_key": company_key,
        "dealer_name": clean_name,
        "sheet_id": sheet_id,
        "sheet_url": f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit",
        "sheet_type": final_st,
        "aliases": final_aliases,
    }


def delete_dealer(company_key: str, dealer_name: str) -> dict[str, Any]:
    """Remove a dealer from a company."""
    company_key = company_key.strip().lower()
    data = load_dealers_data()
    if company_key not in data:
        raise ValueError(f"Invalid company '{company_key}'")

    clean_name = dealer_name.strip()
    corp_dealers = data[company_key]["dealers"]

    if clean_name not in corp_dealers:
        raise ValueError(f"Dealer '{clean_name}' not found under company '{company_key}'")

    del corp_dealers[clean_name]
    save_dealers_data(data)
    sync_to_config_module()

    return {
        "success": True,
        "company_key": company_key,
        "deleted_dealer": clean_name,
    }


def get_sheet_configs() -> dict[str, Any]:
    """Return the Old and New sheet column mapping configurations."""
    data = load_dealers_data()
    return data.get("sheet_configs", _DEFAULT_DEALERS["sheet_configs"])


def save_sheet_configs(configs: dict[str, Any]) -> dict[str, Any]:
    """Update and persist Old and New sheet configurations."""
    data = load_dealers_data()
    current = data.get("sheet_configs", {})

    for st in ("old", "new"):
        if st in configs and isinstance(configs[st], dict):
            if st not in current:
                current[st] = {}
            for field in (
                "name", "tab_name",
                "month_col", "date_col", "order_col", "customer_col", "qty_col", "price_col",
                "eft_col", "eft_alt_col", "cash_col", "cash_alt_col", "alt_col", "comments_col"
            ):
                if field in configs[st]:
                    val = str(configs[st][field]).strip()
                    if field.endswith("_col") and val:
                        val = val.upper()
                    current[st][field] = val

    data["sheet_configs"] = current
    save_dealers_data(data)
    return current


def update_dealer_sheet_type(company_key: str, dealer_name: str, sheet_type: str) -> dict[str, Any]:
    """Change a dealer's sheet category ('old' vs 'new')."""
    company_key = company_key.strip().lower()
    sheet_type = sheet_type.strip().lower()
    if sheet_type not in ("old", "new"):
        raise ValueError(f"Invalid sheet_type '{sheet_type}'. Must be 'old' or 'new'.")

    data = load_dealers_data()
    if company_key not in data or "dealers" not in data[company_key]:
        raise ValueError(f"Company '{company_key}' not found.")

    clean_name = dealer_name.strip()
    corp_dealers = data[company_key]["dealers"]
    if clean_name not in corp_dealers:
        raise ValueError(f"Dealer '{clean_name}' not found under company '{company_key}'.")

    corp_dealers[clean_name]["sheet_type"] = sheet_type
    save_dealers_data(data)

    return {
        "success": True,
        "company_key": company_key,
        "dealer_name": clean_name,
        "sheet_type": sheet_type,
    }


def batch_update_sheet_types(updates: list[dict[str, str]]) -> dict[str, Any]:
    """
    Batch update sheet types for multiple dealers (used by drag-and-drop board).
    Each update item: {"company_key": "speedy", "dealer_name": "Mike", "sheet_type": "old"}
    """
    data = load_dealers_data()
    applied = 0
    errors = []

    for item in updates:
        ckey = str(item.get("company_key") or "").strip().lower()
        dname = str(item.get("dealer_name") or "").strip()
        st = str(item.get("sheet_type") or "").strip().lower()
        if st not in ("old", "new"):
            errors.append(f"Invalid sheet type '{st}' for dealer '{dname}'")
            continue
        if ckey in data and "dealers" in data[ckey] and dname in data[ckey]["dealers"]:
            data[ckey]["dealers"][dname]["sheet_type"] = st
            applied += 1
        else:
            errors.append(f"Dealer '{dname}' not found in '{ckey}'")

    if applied > 0:
        save_dealers_data(data)

    return {
        "success": True,
        "applied_count": applied,
        "errors": errors,
    }


def get_dealer_info(dealer_name: str, company_key: str | None = None) -> Optional[dict[str, Any]]:
    """
    Find dealer information across aliases and companies.
    Returns dealer dict with sheet_id, sheet_type, canonical dealer name, and company_key.
    """
    data = load_dealers_data()
    search_name = (dealer_name or "").strip().lower()
    if not search_name:
        return None

    companies = [company_key.lower()] if company_key and company_key.lower() in data else ["speedy", "inspira", "houston"]

    # 1. Exact match on canonical dealer name
    for ckey in companies:
        dealers = data.get(ckey, {}).get("dealers", {})
        for dname, dinfo in dealers.items():
            if dname.lower() == search_name:
                return {
                    "company_key": ckey,
                    "dealer_name": dname,
                    "sheet_id": dinfo.get("sheet_id", ""),
                    "sheet_type": dinfo.get("sheet_type", "new"),
                    "aliases": dinfo.get("aliases", []),
                }

    # 2. Match on aliases (exact)
    for ckey in companies:
        dealers = data.get(ckey, {}).get("dealers", {})
        for dname, dinfo in dealers.items():
            aliases = [a.lower() for a in dinfo.get("aliases", [])]
            if search_name in aliases:
                return {
                    "company_key": ckey,
                    "dealer_name": dname,
                    "sheet_id": dinfo.get("sheet_id", ""),
                    "sheet_type": dinfo.get("sheet_type", "new"),
                    "aliases": dinfo.get("aliases", []),
                }

    # 3. Smart Word & Token Match
    # Handles full names like "Jeffrin Francis" -> "Jeff", "Komalpreet Singh" -> "Komal", "Philemon Chacko" -> "Phil"
    tokens = [w for w in re.split(r"[\s\-_,]+", search_name) if len(w) >= 2]
    for ckey in companies:
        dealers = data.get(ckey, {}).get("dealers", {})
        for dname, dinfo in dealers.items():
            dname_lower = dname.lower()
            aliases = [a.lower() for a in dinfo.get("aliases", [])]

            # 3a. Exact token matches canonical name or alias
            for t in tokens:
                if t == dname_lower or t in aliases:
                    return {
                        "company_key": ckey,
                        "dealer_name": dname,
                        "sheet_id": dinfo.get("sheet_id", ""),
                        "sheet_type": dinfo.get("sheet_type", "new"),
                        "aliases": dinfo.get("aliases", []),
                    }

            # 3b. Token prefix / stem match (e.g. "komalpreet" starts with "komal")
            for t in tokens:
                if len(dname_lower) >= 4 and t.startswith(dname_lower):
                    return {
                        "company_key": ckey,
                        "dealer_name": dname,
                        "sheet_id": dinfo.get("sheet_id", ""),
                        "sheet_type": dinfo.get("sheet_type", "new"),
                        "aliases": dinfo.get("aliases", []),
                    }
                for a in aliases:
                    if len(a) >= 4 and t.startswith(a):
                        return {
                            "company_key": ckey,
                            "dealer_name": dname,
                            "sheet_id": dinfo.get("sheet_id", ""),
                            "sheet_type": dinfo.get("sheet_type", "new"),
                            "aliases": dinfo.get("aliases", []),
                        }

    # 4. Substring / Prefix match on full search_name
    for ckey in companies:
        dealers = data.get(ckey, {}).get("dealers", {})
        for dname, dinfo in dealers.items():
            dname_lower = dname.lower()
            if len(dname_lower) >= 4 and (search_name.startswith(dname_lower) or dname_lower in search_name):
                return {
                    "company_key": ckey,
                    "dealer_name": dname,
                    "sheet_id": dinfo.get("sheet_id", ""),
                    "sheet_type": dinfo.get("sheet_type", "new"),
                    "aliases": dinfo.get("aliases", []),
                }
            for a in dinfo.get("aliases", []):
                a_lower = a.lower()
                if len(a_lower) >= 4 and (search_name.startswith(a_lower) or a_lower in search_name):
                    return {
                        "company_key": ckey,
                        "dealer_name": dname,
                        "sheet_id": dinfo.get("sheet_id", ""),
                        "sheet_type": dinfo.get("sheet_type", "new"),
                        "aliases": dinfo.get("aliases", []),
                    }

    return None


