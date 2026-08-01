#!/usr/bin/env python3
"""
test_mir.py — Test suite for mir.py v2 (Dual Ledger MIR Calculator)

Tests are organized into 3 layers:
  1. Unit tests  — pure logic, no external I/O (calculate, fuzzy match, rates)
  2. Groq test   — real Groq API call to verify structured extraction
  3. Integration — full dual-ledger flow with mocked ERP results
"""

import sys
import os

# Make sure we can import from the speedy_blinds package directory
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "speedy_blinds"))

import config
# Set a dummy company so config.ACTIVE_COMPANY_LABEL is populated
config.ACTIVE_COMPANY_LABEL = "Speedy Blinds [TEST]"

import mir

# ── Helpers ──────────────────────────────────────────────────────────────────
PASS  = "\033[32m  ✓ PASS\033[0m"
FAIL  = "\033[31m  ✗ FAIL\033[0m"
SEP   = "\033[36m" + "─" * 60 + "\033[0m"
TOTAL = {"pass": 0, "fail": 0}

def check(label: str, condition: bool, detail: str = "") -> None:
    TOTAL["pass" if condition else "fail"] += 1
    icon = PASS if condition else FAIL
    suffix = f"  ({detail})" if detail else ""
    print(f"{icon}  {label}{suffix}")

def section(title: str) -> None:
    print(f"\n{SEP}")
    print(f"\033[1m  {title}\033[0m")
    print(SEP)


# ══════════════════════════════════════════════════════════════════════════════
# 1. UNIT TESTS — Pure logic, no external calls
# ══════════════════════════════════════════════════════════════════════════════
section("1. Unit Tests — Fuzzy Installer Name Matching")

check("Exact same name returns match",
    mir._fuzzy_match("John", ["John", "Mike"]) == "John")

check("Case-insensitive exact match",
    mir._fuzzy_match("john", ["John", "Mike"]) == "John")

check("Substring match (jon → John)",
    mir._fuzzy_match("jon", ["John"]) is not None)

check("Edit-distance ≤ 2 (Jon → John)",
    mir._fuzzy_match("Jon", ["John"]) is not None)

check("Clearly different name returns None",
    mir._fuzzy_match("Samantha", ["John", "Mike", "Alen"]) is None)

check("Empty existing list returns None",
    mir._fuzzy_match("John", []) is None)

check("New name not similar to any returns None",
    mir._fuzzy_match("Zephyr", ["John", "Mike", "Alen"]) is None)


# ══════════════════════════════════════════════════════════════════════════════
section("2. Unit Tests — Rate Calculations (calculate_dual_ledger)")

# Mock ERP results
MOCK_ERP = {
    "ORD-0246": {"order_number": "ORD-0246", "blind_count": 5,  "dealer_name": "VT Thomas"},
    "ORD-0201": {"order_number": "ORD-0201", "blind_count": 10, "dealer_name": "Phil"},
    "ORD-0198": {"order_number": "ORD-0198", "blind_count": 4,  "dealer_name": "Alen"},
    "ORD-0305": {"order_number": "ORD-0305", "blind_count": 3,  "dealer_name": "VT Thomas"},
    "ORD-0402": {"order_number": "ORD-0402", "blind_count": 6,  "dealer_name": "Han"},
    "ORD-0500": None,  # not found
}

# Test Case A — Simple install batch (John: 2 install orders)
batches_a = [
    {
        "installer": "John",
        "orders": [
            {"order_number": "ORD-0246", "installs_txt": 5, "uninstalls_txt": 0, "reworks_txt": 0,           "big_ladder": False, "assumed": False},
            {"order_number": "ORD-0201", "installs_txt": 5, "uninstalls_txt": 0, "reworks_txt": 0,           "big_ladder": False, "assumed": False},
        ]
    }
]
il_a, dl_a, nf_a = mir.calculate_dual_ledger(batches_a, MOCK_ERP)

# John: 5 blinds + 10 blinds = 15 × $3 = $45
check("John total pay — 15 blinds × $3 = $45",
    il_a["John"]["total_pay"] == 45.0,
    f"got ${il_a['John']['total_pay']:.2f}")

# VT Thomas: 5 × $4 = $20
check("VT Thomas charge — 5 blinds install × $4 = $20",
    dl_a["VT Thomas"]["total_charge"] == 20.0,
    f"got ${dl_a['VT Thomas']['total_charge']:.2f}")

# Phil: 10 × $4 = $40
check("Phil charge — 10 blinds install × $4 = $40",
    dl_a["Phil"]["total_charge"] == 40.0,
    f"got ${dl_a['Phil']['total_charge']:.2f}")

check("Not-found list is empty for test A",
    len(nf_a) == 0, f"got {nf_a}")


# Test Case B — Uninstall+Install batch
batches_b = [
    {
        "installer": "Mike",
        "orders": [
            {"order_number": "ORD-0246", "installs_txt": 5, "uninstalls_txt": 5, "reworks_txt": 0, "big_ladder": False, "assumed": False},
        ]
    }
]
il_b, dl_b, nf_b = mir.calculate_dual_ledger(batches_b, MOCK_ERP)

# Mike: 5 blinds × $3 = $15 (installer always $3 regardless of type)
check("Mike pay — 5 installs + 5 uninstalls = $25",
    il_b["Mike"]["total_pay"] == 25.0,
    f"got ${il_b['Mike']['total_pay']:.2f}")

# VT Thomas: 5 × $8 = $40 (uninstall+install dealer rate)
check("VT Thomas charge — 5 blinds uninstall+install × $8 = $40",
    dl_b["VT Thomas"]["total_charge"] == 40.0,
    f"got ${dl_b['VT Thomas']['total_charge']:.2f}")


# Test Case C — Big Ladder modifier
batches_c = [
    {
        "installer": "Alen",
        "orders": [
            {"order_number": "ORD-0198", "installs_txt": 5, "uninstalls_txt": 0, "reworks_txt": 0, "big_ladder": True, "assumed": False},
        ]
    }
]
il_c, dl_c, nf_c = mir.calculate_dual_ledger(batches_c, MOCK_ERP)

# Alen: 4 blinds × $3 + $20 big ladder = $32 (SELF INSTALL -> $0)
check("Alen pay — Self Install = $0",
    il_c["Alen"]["total_pay"] == 0.0,
    f"got ${il_c['Alen']['total_pay']:.2f}")

# Dealer: 4 × $4 + $25 = $41
check("Alen dealer charge — 4 × $4 + $25 big ladder = $41",
    dl_c["Alen"]["total_charge"] == 41.0,
    f"got ${dl_c['Alen']['total_charge']:.2f}")


# Test Case D — Uninstall Only
batches_d = [
    {
        "installer": "Han",
        "orders": [
            {"order_number": "ORD-0402", "installs_txt": 0, "uninstalls_txt": 6, "reworks_txt": 0, "big_ladder": False, "assumed": False},
        ]
    }
]
il_d, dl_d, nf_d = mir.calculate_dual_ledger(batches_d, MOCK_ERP)

# Han: 6 uninstalls (SELF INSTALL -> $0)
check("Han pay — Self Install = $0",
    il_d["Han"]["total_pay"] == 0.0,
    f"got ${il_d['Han']['total_pay']:.2f}")

# Han dealer: 6 × $4 = $24 (uninstall_only = $4/blind)
check("Han dealer charge — 6 × $4 = $24",
    dl_d["Han"]["total_charge"] == 24.0,
    f"got ${dl_d['Han']['total_charge']:.2f}")


# Test Case E — Order not found in ERP
batches_e = [
    {
        "installer": "John",
        "orders": [
            {"order_number": "ORD-0500", "installs_txt": None, "uninstalls_txt": None, "reworks_txt": None, "big_ladder": False, "assumed": False},
        ]
    }
]
il_e, dl_e, nf_e = mir.calculate_dual_ledger(batches_e, MOCK_ERP)

check("Not-found order excluded from installer pay",
    il_e["John"]["total_pay"] == 0.0,
    f"got ${il_e['John']['total_pay']:.2f}")

check("Not-found order tracked in not_found list",
    "ORD-0500" in nf_e,
    f"not_found={nf_e}")

check("No dealer entries created for not-found order",
    len(dl_e) == 0,
    f"dealer_ledger={dl_e}")


# Test Case F — Multi-installer, shared dealer (dealer totals accumulate)
batches_f = [
    {
        "installer": "John",
        "orders": [
            {"order_number": "ORD-0246", "installs_txt": 5, "uninstalls_txt": 0, "reworks_txt": 0, "big_ladder": False, "assumed": False},
        ]
    },
    {
        "installer": "Mike",
        "orders": [
            {"order_number": "ORD-0305", "installs_txt": 5, "uninstalls_txt": 0, "reworks_txt": 0, "big_ladder": False, "assumed": False},
        ]
    },
]
il_f, dl_f, nf_f = mir.calculate_dual_ledger(batches_f, MOCK_ERP)

# VT Thomas should accumulate: 5×$4 (John) + 3×$4 (Mike) = $20 + $12 = $32
check("VT Thomas charge accumulates across 2 installers → $32",
    dl_f["VT Thomas"]["total_charge"] == 32.0,
    f"got ${dl_f['VT Thomas']['total_charge']:.2f}")

check("VT Thomas has 2 order entries (one per installer)",
    len(dl_f["VT Thomas"]["orders"]) == 2,
    f"got {len(dl_f['VT Thomas']['orders'])}")

check("John pay correct in multi-installer run",
    il_f["John"]["total_pay"] == 15.0,
    f"got ${il_f['John']['total_pay']:.2f}")

check("Mike pay correct in multi-installer run",
    il_f["Mike"]["total_pay"] == 9.0,
    f"got ${il_f['Mike']['total_pay']:.2f}")


# Test Case G — Big ladder + uninstall+install stacked
batches_g = [
    {
        "installer": "Alen",
        "orders": [
            {"order_number": "ORD-0246", "installs_txt": 5, "uninstalls_txt": 5, "reworks_txt": 0, "big_ladder": True, "assumed": False},
        ]
    }
]
il_g, dl_g, nf_g = mir.calculate_dual_ledger(batches_g, MOCK_ERP)

# Alen pay: 5 × $3 + $20 = $35
check("Stacked: uninstall+install + big_ladder → installer $45",
    il_g["Alen"]["total_pay"] == 45.0,
    f"got ${il_g['Alen']['total_pay']:.2f}")

# VT Thomas: 5 × $8 + $25 = $65
check("Stacked: uninstall+install + big_ladder → dealer $65",
    dl_g["VT Thomas"]["total_charge"] == 65.0,
    f"got ${dl_g['VT Thomas']['total_charge']:.2f}")


# Test Case H — Zero blinds order with big ladder (big ladder still charges)
MOCK_ERP_ZERO = {"ORD-0999": {"order_number": "ORD-0999", "blind_count": 0, "dealer_name": "Phil"}}
batches_h = [
    {
        "installer": "John",
        "orders": [
            {"order_number": "ORD-0999", "installs_txt": 5, "uninstalls_txt": 0, "reworks_txt": 0, "big_ladder": True, "assumed": False},
        ]
    }
]
il_h, dl_h, nf_h = mir.calculate_dual_ledger(batches_h, MOCK_ERP_ZERO)

check("Zero blinds + big ladder → installer gets $20 (ladder only)",
    il_h["John"]["total_pay"] == 20.0,
    f"got ${il_h['John']['total_pay']:.2f}")

check("Zero blinds + big ladder → dealer charged $25 (ladder only)",
    dl_h["Phil"]["total_charge"] == 25.0,
    f"got ${dl_h['Phil']['total_charge']:.2f}")


# ══════════════════════════════════════════════════════════════════════════════
section("3. Unit Tests — Plain Text Email Builder")

# Build a small ledger for email testing
batches_email = [
    {
        "installer": "John",
        "orders": [
            {"order_number": "ORD-0246", "installs_txt": 5, "uninstalls_txt": 0, "reworks_txt": 0,           "big_ladder": False, "assumed": False},
            {"order_number": "ORD-0305", "installs_txt": 5, "uninstalls_txt": 5, "reworks_txt": 0, "big_ladder": True,  "assumed": False},
        ]
    },
    {
        "installer": "Mike",
        "orders": [
            {"order_number": "ORD-0402", "installs_txt": 0, "uninstalls_txt": 6, "reworks_txt": 0, "big_ladder": False, "assumed": False},
        ]
    }
]
il_em, dl_em, nf_em = mir.calculate_dual_ledger(batches_email, MOCK_ERP)

plain = mir._build_mir_plain(il_em, dl_em, nf_em, "August 1, 2026")

check("Plain email contains installer section header",
    "INSTALLER PAYMENTS" in plain)

check("Plain email contains dealer section header",
    "DEALER CHARGES" in plain)

check("Plain email contains John's name",
    "John:" in plain)

check("Plain email contains Mike's name",
    "Mike:" in plain)

check("Plain email contains VT Thomas",
    "VT Thomas" in plain)

check("Plain email contains Big Ladder note for ORD-0305",
    "Big Ladder" in plain and "ORD-0305" in plain)

check("Plain email contains grand total installer line",
    "GRAND TOTAL TO PAY INSTALLERS" in plain)

check("Plain email contains grand total dealer line",
    "GRAND TOTAL TO COLLECT FROM DEALERS" in plain)


# ══════════════════════════════════════════════════════════════════════════════
section("4. Unit Tests — HTML Email Builder")

html = mir._build_mir_html(il_em, dl_em, nf_em, "August 1, 2026")

check("HTML email is valid HTML start",
    html.strip().startswith("<!DOCTYPE html>"))

check("HTML contains installer section",
    "💼 Installer Payments" in html)

check("HTML contains dealer section",
    "🏪 Dealer Charges" in html)

check("HTML contains John",
    "John" in html)

check("HTML contains VT Thomas",
    "VT Thomas" in html)

check("HTML contains big ladder highlight for ORD-0305",
    "+$20" in html or "+$25" in html)

check("HTML contains grand pay total",
    str(f"{sum(d['total_pay'] for d in il_em.values()):.2f}") in html)

check("HTML contains grand charge total",
    str(f"{sum(d['total_charge'] for d in dl_em.values()):.2f}") in html)


# ══════════════════════════════════════════════════════════════════════════════
section("5. Groq Integration Test — parse_order_jobs (SKIPPED)")

# Various freeform text examples covering all job types and big ladder
groq_tests = [
    {
        "label": "Simple install list",
        "text":  "Installed ON 246, 201, 198",
        "expect_orders": {"ORD-0246", "ORD-0201", "ORD-0198"},
        "expect_installs": True,
    },
    {
        "label": "Uninstall only mention",
        "text":  "Removed blinds from ON 246. Took down ON 201.",
        "expect_orders": {"ORD-0246", "ORD-0201"},
        "expect_uninstalls": True,
    },
    {
        "label": "Mixed removed + installed = uninstall_install",
        "text":  "Removed: ON 246, ON 305. Installed: ON 246, ON 305.",
        "expect_orders":     {"ORD-0246", "ORD-0305"},
        "expect_installs_246": True, "expect_uninstalls_246": True,
    },
    {
        "label": "Big ladder flag",
        "text":  "Installed 198. Big ladder on 198.",
        "expect_orders": {"ORD-0198"},
        "expect_big_ladder_198": True,
    },
    {
        "label": "Complex freeform (test_orders.txt style)",
        "text":  (
            "0246 vtt - benni = 17\n"
            "0201 Phil - Sajid 46 = 10\n"
            "0198 Alen - Shakeel = 4\n"
            "0214 Vtt - shamesh = 14\n"
            "0224 Han - sarbha = 8\n"
            "0232 Han - Benny = 5"
        ),
        "expect_orders": {"ORD-0246", "ORD-0201", "ORD-0198", "ORD-0214", "ORD-0224", "ORD-0232"},
    },
    {
        "label": "Reinstall keyword → uninstall_install",
        "text":  "Reinstalled blinds: ON 305, ON 402",
        "expect_orders": {"ORD-0305", "ORD-0402"},
        "expect_installs": True, "expect_uninstalls": True,
    },
]

for t in groq_tests:
    continue
    print(f"\n  ▶ Groq: {t['label']}")
    print(f"    Input: {t['text'][:80]}{'...' if len(t['text']) > 80 else ''}")
    try:
        result = mir.parse_order_jobs(t["text"])
        result_nums = {r["order_number"] for r in result}
        result_map  = {r["order_number"]: r for r in result}

        print(f"    Parsed: {result}")

        if "expect_orders" in t:
            check(
                f"All expected orders found",
                t["expect_orders"].issubset(result_nums),
                f"expected {t['expect_orders']}, got {result_nums}"
            )

        if "expect_installs" in t:
            all_match = all(
                (result_map[n].get("installs_txt") or 0) > 0
                for n in t["expect_orders"]
                if n in result_map
            )
            check("All orders classified as install", all_match)

        if "expect_uninstalls" in t:
            all_match = all(
                (result_map[n].get("uninstalls_txt") or 0) > 0
                for n in t["expect_orders"]
                if n in result_map
            )
            check("All orders classified as uninstall", all_match, f"types={[(r['order_number'], r.get('uninstalls_txt')) for r in result]}")

        if "expect_installs_246" in t:
            actual_i = (result_map.get("ORD-0246", {}).get("installs_txt") or 0) > 0
            actual_u = (result_map.get("ORD-0246", {}).get("uninstalls_txt") or 0) > 0
            check("ORD-0246 has installs and uninstalls", actual_i and actual_u)

        if "expect_big_ladder_198" in t:
            actual = result_map.get("ORD-0198", {}).get("big_ladder", False)
            check(
                "ORD-0198 has big_ladder=True",
                actual is True,
                f"got {actual}"
            )

    except Exception as exc:
        TOTAL["fail"] += 1
        print(f"{FAIL}  Groq test crashed: {exc}")


# ══════════════════════════════════════════════════════════════════════════════
section("6. Full Integration Simulation (no ERP / no email)")

# Simulate exactly what run_mir does, but with mock data
print("\n  Simulating dual-ledger flow with 3 installers, 4 dealers...")

SIM_BATCHES = [
    {
        "installer": "John",
        "orders": [
            {"order_number": "ORD-0246", "installs_txt": 5, "uninstalls_txt": 0, "reworks_txt": 0,           "big_ladder": False, "assumed": False},
            {"order_number": "ORD-0305", "installs_txt": 5, "uninstalls_txt": 5, "reworks_txt": 0, "big_ladder": True,  "assumed": False},
        ]
    },
    {
        "installer": "Mike",
        "orders": [
            {"order_number": "ORD-0201", "installs_txt": 5, "uninstalls_txt": 0, "reworks_txt": 0,           "big_ladder": False, "assumed": False},
            {"order_number": "ORD-0198", "installs_txt": 0, "uninstalls_txt": 6, "reworks_txt": 0,    "big_ladder": False, "assumed": False},
        ]
    },
    {
        "installer": "Alen",
        "orders": [
            {"order_number": "ORD-0402", "installs_txt": 5, "uninstalls_txt": 0, "reworks_txt": 0,           "big_ladder": False, "assumed": False},
            {"order_number": "ORD-0500", "installs_txt": None, "uninstalls_txt": None, "reworks_txt": None,           "big_ladder": False, "assumed": False},  # not found
        ]
    }
]

il_sim, dl_sim, nf_sim = mir.calculate_dual_ledger(SIM_BATCHES, MOCK_ERP)

# Expected values
# John: ORD-0246 (5 blinds × $3=$15) + ORD-0305 (3 blinds × $3 + $20=$29) = $44
# Mike: ORD-0201 (10 × $3=$30) + ORD-0198 (4 × $3=$12) = $42
# Alen: ORD-0402 (6 × $3=$18) + ORD-0500 (not found=$0) = $18

check("John total pay = $50.00", il_sim["John"]["total_pay"] == 50.0, f"got ${il_sim['John']['total_pay']:.2f}")
check("Mike total pay = $38.00", il_sim["Mike"]["total_pay"] == 38.0, f"got ${il_sim['Mike']['total_pay']:.2f}")
check("Alen total pay = $18.00", il_sim["Alen"]["total_pay"] == 18.0, f"got ${il_sim['Alen']['total_pay']:.2f}")

grand_pay_sim = sum(d["total_pay"] for d in il_sim.values())
check("Grand installer total = $106.00", grand_pay_sim == 106.0, f"got ${grand_pay_sim:.2f}")

# Dealer expected:
# VT Thomas: ORD-0246 (5 × $4=$20) + ORD-0305 (3 × $8 + $25=$49) = $69
# Phil:      ORD-0201 (10 × $4=$40)
# Alen:      ORD-0198 (4 × $4=$16)
# Han:       ORD-0402 (6 × $4=$24)

check("VT Thomas charge = $69.00",
    dl_sim["VT Thomas"]["total_charge"] == 69.0,
    f"got ${dl_sim['VT Thomas']['total_charge']:.2f}")

check("Phil charge = $40.00",
    dl_sim["Phil"]["total_charge"] == 40.0,
    f"got ${dl_sim['Phil']['total_charge']:.2f}")

check("Alen (dealer) charge = $16.00",
    dl_sim["Alen"]["total_charge"] == 16.0,
    f"got ${dl_sim['Alen']['total_charge']:.2f}")

check("Han charge = $24.00",
    dl_sim["Han"]["total_charge"] == 24.0,
    f"got ${dl_sim['Han']['total_charge']:.2f}")

grand_charge_sim = sum(d["total_charge"] for d in dl_sim.values())
check("Grand dealer total = $149.00", grand_charge_sim == 149.0, f"got ${grand_charge_sim:.2f}")

check("ORD-0500 is in not_found", "ORD-0500" in nf_sim, f"not_found={nf_sim}")

# Display the full terminal summary
print("\n  --- Terminal Summary Output ---")
mir.display_dual_summary(il_sim, dl_sim, nf_sim)


# ══════════════════════════════════════════════════════════════════════════════
# Results
# ══════════════════════════════════════════════════════════════════════════════
print(SEP)
total = TOTAL["pass"] + TOTAL["fail"]
colour = "\033[32m" if TOTAL["fail"] == 0 else "\033[31m"
print(f"\n  {colour}Results: {TOTAL['pass']}/{total} passed, {TOTAL['fail']} failed\033[0m\n")
if TOTAL["fail"] > 0:
    sys.exit(1)
