"""
tests/test_sheets.py — Unit tests for sheets.py
================================================
All Google API calls and file I/O are mocked. Tests cover:
  - _order_to_row (all fields, missing fields, None values, zero values)
  - write_order (happy path, unknown dealer, placeholder ID, API error, missing credentials)
  - write_orders (all succeed, some skipped, skip_unknown flag)
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


# ---------------------------------------------------------------------------
# _order_to_row
# ---------------------------------------------------------------------------

class TestOrderToRow:
    def _call(self, order):
        import sheets
        return sheets._order_to_row(order)

    def _full_order(self):
        return {
            "date": "2026-06-01",
            "order_number": "ON9265",
            "customer_name": "John Smith",
            "qty": 3,
            "amount": 450.50,
            "price": 320.00,
        }

    def test_returns_list_of_six_elements(self):
        row = self._call(self._full_order())
        assert len(row) == 6

    def test_correct_column_order(self):
        row = self._call(self._full_order())
        assert row[0] == "2026-06-01"       # B: Date
        assert row[1] == "ON9265"           # C: Order Number
        assert row[2] == "John Smith"       # D: Customer Name
        assert row[3] == 3                  # E: Qty
        assert row[4] == 450.50             # F: Amount
        assert row[5] == 320.00             # G: Price

    def test_missing_date_becomes_empty_string(self):
        order = self._full_order()
        del order["date"]
        assert self._call(order)[0] == ""

    def test_none_date_becomes_empty_string(self):
        order = self._full_order()
        order["date"] = None
        assert self._call(order)[0] == ""

    def test_missing_price_becomes_empty_string(self):
        order = self._full_order()
        del order["price"]
        assert self._call(order)[5] == ""

    def test_none_price_becomes_empty_string(self):
        order = self._full_order()
        order["price"] = None
        assert self._call(order)[5] == ""

    def test_zero_qty_preserved(self):
        order = self._full_order()
        order["qty"] = 0
        assert self._call(order)[3] == 0

    def test_zero_amount_preserved(self):
        order = self._full_order()
        order["amount"] = 0.0
        assert self._call(order)[4] == 0.0

    def test_zero_price_preserved(self):
        """0.0 is a valid price — must not become empty string."""
        order = self._full_order()
        order["price"] = 0.0
        assert self._call(order)[5] == 0.0

    def test_completely_empty_order(self):
        row = self._call({})
        assert row == ["", "", "", "", "", ""]

    def test_extra_fields_ignored(self):
        order = self._full_order()
        order["dealer"] = "Mike"
        order["unexpected"] = "value"
        row = self._call(order)
        assert len(row) == 6


# ---------------------------------------------------------------------------
# write_order
# ---------------------------------------------------------------------------

class TestWriteOrder:
    def _mock_service(self):
        service = MagicMock()
        append = service.spreadsheets.return_value.values.return_value.append.return_value
        append.execute.return_value = {"updates": {"updatedRange": "2026!B5:G5"}}
        return service

    def _order(self, dealer="VT Thomas"):
        return {
            "dealer": dealer,
            "date": "2026-06-01",
            "order_number": "ON001",
            "customer_name": "Alice",
            "qty": 1,
            "amount": 200.0,
            "price": 150.0,
        }

    def test_raises_value_error_when_no_dealer(self):
        import sheets
        order = self._order()
        del order["dealer"]
        with pytest.raises(ValueError, match="no dealer"):
            sheets.write_order(order)

    def test_raises_value_error_for_unknown_dealer(self):
        import sheets
        with pytest.raises(ValueError, match="No spreadsheet ID configured"):
            sheets.write_order(self._order(dealer="UnknownDealer"))

    def test_raises_value_error_for_placeholder_id(self):
        import sheets
        # Temporarily inject a placeholder ID for a known dealer
        with patch.dict(sheets.DEALER_SHEETS, {"VT Thomas": "SPREADSHEET_ID_HERE"}):
            with pytest.raises(ValueError, match="No spreadsheet ID configured"):
                sheets.write_order(self._order(dealer="VT Thomas"))

    def test_successful_write_returns_api_response(self):
        import sheets
        fake_id = "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms"
        with (
            patch.dict(sheets.DEALER_SHEETS, {"VT Thomas": fake_id}),
            patch("sheets._get_service", return_value=self._mock_service()),
        ):
            result = sheets.write_order(self._order())
        assert "updates" in result

    def test_raises_runtime_error_on_api_http_error(self):
        import sheets
        from googleapiclient.errors import HttpError
        from unittest.mock import MagicMock
        fake_id = "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms"

        mock_service = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status = 403
        mock_resp.reason = "Forbidden"
        mock_service.spreadsheets.return_value.values.return_value.append.return_value.execute.side_effect = (
            HttpError(resp=mock_resp, content=b"Forbidden")
        )
        with (
            patch.dict(sheets.DEALER_SHEETS, {"VT Thomas": fake_id}),
            patch("sheets._get_service", return_value=mock_service),
        ):
            with pytest.raises(RuntimeError, match="Google Sheets API error"):
                sheets.write_order(self._order())

    def test_raises_file_not_found_when_credentials_missing(self):
        import sheets
        fake_id = "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms"
        with (
            patch.dict(sheets.DEALER_SHEETS, {"VT Thomas": fake_id}),
            patch("sheets._get_service", side_effect=FileNotFoundError("credentials.json not found")),
        ):
            with pytest.raises(FileNotFoundError):
                sheets.write_order(self._order())

    def test_correct_spreadsheet_id_used(self):
        import sheets
        fake_id = "REAL_SHEET_ID_12345"
        with (
            patch.dict(sheets.DEALER_SHEETS, {"VT Thomas": fake_id}),
            patch("sheets._get_service", return_value=self._mock_service()) as mock_svc_fn,
        ):
            sheets.write_order(self._order())

        service = mock_svc_fn.return_value
        call_kwargs = service.spreadsheets.return_value.values.return_value.append.call_args[1]
        assert call_kwargs["spreadsheetId"] == fake_id

    def test_insert_data_option_is_insert_rows(self):
        """Must never overwrite existing rows."""
        import sheets
        fake_id = "REAL_SHEET_ID_12345"
        with (
            patch.dict(sheets.DEALER_SHEETS, {"VT Thomas": fake_id}),
            patch("sheets._get_service", return_value=self._mock_service()) as mock_svc_fn,
        ):
            sheets.write_order(self._order())

        service = mock_svc_fn.return_value
        call_kwargs = service.spreadsheets.return_value.values.return_value.append.call_args[1]
        assert call_kwargs["insertDataOption"] == "INSERT_ROWS"

    def test_value_input_option_is_user_entered(self):
        """USER_ENTERED respects date formatting in Sheets."""
        import sheets
        fake_id = "REAL_SHEET_ID_12345"
        with (
            patch.dict(sheets.DEALER_SHEETS, {"VT Thomas": fake_id}),
            patch("sheets._get_service", return_value=self._mock_service()) as mock_svc_fn,
        ):
            sheets.write_order(self._order())

        service = mock_svc_fn.return_value
        call_kwargs = service.spreadsheets.return_value.values.return_value.append.call_args[1]
        assert call_kwargs["valueInputOption"] == "USER_ENTERED"


# ---------------------------------------------------------------------------
# write_orders
# ---------------------------------------------------------------------------

class TestWriteOrders:
    def _order(self, dealer="VT Thomas", order_number="ON001"):
        return {
            "dealer": dealer,
            "order_number": order_number,
            "date": "2026-06-01",
            "customer_name": "Alice",
            "qty": 1,
            "amount": 200.0,
            "price": 150.0,
        }

    def test_all_succeed_returns_correct_counts(self):
        import sheets
        fake_id = "SHEET_ID"
        service = MagicMock()
        service.spreadsheets.return_value.values.return_value.append.return_value.execute.return_value = {
            "updates": {"updatedRange": "2026!B5:G5"}
        }
        orders = [self._order("VT Thomas", "ON001"), self._order("VT Thomas", "ON002")]
        with (
            patch.dict(sheets.DEALER_SHEETS, {"VT Thomas": fake_id}),
            patch("sheets._get_service", return_value=service),
        ):
            success, skipped = sheets.write_orders(orders)
        assert success == 2
        assert skipped == 0

    def test_unknown_dealer_skipped_when_skip_unknown_true(self):
        import sheets
        fake_id = "SHEET_ID"
        service = MagicMock()
        service.spreadsheets.return_value.values.return_value.append.return_value.execute.return_value = {
            "updates": {"updatedRange": "2026!B5:G5"}
        }
        orders = [
            self._order("VT Thomas", "ON001"),
            self._order("Ghost Dealer", "ON002"),
        ]
        with (
            patch.dict(sheets.DEALER_SHEETS, {"VT Thomas": fake_id}),
            patch("sheets._get_service", return_value=service),
        ):
            success, skipped = sheets.write_orders(orders, skip_unknown=True)
        assert success == 1
        assert skipped == 1

    def test_unknown_dealer_raises_when_skip_unknown_false(self):
        import sheets
        fake_id = "SHEET_ID"
        service = MagicMock()
        orders = [self._order("Ghost Dealer", "ON001")]
        with (
            patch.dict(sheets.DEALER_SHEETS, {}),
            patch("sheets._get_service", return_value=service),
        ):
            with pytest.raises(ValueError):
                sheets.write_orders(orders, skip_unknown=False)

    def test_empty_order_list_returns_zero_counts(self):
        import sheets
        success, skipped = sheets.write_orders([])
        assert success == 0
        assert skipped == 0
