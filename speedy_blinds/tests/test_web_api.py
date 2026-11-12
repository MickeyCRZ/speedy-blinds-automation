"""
test_web_api.py — Unit & Integration tests for the Flask Web Application
=========================================================================
"""

import sys
from pathlib import Path
import pytest

# Ensure root dir in path
root_dir = Path(__file__).parent.parent.parent
if str(root_dir) not in sys.path:
    sys.path.insert(0, str(root_dir))

import app


@pytest.fixture
def client():
    app.app.config["TESTING"] = True
    with app.app.test_client() as client:
        yield client


class TestWebPages:
    def test_index_page_returns_200(self, client):
        res = client.get("/")
        assert res.status_code == 200
        assert b"Dealer Order Automation" in res.data
        assert b"WhatsApp Orders" in res.data
        assert b"MIR Calculator" in res.data

    def test_404_api_returns_json(self, client):
        res = client.get("/api/nonexistent")
        assert res.status_code == 404
        assert res.is_json
        assert "error" in res.get_json()


class TestSystemAndCompanyAPIs:
    def test_get_companies(self, client):
        res = client.get("/api/companies")
        assert res.status_code == 200
        data = res.get_json()
        assert "companies" in data
        keys = [c["key"] for c in data["companies"]]
        assert "speedy" in keys
        assert "inspira" in keys
        assert "houston" in keys

    def test_get_system_status(self, client):
        res = client.get("/api/status")
        assert res.status_code == 200
        data = res.get_json()
        assert "active_company" in data
        assert "integrations" in data
        assert "tokens" in data
        assert data["active_company"]["key"] in ("speedy", "inspira", "houston")

    def test_switch_company(self, client):
        res = client.post("/api/company", json={"company": "inspira"})
        assert res.status_code == 200
        assert res.get_json()["company_key"] == "inspira"

        # Switch back to speedy
        res = client.post("/api/company", json={"company": "speedy"})
        assert res.status_code == 200
        assert res.get_json()["company_key"] == "speedy"

    def test_get_dealers(self, client):
        res = client.get("/api/dealers?company=speedy")
        assert res.status_code == 200
        data = res.get_json()
        assert "dealers" in data
        assert "VT Thomas" in data["dealers"]


class TestAuditLogsAPI:
    def test_get_audit_logs(self, client):
        res = client.get("/api/logs?limit=10")
        assert res.status_code == 200
        data = res.get_json()
        assert "logs" in data
        assert isinstance(data["logs"], list)


class TestCredentialsAPI:
    def test_get_credentials_info(self, client):
        res = client.get("/api/credentials")
        assert res.status_code == 200
        data = res.get_json()
        assert "groq" in data
        assert "sheets" in data
        assert "erp" in data
        assert "email" in data
        assert "whatsapp" in data
        assert "env_content" in data

    def test_test_sheets_connection(self, client, monkeypatch):
        import sheets
        from unittest.mock import MagicMock
        mock_svc = MagicMock()
        mock_svc.spreadsheets().get().execute.return_value = {
            "properties": {"title": "Mock Sales Statement"}
        }
        monkeypatch.setattr(sheets, "_get_service", lambda: mock_svc)

        res = client.post("/api/credentials/test-sheets")
        assert res.status_code == 200
        data = res.get_json()
        assert data.get("success") is True
        assert data.get("spreadsheet_title") == "Mock Sales Statement"


class TestDealerManagementAPI:
    def test_get_all_dealers(self, client):
        res = client.get("/api/dealers/all")
        assert res.status_code == 200
        data = res.get_json()
        assert "dealers" in data
        assert len(data["dealers"]) >= 36

    def test_add_edit_and_delete_dealer(self, client):
        # 1. Add dealer with a full Google Sheets URL
        full_url = "https://docs.google.com/spreadsheets/d/1vf8lvNBBFLCeTJlUIwQK5i6Qb0ay9KbyCNGnfRFEiP8/edit#gid=0"
        res = client.post("/api/dealers/save", json={
            "company": "houston",
            "name": "Integration Test Dealer",
            "sheet_url": full_url,
            "aliases": "itd, integration, test dealer"
        })
        assert res.status_code == 200
        saved = res.get_json()
        assert saved["dealer_name"] == "Integration Test Dealer"
        assert saved["sheet_id"] == "1vf8lvNBBFLCeTJlUIwQK5i6Qb0ay9KbyCNGnfRFEiP8"
        assert "itd" in saved["aliases"]

        # 2. Edit dealer
        res = client.post("/api/dealers/save", json={
            "company": "houston",
            "name": "Integration Test Dealer Renamed",
            "sheet_url": "1vf8lvNBBFLCeTJlUIwQK5i6Qb0ay9KbyCNGnfRFEiP8",
            "aliases": "renamed, itd",
            "original_name": "Integration Test Dealer"
        })
        assert res.status_code == 200

        # 3. Delete dealer
        res = client.post("/api/dealers/delete", json={
            "company": "houston",
            "name": "Integration Test Dealer Renamed"
        })
        assert res.status_code == 200


class TestInputValidations:
    def test_orders_parse_empty_text(self, client):
        res = client.post("/api/orders/parse", json={"text": ""})
        assert res.status_code == 400
        assert "error" in res.get_json()

    def test_mir_parse_batch_empty_text(self, client):
        res = client.post("/api/mir/parse-batch", json={"installer": "John", "text": ""})
        assert res.status_code == 400

    def test_bulk_lookup_empty_text(self, client):
        res = client.post("/api/bulk-complete/lookup", json={"text": ""})
        assert res.status_code == 400

    def test_price_checker_empty_text(self, client):
        res = client.post("/api/price-checker/run", json={"text": ""})
        assert res.status_code == 400

    def test_reworks_empty_range(self, client):
        res = client.post("/api/reworks/process", json={"range": ""})
        assert res.status_code == 400


class TestOrdersPushSheetsAndEmail:
    def test_orders_push_sheets_dispatches_summary_email(self, client, monkeypatch):
        from unittest.mock import MagicMock
        import web_services

        # Mock sheets write in web_services
        monkeypatch.setattr(
            web_services.sheets,
            "write_order",
            lambda o: {"updates": {"updatedRange": "'2026'!B10:G10"}},
        )

        # Mock RunReport.dispatch in web_services
        dispatch_mock = MagicMock(return_value={
            "email_sent": True,
            "email_to": "notifications@example.com",
            "email_error": None,
        })
        monkeypatch.setattr(web_services.notifier.RunReport, "dispatch", dispatch_mock)

        order_payload = [{
            "order_number": "ORD-0246",
            "dealer": "VT Thomas",
            "date": "2026-06-09",
            "customer_name": "benni",
            "qty": 17,
            "price": 250.0,
            "type": "order",
        }]

        res = client.post(
            "/api/orders/push-sheets",
            json={"orders": order_payload, "company": "speedy", "send_email": True},
        )
        assert res.status_code == 200
        data = res.get_json()
        assert data["total_written"] == 1
        assert data["email_sent"] is True
        assert data["email_to"] == "notifications@example.com"
        assert dispatch_mock.called

    def test_orders_push_sheets_can_skip_email_when_flag_false(self, client, monkeypatch):
        from unittest.mock import MagicMock
        import web_services

        monkeypatch.setattr(
            web_services.sheets,
            "write_order",
            lambda o: {"updates": {"updatedRange": "'2026'!B11:G11"}},
        )
        dispatch_mock = MagicMock()
        monkeypatch.setattr(web_services.notifier.RunReport, "dispatch", dispatch_mock)

        order_payload = [{
            "order_number": "ORD-0198",
            "dealer": "Alen",
            "date": "2026-06-09",
            "customer_name": "Shakeel",
            "qty": 4,
            "price": 120.0,
            "type": "order",
        }]

        res = client.post(
            "/api/orders/push-sheets",
            json={"orders": order_payload, "company": "speedy", "send_email": False},
        )
        assert res.status_code == 200
        data = res.get_json()
        assert data["total_written"] == 1
        assert data["email_sent"] is False
        assert not dispatch_mock.called

    def test_orders_push_holds_email_and_prompts_when_error_occurs(self, client, monkeypatch):
        import web_services

        def mock_write(o):
            if o.get("order_number") == "ORD-0608":
                raise RuntimeError("Range exceeds grid limits")
            return {"updates": {"updatedRange": "'2026'!B12:G12"}}

        monkeypatch.setattr(web_services.sheets, "write_order", mock_write)

        orders = [
            {"order_number": "ORD-0535", "dealer": "Phil", "date": "2026-11-06", "qty": 9, "price": 1245.0, "type": "order"},
            {"order_number": "ORD-0608", "dealer": "Jerin", "date": "2026-11-06", "qty": 10, "price": 1050.0, "type": "order"},
        ]

        res = client.post(
            "/api/orders/push-sheets",
            json={"orders": orders, "company": "inspira", "send_email": True},
        )
        assert res.status_code == 200
        data = res.get_json()
        assert data["total_written"] == 1
        assert data["total_errors"] == 1
        assert data["prompt_summary_email"] is True
        assert data["email_sent"] is False

    def test_send_summary_email_api_includes_failed_orders_in_totals(self, client, monkeypatch):
        from unittest.mock import MagicMock
        import web_services

        dispatch_mock = MagicMock(return_value={
            "email_sent": True,
            "email_to": "admin@example.com",
            "email_error": None,
        })
        monkeypatch.setattr(web_services.notifier.RunReport, "dispatch", dispatch_mock)

        written = [
            {"order_number": "ORD-0535", "dealer": "Phil", "date": "2026-11-06", "qty": 9, "price": 1245.0, "type": "order"}
        ]
        failed = [
            {"order_number": "ORD-0608", "dealer": "Jerin", "date": "2026-11-06", "qty": 10, "price": 1050.0, "type": "order", "_sheet_error": "Grid limit"}
        ]

        res = client.post(
            "/api/orders/send-summary-email",
            json={"orders": written, "failed_orders": failed, "include_failed": True, "company": "inspira"},
        )
        assert res.status_code == 200
        data = res.get_json()
        assert data["success"] is True
        assert data["email_sent"] is True
        assert data["total_orders"] == 2
        assert data["written_count"] == 1
        assert data["failed_count"] == 1
        assert dispatch_mock.called
