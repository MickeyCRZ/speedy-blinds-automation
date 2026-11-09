"""
test_payments.py — Unit & Integration tests for EFT & Cash Payments and Sheet Layouts
"""

import json
from unittest.mock import MagicMock, patch
import pytest

from speedy_blinds import dealers_manager, payments_service
from app import app


@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


@pytest.fixture(autouse=True)
def prevent_log_writes(monkeypatch):
    """Never write test runs to erp_updates.log."""
    monkeypatch.setattr(payments_service, "_log_payment_action", lambda *args, **kwargs: None)


@pytest.fixture(autouse=True)
def isolate_dealers_json(tmp_path, monkeypatch):
    """
    Isolate dealers.json for tests so live settings are not overwritten
    and tests run with consistent defaults.
    """
    temp_json = tmp_path / "dealers.json"
    with open(temp_json, "w", encoding="utf-8") as f:
        json.dump(dealers_manager._DEFAULT_DEALERS, f, indent=2)
    monkeypatch.setattr(dealers_manager, "_DEALERS_JSON_PATH", temp_json)



# ---------------------------------------------------------------------------
# 1. Sheet Configs & Drag-and-Drop Tests
# ---------------------------------------------------------------------------

def test_sheet_configs_get_and_save():
    configs = dealers_manager.get_sheet_configs()
    assert "old" in configs
    assert "new" in configs
    assert configs["new"]["order_col"] == "D"
    assert configs["new"]["eft_col"] == "H"
    assert configs["new"]["cash_col"] == "I"
    assert configs["new"]["eft_alt_col"] == "J"
    assert configs["new"]["cash_alt_col"] == "L"
    assert configs["new"]["comments_col"] == "K"

    # Test saving an update
    updated = dealers_manager.save_sheet_configs({
        "new": {"eft_col": "h", "cash_col": "i", "eft_alt_col": "j", "cash_alt_col": "l"}
    })
    assert updated["new"]["eft_col"] == "H"
    assert updated["new"]["cash_col"] == "I"
    assert updated["new"]["eft_alt_col"] == "J"
    assert updated["new"]["cash_alt_col"] == "L"


def test_update_dealer_sheet_type_and_batch():
    # Update single dealer
    res = dealers_manager.update_dealer_sheet_type("speedy", "Phil", "old")
    assert res["success"] is True
    assert res["sheet_type"] == "old"

    info = dealers_manager.get_dealer_info("Phil", "speedy")
    assert info is not None
    assert info["sheet_type"] == "old"

    # Batch update (drag-and-drop)
    batch_res = dealers_manager.batch_update_sheet_types([
        {"company_key": "speedy", "dealer_name": "Mike", "sheet_type": "old"},
        {"company_key": "speedy", "dealer_name": "Mike", "sheet_type": "new"},
    ])
    assert batch_res["success"] is True
    assert batch_res["applied_count"] == 2


# ---------------------------------------------------------------------------
# 2. Payment Parsing Tests
# ---------------------------------------------------------------------------

def test_parse_bulk_payments_regex_fallback():
    raw_text = """
    ORD-0246 : 500 : John Doe : EFT : Mike
    0248 : -150.50 : Wire Return : EFT : Phil
    ORD-0250 : 250 : Cash handoff : Cash : Jubin
    """
    items = payments_service.parse_bulk_payments_with_groq(raw_text, default_date="2026-03-24")
    assert len(items) == 3

    # Check item 1
    assert items[0]["order_number"] == "ORD-0246"
    assert items[0]["amount"] == 500.0
    assert items[0]["type"] == "EFT"
    assert items[0]["sender"] == "John Doe"
    assert items[0]["is_negative"] is False

    # Check item 2 (Negative amount deduction)
    assert items[1]["order_number"] == "ORD-0248"
    assert items[1]["amount"] == -150.50
    assert items[1]["is_negative"] is True
    assert items[1]["sender"] == "Wire Return"

    # Check item 3 (Cash)
    assert items[2]["order_number"] == "ORD-0250"
    assert items[2]["amount"] == 250.0
    assert items[2]["type"] == "Cash"


# ---------------------------------------------------------------------------
# 3. Sheet Pre-check & Push Mocked Tests
# ---------------------------------------------------------------------------

def test_check_payment_in_sheet_matched(monkeypatch):
    mock_service = MagicMock()
    # Mock order search returning row 4 for order ORD-0246
    mock_orders = {"values": [["ORD-0100"], ["ORD-0200"], ["ORD-0246"]]}
    # Mock row read
    mock_row = {"values": [["", "", ""]]} # primary, alt, comments are empty

    mock_service.spreadsheets().values().get().execute.side_effect = [mock_orders, mock_row]
    monkeypatch.setattr(payments_service.sheets, "_get_service", lambda: mock_service)

    res = payments_service.check_payment_in_sheet("VT Thomas", "ORD-0246", payment_type="EFT", company_key="speedy")
    assert res["found"] is True
    assert res["row"] == 3
    assert res["is_primary_occupied"] is False


def test_push_payment_to_sheet_matched_and_appends_comment(monkeypatch):
    mock_service = MagicMock()
    # 1. Order lookup returns row 3
    mock_orders = {"values": [["ORD-0100"], ["ORD-0200"], ["ORD-0246"]]}
    # 2. Existing primary cell check
    mock_cell = {"values": [[""]]}
    # 3. Existing comments check
    mock_comm = {"values": [["Previous Note"]]}

    mock_service.spreadsheets().values().get().execute.side_effect = [mock_orders, mock_cell, mock_comm]
    mock_batch = MagicMock()
    mock_batch.execute.return_value = {"totalUpdatedCells": 2}
    mock_service.spreadsheets().values().batchUpdate.return_value = mock_batch
    monkeypatch.setattr(payments_service.sheets, "_get_service", lambda: mock_service)

    payment = {
        "order_number": "ORD-0246",
        "amount": 500.0,
        "type": "EFT",
        "sender": "John Doe",
        "payment_date": "2026-03-24",
        "dealer": "VT Thomas",
        "company_key": "speedy",
        "is_negative": False,
        "target_column_mode": "primary",
    }
    res = payments_service.push_payment_to_sheet(payment)
    assert res["status"] == "success"
    assert res["row"] == 3
    assert res["payment_cell"] == "H3"
    assert res["comments_cell"] == "K3"
    assert "Previous Note; 2026-03-24-John Doe" in res["comments"]


def test_push_payment_to_sheet_not_found_appends_red(monkeypatch):
    mock_service = MagicMock()
    # 1. Order lookup returns list without the order
    mock_orders = {"values": [["ORD-0100"], ["ORD-0200"]]}
    # 2. Last row search (A:G) returns 5 rows
    mock_all = {"values": [["Jan", "1-Jan-2026", "ORD-0100"], ["Jan", "2-Jan-2026", "ORD-0200"]]}

    mock_service.spreadsheets().values().get().execute.side_effect = [mock_orders, mock_all]
    mock_batch = MagicMock()
    mock_batch.execute.return_value = {"totalUpdatedCells": 3}
    mock_service.spreadsheets().values().batchUpdate.return_value = mock_batch
    monkeypatch.setattr(payments_service.sheets, "_get_service", lambda: mock_service)

    # Mock highlight
    highlight_called = []
    def mock_hl(srv, sid, tab, row, max_cols=20, color="red"):
        highlight_called.append((sid, tab, row, color))
    monkeypatch.setattr(payments_service, "_highlight_full_row", mock_hl)

    payment = {
        "order_number": "ORD-9999",
        "amount": 350.0,
        "type": "Cash",
        "sender": "Sunny",
        "payment_date": "2026-03-24",
        "dealer": "VT Thomas",
        "company_key": "speedy",
        "is_negative": False,
    }
    res = payments_service.push_payment_to_sheet(payment)
    assert res["status"] == "appended_not_found"
    assert res["highlighted"] is True
    assert res["highlight_color"] == "red"
    assert res["highlighted_red"] is True
    assert res["row"] == 3 # Appended at next row after row 2
    assert res["payment_cell"] == "I3" # Cash column is I
    assert len(highlight_called) == 1
    assert highlight_called[0][3] == "red"


# ---------------------------------------------------------------------------
# 4. Web Endpoints Integration Tests
# ---------------------------------------------------------------------------

def test_api_sheet_configs(client):
    res = client.get("/api/sheet-configs")
    assert res.status_code == 200
    data = res.get_json()
    assert "configs" in data

    # POST save
    post_res = client.post("/api/sheet-configs", json={
        "configs": {"old": {"order_col": "D", "eft_col": "H"}}
    })
    assert post_res.status_code == 200
    assert post_res.get_json()["success"] is True


def test_api_dealers_sheet_type_and_batch(client):
    res = client.post("/api/dealers/sheet-type", json={
        "company": "speedy",
        "name": "Alen",
        "sheet_type": "old"
    })
    assert res.status_code == 200
    assert res.get_json()["sheet_type"] == "old"

    batch_res = client.post("/api/dealers/batch-sheet-types", json={
        "updates": [{"company_key": "speedy", "dealer_name": "Alen", "sheet_type": "old"}]
    })
    assert batch_res.status_code == 200
    assert batch_res.get_json()["applied_count"] == 1


def test_api_payments_parse(client):
    res = client.post("/api/payments/parse", json={
        "text": "ORD-0246 : 500 : John : EFT : Mike",
        "default_date": "2026-03-24",
    })
    assert res.status_code == 200
    data = res.get_json()
    assert data["count"] == 1
    assert data["payments"][0]["order_number"] == "ORD-0246"
    assert data["payments"][0]["amount"] == 500.0


def test_api_payments_push_batch(client, monkeypatch):
    def mock_push(p):
        return {"status": "success", "row": 10, "payment_cell": "H10"}
    monkeypatch.setattr(payments_service, "push_payment_to_sheet", mock_push)

    res = client.post("/api/payments/push-batch", json={
        "payments": [
            {"order_number": "ORD-0246", "amount": 500, "dealer": "VT Thomas"},
            {"order_number": "ORD-0247", "amount": 300, "dealer": "Mike"},
        ]
    })
    assert res.status_code == 200
    data = res.get_json()
    assert data["total"] == 2
    assert data["succeeded"] == 2


def test_minus_toggle_only_affects_sheet_not_erp(monkeypatch):
    """
    Verify that when minus is toggled (is_negative=True):
    1. Google Sheet receives negative amount (-500.0).
    2. ERP receives positive absolute amount (+500.0) without any minus.
    """
    mock_erp_payment = MagicMock(return_value={"success": True, "line_id": 999})
    monkeypatch.setattr(payments_service.erp, "record_dealer_payment", mock_erp_payment)

    mock_sheet_push = MagicMock(return_value={"status": "success", "amount": -500.0, "row": 5})
    monkeypatch.setattr(payments_service, "push_payment_to_sheet", mock_sheet_push)

    item = {
        "entry_type": "payment",
        "order_number": "ORD-0246",
        "dealer": "VT Thomas",
        "dealer_id": 8,
        "amount": 500.0,
        "is_negative": True,
        "type": "EFT",
        "sender": "Deduction Return",
        "post_to_erp": True,
        "push_to_sheets": True,
    }

    res = payments_service.execute_transaction(item)
    assert res["success"] is True

    # 1. Verify Google Sheet received is_negative=True
    assert mock_sheet_push.call_count == 1
    passed_sheet_item = mock_sheet_push.call_args[0][0]
    assert passed_sheet_item["is_negative"] is True

    # 2. Verify ERP received positive absolute amount (+500.0)
    assert mock_erp_payment.call_count == 1
    erp_kwargs = mock_erp_payment.call_args[1]
    assert erp_kwargs["amount"] == 500.0
    assert erp_kwargs["dealer_id"] == 8

    # 3. Verify returned payload explicitly breaks down sheet vs erp amounts
    assert res["sheet_amount"] == -500.0
    assert res["erp_amount"] == 500.0


def test_push_payment_diverts_to_separate_eft_and_cash_alternates(monkeypatch):
    """
    Verify that when primary cells are filled:
    1. EFT filled -> diverts to eft_alt_col (J).
    2. Cash filled -> diverts to cash_alt_col (L).
    """
    mock_service = MagicMock()
    # 1. Order lookup returns row 4
    mock_orders = {"values": [["ORD-0100"], ["ORD-0200"], ["ORD-0246"], ["ORD-0300"]]}
    # 2. Existing primary cell check returns filled: "500"
    mock_cell_filled = {"values": [["500.00"]]}
    # 3. Existing comment check
    mock_comm = {"values": [[""]]}

    mock_service.spreadsheets().values().get().execute.side_effect = [
        mock_orders, mock_cell_filled, mock_comm, # for EFT test
        mock_orders, mock_cell_filled, mock_comm  # for Cash test
    ]
    mock_batch = MagicMock()
    mock_batch.execute.return_value = {"totalUpdatedCells": 2}
    mock_service.spreadsheets().values().batchUpdate.return_value = mock_batch
    monkeypatch.setattr(payments_service.sheets, "_get_service", lambda: mock_service)

    # 1. Test EFT diversion (primary is H, alternate must be J)
    payment_eft = {
        "order_number": "ORD-0246",
        "amount": 250.0,
        "type": "EFT",
        "dealer": "VT Thomas",
        "company_key": "speedy",
        "target_column_mode": "auto",
    }
    res_eft = payments_service.push_payment_to_sheet(payment_eft)
    assert res_eft["status"] == "success"
    assert res_eft["payment_cell"] == "J3" # Diverted to EFT Alternate (J)
    assert "Wrote to Alternate EFT Col J" in (res_eft.get("warning") or "")

    # 2. Test Cash diversion (primary is I, alternate must be L)
    payment_cash = {
        "order_number": "ORD-0246",
        "amount": 100.0,
        "type": "Cash",
        "dealer": "VT Thomas",
        "company_key": "speedy",
        "target_column_mode": "auto",
    }
    res_cash = payments_service.push_payment_to_sheet(payment_cash)
    assert res_cash["status"] == "success"
    assert res_cash["payment_cell"] == "L3" # Diverted to Cash Alternate (L)
    assert "Wrote to Alternate Cash Col L" in (res_cash.get("warning") or "")


def test_push_payment_lump_sum_appends_to_empty_row(monkeypatch):
    """
    Verify that payments without an order number (lump-sum) append to the next empty row,
    set order column to 'LUMP-SUM', and highlight in amber.
    """
    mock_service = MagicMock()
    # Sheet has 2 rows already populated
    mock_all = {"values": [["Jan", "1-Jan-2026", "ORD-0100"], ["Jan", "2-Jan-2026", "ORD-0200"]]}
    mock_service.spreadsheets().values().get().execute.return_value = mock_all
    mock_batch = MagicMock()
    mock_batch.execute.return_value = {"totalUpdatedCells": 5}
    mock_service.spreadsheets().values().batchUpdate.return_value = mock_batch
    monkeypatch.setattr(payments_service.sheets, "_get_service", lambda: mock_service)

    payment = {
        "order_number": "",  # Empty order number -> general lump-sum deposit
        "amount": 2500.0,
        "type": "EFT",
        "sender": "Apex Wire",
        "dealer": "VT Thomas",
        "company_key": "speedy",
    }
    res = payments_service.push_payment_to_sheet(payment)
    assert res["status"] == "lump_sum_appended"
    assert res["order_number"] == "LUMP-SUM"
    assert res["row"] == 3  # Appended at empty row 3
    assert res["payment_cell"] == "H3"
    assert res["highlight_color"] == "amber"


def test_sheets_write_order_never_touches_payment_or_lump_sum_row(monkeypatch):
    """
    Verify that when processing WhatsApp orders via sheets.write_order(),
    any row containing a payment (in H/I/J/L) or a LUMP-SUM deposit is considered
    occupied and WhatsApp orders append BELOW it, never overwriting it.
    """
    from speedy_blinds import sheets

    mock_service = MagicMock()
    # Row 1: Header/Order
    # Row 2: WhatsApp order ORD-0100
    # Row 3: General LUMP-SUM payment (only has Order# 'LUMP-SUM' and Payment in H=2500.0)
    mock_sheet_rows = {
        "values": [
            ["January", "1-Jan-2026", "ORD-0001", "Customer A", "2", "300.0"],
            ["January", "2-Jan-2026", "ORD-0100", "Customer B", "1", "150.0"],
            ["", "", "LUMP-SUM", "", "", "", "", "2500.0", "", "", "2026-11-09-Wire"], # Row 3 is a Payment row!
        ]
    }
    mock_service.spreadsheets().values().get().execute.return_value = mock_sheet_rows
    mock_update = MagicMock()
    mock_update.execute.return_value = {"updatedRange": "'2026'!B4:G4"}
    mock_service.spreadsheets().values().update.return_value = mock_update
    monkeypatch.setattr(sheets, "_get_service", lambda: mock_service)

    # Now write a new incoming WhatsApp order
    new_whatsapp_order = {
        "dealer": "VT Thomas",
        "date": "2026-03-24",
        "order_number": "ORD-0246",
        "customer_name": "New Customer",
        "qty": 3,
        "price": 450.0,
        "type": "order",
    }
    res = sheets.write_order(new_whatsapp_order)

    # Verify write_order targeted Row 4 (NOT overwriting Row 3!)
    update_call_kwargs = mock_service.spreadsheets().values().update.call_args[1]
    assert "4" in update_call_kwargs["range"]
    assert "3" not in update_call_kwargs["range"].split("!")[1]


def test_execute_transaction_lump_sum_omits_order_number_in_erp(monkeypatch):
    """
    Verify that for a general lump-sum deposit, ERP record_dealer_payment is called
    with order_number=None so ERP treats it as an unlinked general balance credit.
    """
    mock_erp_payment = MagicMock(return_value={"success": True, "line_id": 101})
    monkeypatch.setattr(payments_service.erp, "record_dealer_payment", mock_erp_payment)
    monkeypatch.setattr(payments_service, "push_payment_to_sheet", lambda p: {"status": "lump_sum_appended", "row": 5})

    item = {
        "entry_type": "payment",
        "order_number": "LUMP-SUM",
        "dealer": "VT Thomas",
        "dealer_id": 8,
        "amount": 1800.0,
        "type": "EFT",
        "sender": "Monthly Advance",
        "post_to_erp": True,
        "push_to_sheets": True,
    }

    res = payments_service.execute_transaction(item)
    assert res["success"] is True

    # Assert ERP record_dealer_payment was called with order_number=None
    assert mock_erp_payment.call_count == 1
    erp_kwargs = mock_erp_payment.call_args[1]
    assert erp_kwargs["order_number"] is None
    assert erp_kwargs["amount"] == 1800.0
    assert erp_kwargs["dealer_id"] == 8


def test_dealer_smart_alias_resolution_for_full_names():
    """
    Verify that full names from ERP or user entry like 'Jeffrin Francis' and 'Komalpreet Singh'
    correctly resolve to their canonical dealers and Google Sheets.
    """
    jeff_info = dealers_manager.get_dealer_info("Jeffrin Francis")
    assert jeff_info is not None
    assert jeff_info["dealer_name"] == "Jeff"
    assert jeff_info["sheet_id"] == "1o8DwXb-yN_xjLp0L8xIA29swgFE9aYzUuD6gq3BeibU"

    komal_info = dealers_manager.get_dealer_info("Komalpreet Singh")
    assert komal_info is not None
    assert komal_info["dealer_name"] == "Komal"
    assert komal_info["sheet_id"] == "1T3WNko2mYnG6uUBUtHkKlu1lAglnHU6wiI8Xg7DQfxM"

    phil_info = dealers_manager.get_dealer_info("Philemon Chacko")
    assert phil_info is not None
    assert phil_info["dealer_name"] == "Phil"


def test_whatsapp_order_target_columns_configurable():
    """
    Verify that WhatsApp order writing builds the exact row values and target range
    according to configured columns (both default old/new layouts and custom column layouts).
    """
    from speedy_blinds import sheets

    order = {
        "date": "2026-06-01",
        "order_number": "ORD-0246",
        "customer_name": "John Smith",
        "qty": 3,
        "price": 450.50,
        "dealer": "Phil",
        "type": "order",
    }

    # 1. Default Old Sheet columns: Month=A, Date=C, Order=D, Customer=E, Qty=F, Price=G
    old_cfg = {
        "tab_name": "2026",
        "month_col": "A",
        "date_col": "C",
        "order_col": "D",
        "customer_col": "E",
        "qty_col": "F",
        "price_col": "G",
    }
    row_old, range_old = sheets._build_order_row_and_range(order, 10, cfg=old_cfg, sheet_type="old")
    assert range_old == "2026!A10:G10"
    # Row length = 7 (A to G), with empty B column
    assert row_old == ["June", "", "1-June-2026", "ORD-0246", "John Smith", 3, 450.50]

    # 2. Default New Sheet columns: Month=B, Date=C, Order=D, Customer=E, Qty=F, Price=G
    new_cfg = {
        "tab_name": "2026",
        "month_col": "B",
        "date_col": "C",
        "order_col": "D",
        "customer_col": "E",
        "qty_col": "F",
        "price_col": "G",
    }
    row_new, range_new = sheets._build_order_row_and_range(order, 10, cfg=new_cfg, sheet_type="new")
    assert range_new == "2026!B10:G10"
    # Row length = 6 (B to G), contiguous
    assert row_new == ["June", "1-June-2026", "ORD-0246", "John Smith", 3, 450.50]

    # 3. Custom Configured Columns: Month=B, Date=D, Order=E, Customer=F, Qty=G, Price=H
    custom_cfg = {
        "tab_name": "CustomTab",
        "month_col": "B",
        "date_col": "D",
        "order_col": "E",
        "customer_col": "F",
        "qty_col": "G",
        "price_col": "H",
    }
    row_cust, range_cust = sheets._build_order_row_and_range(order, 25, cfg=custom_cfg)
    assert range_cust == "CustomTab!B25:H25"
    assert row_cust[0] == "June"         # B
    assert row_cust[1] == ""             # C (skipped)
    assert row_cust[2] == "1-June-2026"  # D
    assert row_cust[3] == "ORD-0246"     # E
    assert row_cust[4] == "John Smith"   # F
    assert row_cust[5] == 3              # G
    assert row_cust[6] == 450.50         # H






