"""
config.py — Speedy Blinds / Inspira Blinds Order Automation
============================================================
Fill in your values here before running main.py.

The COMPANY dict maps a short name → its ERP config.
The active company is chosen at runtime by the user prompt in main.py.
"""

import os
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Groq API (free tier, no billing required)
# ---------------------------------------------------------------------------
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL   = "llama-3.3-70b-versatile"   # best free model for structured extraction

# ---------------------------------------------------------------------------
# Google Sheets
# ---------------------------------------------------------------------------
CREDENTIALS_FILE = "credentials.json"   # path relative to project root
SHEET_TAB        = "2026"               # worksheet tab name in every dealer file

# Columns B–G receive: Mth | Date | Order# | Customer | Qty | Total Cost
# We write all 6 fields. G (Total Cost) receives the ERP price.
FIRST_COLUMN = "B"   # start column letter
WRITE_COLUMNS = 6    # B, C, D, E, F, G

# ---------------------------------------------------------------------------
# ERP API — per-company configuration
# ---------------------------------------------------------------------------
# Credentials
SPEEDY_ERP_EMAIL    = os.getenv("ERP_EMAIL", "")
SPEEDY_ERP_PASSWORD = os.getenv("ERP_PASSWORD", "")

INSPIRA_ERP_EMAIL    = os.getenv("INSPIRA_ERP_EMAIL", "bot_admin@inspirablinds.com")
INSPIRA_ERP_PASSWORD = os.getenv("INSPIRA_ERP_PASSWORD", "Admin@123")

ERP_EMAIL    = SPEEDY_ERP_EMAIL
ERP_PASSWORD = SPEEDY_ERP_PASSWORD

# Company definitions.
# token_key / token_expires_key → the .env variable names used to cache
# each company's Bearer token separately so they don't overwrite each other.
COMPANY: dict[str, dict] = {
    "speedy": {
        "label":             "Speedy Blinds",
        "base_url":          "https://api.speedyblinds.cloud/api/v1",
        "tenant_ids":        [1],   # Speedy Blinds — single tenant
        "token_key":         "ERP_TOKEN",
        "token_expires_key": "ERP_TOKEN_EXPIRES_AT",
    },
    "inspira": {
        "label":             "Inspira Blinds",
        "base_url":          "https://api.inspirablinds.cloud/api/v1",
        "tenant_ids":        [1],
        "token_key":         "INSPIRA_ERP_TOKEN",
        "token_expires_key": "INSPIRA_ERP_TOKEN_EXPIRES_AT",
    },
}

# ---------------------------------------------------------------------------
# Defaults — these are overridden at runtime by the company the user picks.
# erp.py reads these after main.py calls config.set_company().
# ---------------------------------------------------------------------------
ERP_BASE_URL         = COMPANY["speedy"]["base_url"]
ERP_TENANT_IDS: list[int] = COMPANY["speedy"]["tenant_ids"]
ERP_TOKEN_KEY        = COMPANY["speedy"]["token_key"]
ERP_TOKEN_EXPIRES_KEY = COMPANY["speedy"]["token_expires_key"]
ACTIVE_COMPANY_LABEL = COMPANY["speedy"]["label"]


def set_company(key: str) -> None:
    """
    Called by main.py once the user has chosen a company.
    Mutates the module-level ERP_* variables so erp.py picks them up.
    """
    global ERP_BASE_URL, ERP_TENANT_IDS, ERP_TOKEN_KEY, ERP_TOKEN_EXPIRES_KEY, ACTIVE_COMPANY_LABEL
    global DEALER_SHEETS, DEALER_ALIASES, ERP_EMAIL, ERP_PASSWORD
    cfg = COMPANY[key]
    ERP_BASE_URL          = cfg["base_url"]
    ERP_TENANT_IDS        = cfg["tenant_ids"]
    ERP_TOKEN_KEY         = cfg["token_key"]
    ERP_TOKEN_EXPIRES_KEY = cfg["token_expires_key"]
    ACTIVE_COMPANY_LABEL  = cfg["label"]
    if key == "inspira":
        DEALER_SHEETS  = INSPIRA_DEALER_SHEETS
        DEALER_ALIASES = INSPIRA_DEALER_ALIASES
        ERP_EMAIL      = INSPIRA_ERP_EMAIL
        ERP_PASSWORD   = INSPIRA_ERP_PASSWORD
    else:
        DEALER_SHEETS  = SPEEDY_DEALER_SHEETS
        DEALER_ALIASES = SPEEDY_DEALER_ALIASES
        ERP_EMAIL      = SPEEDY_ERP_EMAIL
        ERP_PASSWORD   = SPEEDY_ERP_PASSWORD

# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------
# macOS notifications are automatic (no config needed).
#
# For Gmail, generate an App Password at:
#   https://myaccount.google.com/apppasswords
# Then add these to your .env file:
#   NOTIFY_EMAIL_FROM=you@gmail.com
#   NOTIFY_EMAIL_TO=you@gmail.com          (can be same address)
#   NOTIFY_EMAIL_APP_PASSWORD=xxxx xxxx xxxx xxxx
NOTIFY_EMAIL_FROM         = os.getenv("NOTIFY_EMAIL_FROM", "")
NOTIFY_EMAIL_TO           = os.getenv("NOTIFY_EMAIL_TO", "")
NOTIFY_EMAIL_APP_PASSWORD = os.getenv("NOTIFY_EMAIL_APP_PASSWORD", "")

# ---------------------------------------------------------------------------
# Dealer → Spreadsheet ID (Speedy Blinds)
# ---------------------------------------------------------------------------
SPEEDY_DEALER_SHEETS: dict[str, str] = {
    "VT Thomas": "1KaauO3BEkZF3A_hW_3zgH3VE6jBME21UY0fF5GIoiN4",
    "Alen":      "1pkyF_19FWWSbjxvu8Ybht1u9kKu7Eyec5VhqDdcdF1A",
    "Mike":      "1zT3LGa38Khg3CPH6BMk3N7THlshms8w5GpGGULxqTGU",
    "Ayush":     "1PJ3Um_RUf95dHw_imbDS4IKR6HLN9AgdOolE5TKmXHA",
    "Phil":      "1hNaD9gixkWfQdrQM4Lnc-YtjAfT6GiJBhNUnRw1klWY",
    "Han":       "1pr7_aXotzMTPxQYXcCGwyc2dhODZpcxOLa7ez2Bn5Hc",
    "Jubin":     "1ZsTPaFqyDtMITPlwICmIrKolRv_F6ALvimZsOGjydYE",
    "Komal":     "1T3WNko2mYnG6uUBUtHkKlu1lAglnHU6wiI8Xg7DQfxM",
    "Aman":      "1xVXiuPsymDtC_Mtm8KikX8r4aigFxdkJVApYJeCzDVc",
    "Tom":       "10M5u4CKllu69fVCXH9V2iZ2RFUZuc1X8CCGf1ueIFeo",
    "Shawn":     "1GFouQwTS5MR6z5beZxEeGUz-I_0icLPX9KVw0YgKgrI",
    "Mathew":    "1oBayeMSr6VcroVqKSBLCe9bUqwoR1Peem9JEbpAeYeo",
    "Akash":     "1fZh-58DzPYMiGvXdPfw2TYPoWPPrsJhJZlqnHH8f3D4",
    "Joseph":    "12f1_iP6O_Rnp9Ze4bKbjF1zBMYeWxJFutvNc1OzZXb0",
    "Nithin":    "1kstbKMPhgnf3vPuZ7EEpL8cJrgaLQdChEu5eJ5AGGNA",
}

# Fuzzy-match aliases (lowercase → canonical dealer name)
SPEEDY_DEALER_ALIASES: dict[str, str] = {
    "vt thomas":  "VT Thomas",
    "thomas":     "VT Thomas",
    "vtt":        "VT Thomas",
    "alen":       "Alen",
    "allen":      "Alen",
    "mike":       "Mike",
    "michael":    "Mike",
    "ayush":      "Ayush",
    "phil":       "Phil",
    "philip":     "Phil",
    "han":        "Han",
    "jubin":      "Jubin",
    "komal":      "Komal",
    "kamal":      "Komal",
    "aman":       "Aman",
    "tom":        "Tom",
    "shawn":      "Shawn",
    "mathew":     "Mathew",
    "matthew":    "Mathew",
    "sneha":      "Mathew",   # Sneha = Mathew (same dealer)
    "akash":      "Akash",
    "joseph":     "Joseph",
    "nithin":     "Nithin",
}

# ---------------------------------------------------------------------------
# Dealer → Spreadsheet ID (Inspira Blinds)
# ---------------------------------------------------------------------------
INSPIRA_DEALER_SHEETS: dict[str, str] = {
    "Aman":      "1IuOEqWFBS5McgDtgfu1DHsh_xaS9Q0zD77BLNkowa10",
    "Harvinder": "1WGxd4xrNOR4JJQG2KJRHZ60xWVM6VaGl06id1sO9e5k",
    "Heman":     "1hVqK0SlfXZKhrDiSLOwaw3eAZ9GEqegBiAhYSwwCpIk",
    "Jerin":     "1UY4Nv9IXUYGfea4873dyHpfz6GKLLDnaWNgvC8p_iQ4",
    "Rahul":     "1UCCKwchD1GTwh-2B3frixbMqz7kTGwnxhaZnLzN3BAE",
    "Surinder":  "1fTMRPODsdmyXPbjgRRTr_2oOOtv_WWlS-N9fwisvv8Y",
    "AJ":        "1pM0QWHUHF2DQ0STfjUflOaEz_-pxmA-RRS_kJzUjVlA",
    "Sidhu":     "1qb1y18Z8H3zYLkDCeJ161mxr0EG9Vxhn5MT94jJVOEQ",
}

INSPIRA_DEALER_ALIASES: dict[str, str] = {
    "haraman":      "Aman",
    "aman":         "Aman",
    "amandeep":     "Aman",
    "harvinder":    "Harvinder",
    "heman":        "Heman",
    "heman walia":  "Heman",
    "walia":        "Heman",
    "jerin":        "Jerin",
    "rahul":        "Rahul",
    "rahul bansal": "Rahul",
    "bansal":       "Rahul",
    "surinder":     "Surinder",
    "aj":           "AJ",
    "sidhu":        "Sidhu",
}

# Active defaults (overridden by set_company() at runtime)
DEALER_SHEETS  = SPEEDY_DEALER_SHEETS
DEALER_ALIASES = SPEEDY_DEALER_ALIASES
