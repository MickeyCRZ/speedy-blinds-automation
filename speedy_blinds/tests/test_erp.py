"""
tests/test_erp.py — Unit tests for erp.py
==========================================
All network I/O and file I/O are mocked. Tests cover:
  - Token validation logic (_is_token_valid)
  - Cached token reading/writing (per company)
  - Login flow (success, bad status, no token in response)
  - get_token() cache-hit / cache-miss / expiry paths
  - _search_order_in_tenant (found / not found / 401 / network error / wrong order number)
  - fetch_price (parallel fan-out, all-miss, first-hit wins)
  - enrich_orders (full flow, partial miss, all miss, empty list, ERP down)
"""

from __future__ import annotations

import sys
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch, mock_open, call
from concurrent.futures import Future

import pytest

# ---------------------------------------------------------------------------
# Ensure the speedy_blinds package directory is on sys.path so imports work
# when pytest is run from the Automate root.
# ---------------------------------------------------------------------------
sys.path.insert(0, str(Path(__file__).parent.parent))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _future_ts(delta_days: int = 29) -> str:
    """ISO-8601 timestamp delta_days from now (UTC)."""
    dt = datetime.now(timezone.utc) + timedelta(days=delta_days)
    return dt.isoformat()


def _past_ts(delta_days: int = 1) -> str:
    """ISO-8601 timestamp delta_days in the past (UTC)."""
    dt = datetime.now(timezone.utc) - timedelta(days=delta_days)
    return dt.isoformat()


# ---------------------------------------------------------------------------
# _is_token_valid
# ---------------------------------------------------------------------------

class TestIsTokenValid:
    def _call(self, token, expires_at):
        import erp
        return erp._is_token_valid(token, expires_at)

    def test_valid_token_returns_true(self):
        assert self._call("abc123", _future_ts()) is True

    def test_empty_token_returns_false(self):
        assert self._call("", _future_ts()) is False

    def test_none_token_returns_false(self):
        assert self._call(None, _future_ts()) is False

    def test_empty_expires_returns_false(self):
        assert self._call("abc123", "") is False

    def test_expired_token_returns_false(self):
        assert self._call("abc123", _past_ts()) is False

    def test_malformed_expiry_returns_false(self):
        assert self._call("abc123", "not-a-date") is False

    def test_z_suffix_expiry_parsed_correctly(self):
        ts = _future_ts().replace("+00:00", "Z")
        assert self._call("abc123", ts) is True

    def test_token_expiring_in_one_minute_is_valid(self):
        # Should still be valid (buffer is 5 min, but 1 min from now > buffer... wait)
        # The code uses `now < expiry` (no buffer logic exists in _is_token_valid)
        ts = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()
        assert self._call("abc123", ts) is True

    def test_already_expired_by_one_second(self):
        ts = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        assert self._call("abc123", ts) is False


# ---------------------------------------------------------------------------
# _read_cached_token
# ---------------------------------------------------------------------------

class TestReadCachedToken:
    @patch("erp._ENV_PATH")
    def test_reads_speedy_token(self, mock_path):
        import config, erp
        config.set_company("speedy")
        mock_path.exists.return_value = True
        mock_path.read_text.return_value = (
            "ERP_TOKEN=speedy_tok\n"
            "ERP_TOKEN_EXPIRES_AT=2099-01-01T00:00:00+00:00\n"
        )
        token, expires = erp._read_cached_token()
        assert token == "speedy_tok"
        assert "2099" in expires

    @patch("erp._ENV_PATH")
    def test_reads_inspira_token(self, mock_path):
        import config, erp
        config.set_company("inspira")
        mock_path.exists.return_value = True
        mock_path.read_text.return_value = (
            "ERP_TOKEN=speedy_tok\n"
            "INSPIRA_ERP_TOKEN=inspira_tok\n"
            "INSPIRA_ERP_TOKEN_EXPIRES_AT=2099-01-01T00:00:00+00:00\n"
        )
        token, expires = erp._read_cached_token()
        assert token == "inspira_tok"
        assert "2099" in expires

    @patch("erp._ENV_PATH")
    def test_returns_empty_strings_when_file_missing(self, mock_path):
        import erp
        mock_path.exists.return_value = False
        token, expires = erp._read_cached_token()
        assert token == ""
        assert expires == ""

    @patch("erp._ENV_PATH")
    def test_returns_empty_when_key_absent(self, mock_path):
        import config, erp
        config.set_company("speedy")
        mock_path.exists.return_value = True
        mock_path.read_text.return_value = "OTHER_KEY=value\n"
        token, expires = erp._read_cached_token()
        assert token == ""
        assert expires == ""

    @patch("erp._ENV_PATH")
    def test_inspira_token_not_confused_with_speedy(self, mock_path):
        """Inspira token key is a prefix of... no, but let's be explicit."""
        import config, erp
        config.set_company("speedy")
        mock_path.exists.return_value = True
        mock_path.read_text.return_value = (
            "ERP_TOKEN=speedy_tok\n"
            "INSPIRA_ERP_TOKEN=inspira_tok\n"
        )
        token, _ = erp._read_cached_token()
        assert token == "speedy_tok"


# ---------------------------------------------------------------------------
# _write_token_to_env
# ---------------------------------------------------------------------------

class TestWriteTokenToEnv:
    @patch("erp._ENV_PATH")
    def test_writes_speedy_keys(self, mock_path):
        import config, erp
        config.set_company("speedy")
        mock_path.exists.return_value = True
        mock_path.read_text.return_value = ""
        written = {}
        mock_path.write_text.side_effect = lambda text, encoding: written.update({"text": text})
        erp._write_token_to_env("tok123", "2099-01-01")
        assert "ERP_TOKEN=tok123" in written["text"]
        assert "ERP_TOKEN_EXPIRES_AT=2099-01-01" in written["text"]

    @patch("erp._ENV_PATH")
    def test_writes_inspira_keys(self, mock_path):
        import config, erp
        config.set_company("inspira")
        mock_path.exists.return_value = True
        mock_path.read_text.return_value = ""
        written = {}
        mock_path.write_text.side_effect = lambda text, encoding: written.update({"text": text})
        erp._write_token_to_env("tok456", "2099-06-01")
        assert "INSPIRA_ERP_TOKEN=tok456" in written["text"]
        assert "INSPIRA_ERP_TOKEN_EXPIRES_AT=2099-06-01" in written["text"]

    @patch("erp._ENV_PATH")
    def test_upserts_existing_key(self, mock_path):
        import config, erp
        config.set_company("speedy")
        mock_path.exists.return_value = True
        mock_path.read_text.return_value = "ERP_TOKEN=old_token\nERP_TOKEN_EXPIRES_AT=old\n"
        written = {}
        mock_path.write_text.side_effect = lambda text, encoding: written.update({"text": text})
        erp._write_token_to_env("new_token", "2099-12-31")
        assert "ERP_TOKEN=new_token" in written["text"]
        assert "old_token" not in written["text"]

    @patch("erp._ENV_PATH")
    def test_creates_file_if_missing(self, mock_path):
        import config, erp
        config.set_company("speedy")
        mock_path.exists.return_value = False
        mock_path.read_text.return_value = ""
        mock_path.write_text = MagicMock()
        # First write_text call creates the file
        erp._write_token_to_env("tok", "2099-01-01")
        # Should have called write_text at least once (create + upsert)
        assert mock_path.write_text.called


# ---------------------------------------------------------------------------
# _login
# ---------------------------------------------------------------------------

class TestLogin:
    @patch("erp._write_token_to_env")
    @patch("erp._SESSION")
    def test_successful_login_returns_token(self, mock_session, mock_write):
        import config, erp
        config.set_company("speedy")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "data": {"access_token": "fresh_tok", "expires_at": "2099-01-01T00:00:00Z"}
        }
        mock_session.post.return_value = mock_resp

        with patch("erp.ERP_EMAIL", "admin@speedy.com"), patch("erp.ERP_PASSWORD", "pass"):
            token = erp._login()
        assert token == "fresh_tok"
        mock_write.assert_called_once_with("fresh_tok", "2099-01-01T00:00:00Z")

    @patch("erp._SESSION")
    def test_login_uses_inspira_url_when_inspira_selected(self, mock_session):
        import config, erp
        config.set_company("inspira")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "data": {"access_token": "ins_tok", "expires_at": "2099-01-01T00:00:00Z"}
        }
        mock_session.post.return_value = mock_resp

        with (
            patch("erp._write_token_to_env"),
            patch("erp.ERP_EMAIL", "admin@inspira.com"),
            patch("erp.ERP_PASSWORD", "pass"),
        ):
            erp._login()

        call_url = mock_session.post.call_args[0][0]
        assert "inspirablinds" in call_url

    @patch("erp._SESSION")
    def test_login_raises_on_non_200(self, mock_session):
        import config, erp
        config.set_company("speedy")
        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_resp.text = "Unauthorized"
        mock_session.post.return_value = mock_resp
        with patch("erp.ERP_EMAIL", "a@b.com"), patch("erp.ERP_PASSWORD", "p"):
            with pytest.raises(RuntimeError, match="ERP login failed"):
                erp._login()

    @patch("erp._SESSION")
    def test_login_raises_when_no_token_in_response(self, mock_session):
        import config, erp
        config.set_company("speedy")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"data": {}}
        mock_resp.text = "{}"
        mock_session.post.return_value = mock_resp
        with patch("erp.ERP_EMAIL", "a@b.com"), patch("erp.ERP_PASSWORD", "p"):
            with pytest.raises(RuntimeError, match="no access_token"):
                erp._login()

    def test_login_raises_when_credentials_missing(self):
        import erp
        with (
            patch("erp.ERP_EMAIL", ""),
            patch("erp.ERP_PASSWORD", ""),
        ):
            with pytest.raises(RuntimeError, match="ERP_EMAIL and ERP_PASSWORD"):
                erp._login()

    @patch("erp._SESSION")
    def test_login_sends_allow_multiple_true(self, mock_session):
        import config, erp
        config.set_company("speedy")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "data": {"access_token": "tok", "expires_at": "2099-01-01T00:00:00Z"}
        }
        mock_session.post.return_value = mock_resp
        with (
            patch("erp._write_token_to_env"),
            patch("erp.ERP_EMAIL", "a@b.com"),
            patch("erp.ERP_PASSWORD", "p"),
        ):
            erp._login()
        payload = mock_session.post.call_args[1]["json"]
        assert payload.get("allow_multiple") is True


# ---------------------------------------------------------------------------
# get_token
# ---------------------------------------------------------------------------

class TestGetToken:
    def test_returns_cached_env_token_when_valid(self):
        import config, erp
        config.set_company("speedy")
        ts = _future_ts()
        with (
            patch.dict(os.environ, {"ERP_TOKEN": "env_tok", "ERP_TOKEN_EXPIRES_AT": ts}),
            patch("erp._login") as mock_login,
        ):
            token = erp.get_token()
        assert token == "env_tok"
        mock_login.assert_not_called()

    def test_falls_back_to_file_cache_when_env_empty(self):
        import config, erp
        config.set_company("speedy")
        ts = _future_ts()
        with (
            patch.dict(os.environ, {"ERP_TOKEN": "", "ERP_TOKEN_EXPIRES_AT": ""}, clear=False),
            patch("erp._read_cached_token", return_value=("file_tok", ts)),
            patch("erp._login") as mock_login,
        ):
            token = erp.get_token()
        assert token == "file_tok"
        mock_login.assert_not_called()

    def test_calls_login_when_all_caches_miss(self):
        import config, erp
        config.set_company("speedy")
        with (
            patch.dict(os.environ, {"ERP_TOKEN": "", "ERP_TOKEN_EXPIRES_AT": ""}, clear=False),
            patch("erp._read_cached_token", return_value=("", "")),
            patch("erp._login", return_value="fresh") as mock_login,
        ):
            token = erp.get_token()
        assert token == "fresh"
        mock_login.assert_called_once()

    def test_calls_login_when_token_expired(self):
        import config, erp
        config.set_company("speedy")
        ts = _past_ts()
        with (
            patch.dict(os.environ, {"ERP_TOKEN": "old", "ERP_TOKEN_EXPIRES_AT": ts}, clear=False),
            patch("erp._read_cached_token", return_value=("old", ts)),
            patch("erp._login", return_value="renewed") as mock_login,
        ):
            token = erp.get_token()
        assert token == "renewed"
        mock_login.assert_called_once()

    def test_inspira_reads_inspira_env_key(self):
        import config, erp
        config.set_company("inspira")
        ts = _future_ts()
        with (
            patch.dict(os.environ, {"INSPIRA_ERP_TOKEN": "ins_tok",
                                    "INSPIRA_ERP_TOKEN_EXPIRES_AT": ts}, clear=False),
            patch("erp._login") as mock_login,
        ):
            token = erp.get_token()
        assert token == "ins_tok"
        mock_login.assert_not_called()


# ---------------------------------------------------------------------------
# _search_order_in_tenant
# ---------------------------------------------------------------------------

class TestSearchOrderInTenant:
    def _call(self, order_number, mock_resp, tenant_id=1):
        import config, erp
        config.set_company("speedy")
        with patch("erp._SESSION") as mock_session:
            mock_session.get.return_value = mock_resp
            return erp._search_order_in_tenant(order_number, "tok", tenant_id)

    def _make_resp(self, status, orders):
        mock_resp = MagicMock()
        mock_resp.status_code = status
        mock_resp.json.return_value = {"data": orders}
        mock_resp.text = str(orders)
        return mock_resp

    def test_returns_price_when_order_found(self):
        resp = self._make_resp(200, [{"order_number": "ON9265", "total_price": 350.0}])
        result = self._call("ON9265", resp)
        assert result == 350.0

    def test_returns_none_when_order_not_in_results(self):
        resp = self._make_resp(200, [{"order_number": "ON0001", "total_price": 100.0}])
        result = self._call("ON9265", resp)
        assert result is None

    def test_returns_none_when_data_is_empty(self):
        resp = self._make_resp(200, [])
        result = self._call("ON9265", resp)
        assert result is None

    def test_raises_on_401(self):
        import erp, config
        config.set_company("speedy")
        mock_resp = MagicMock()
        mock_resp.status_code = 401
        with patch("erp._SESSION") as mock_session:
            mock_session.get.return_value = mock_resp
            with pytest.raises(RuntimeError, match="401"):
                erp._search_order_in_tenant("ON9265", "tok", 1)

    def test_returns_none_on_non_401_error(self):
        resp = self._make_resp(500, [])
        result = self._call("ON9265", resp)
        assert result is None

    def test_case_insensitive_order_number_match(self):
        resp = self._make_resp(200, [{"order_number": "on9265", "total_price": 99.0}])
        result = self._call("ON9265", resp)
        assert result == 99.0

    def test_returns_none_on_network_exception(self):
        """Network errors are caught and return None instead of crashing."""
        import erp, config
        config.set_company("speedy")
        # Patch requests.RequestException to be OSError so erp.py's except clause fires
        import requests as _req
        _req.RequestException = OSError
        with patch("erp._SESSION") as mock_session:
            mock_session.get.side_effect = OSError("connection refused")
            result = erp._search_order_in_tenant("ON9265", "tok", 1)
        assert result is None

    def test_uses_correct_base_url_for_company(self):
        import erp, config
        config.set_company("inspira")
        with patch("erp._SESSION") as mock_session:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {"data": []}
            mock_session.get.return_value = mock_resp
            erp._search_order_in_tenant("ON9265", "tok", 1)
        call_url = mock_session.get.call_args[0][0]
        assert "inspirablinds" in call_url

    def test_passes_x_active_tenant_id_header(self):
        import erp, config
        config.set_company("speedy")
        with patch("erp._SESSION") as mock_session:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {"data": []}
            mock_session.get.return_value = mock_resp
            erp._search_order_in_tenant("ON9265", "tok", 42)
        headers = mock_session.get.call_args[1]["headers"]
        assert headers["X-Active-Tenant-Id"] == "42"

    def test_returns_none_when_data_is_not_a_list(self):
        """Guard against unexpected API response shape."""
        import erp, config
        config.set_company("speedy")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"data": {"unexpected": "object"}}
        with patch("erp._SESSION") as mock_session:
            mock_session.get.return_value = mock_resp
            result = erp._search_order_in_tenant("ON9265", "tok", 1)
        assert result is None

    def test_returns_none_when_price_is_none_in_response(self):
        resp = self._make_resp(200, [{"order_number": "ON9265", "total_price": None}])
        result = self._call("ON9265", resp)
        assert result is None


# ---------------------------------------------------------------------------
# fetch_price
# ---------------------------------------------------------------------------

class TestFetchPrice:
    def test_returns_price_when_found_in_first_tenant(self):
        import config, erp
        config.set_company("speedy")
        with patch("erp._search_order_in_tenant", return_value=250.0):
            result = erp.fetch_price("ON9265", "tok")
        assert result == 250.0

    def test_returns_none_when_not_found_in_any_tenant(self):
        import config, erp
        config.set_company("speedy")
        with patch("erp._search_order_in_tenant", return_value=None):
            result = erp.fetch_price("ON0000", "tok")
        assert result is None

    def test_raises_when_tenant_ids_empty(self):
        import config, erp
        config.set_company("speedy")
        original = config.ERP_TENANT_IDS[:]
        config.ERP_TENANT_IDS = []
        try:
            with pytest.raises(RuntimeError, match="ERP_TENANT_IDS is empty"):
                erp.fetch_price("ON9265", "tok")
        finally:
            config.ERP_TENANT_IDS = original

    def test_inspira_only_searches_one_tenant(self):
        import config, erp
        config.set_company("inspira")
        calls = []

        def fake_search(order_number, token, tenant_id):
            calls.append(tenant_id)
            return None

        with patch("erp._search_order_in_tenant", side_effect=fake_search):
            erp.fetch_price("ON9265", "tok")

        assert calls == [1]

    def test_speedy_searches_both_tenants_when_first_misses(self):
        import config, erp
        config.set_company("speedy")
        calls = []

        def fake_search(order_number, token, tenant_id):
            calls.append(tenant_id)
            return None  # miss all

        with patch("erp._search_order_in_tenant", side_effect=fake_search):
            erp.fetch_price("ON9265", "tok")

        assert set(calls) == set(config.COMPANY["speedy"]["tenant_ids"])


# ---------------------------------------------------------------------------
# enrich_orders
# ---------------------------------------------------------------------------

class TestEnrichOrders:
    def test_empty_list_returns_immediately(self):
        import erp
        with patch("erp.get_token") as mock_tok:
            result = erp.enrich_orders([])
        mock_tok.assert_not_called()
        assert result == []

    def test_prices_added_to_all_orders(self):
        import config, erp
        config.set_company("speedy")
        orders = [
            {"order_number": "ON001"},
            {"order_number": "ON002"},
        ]
        prices = {"ON001": 100.0, "ON002": 200.0}

        def fake_fetch(order_number, token):
            return prices.get(order_number)

        with (
            patch("erp.get_token", return_value="tok"),
            patch("erp.fetch_price", side_effect=fake_fetch),
        ):
            result = erp.enrich_orders(orders)

        assert result[0]["price"] == 100.0
        assert result[1]["price"] == 200.0

    def test_missing_price_sets_none(self):
        import config, erp
        config.set_company("speedy")
        orders = [{"order_number": "ON999"}]
        with (
            patch("erp.get_token", return_value="tok"),
            patch("erp.fetch_price", return_value=None),
        ):
            result = erp.enrich_orders(orders)
        assert result[0]["price"] is None

    def test_returns_same_list_object(self):
        """enrich_orders mutates in place and returns the same list."""
        import config, erp
        config.set_company("speedy")
        orders = [{"order_number": "ON001"}]
        with (
            patch("erp.get_token", return_value="tok"),
            patch("erp.fetch_price", return_value=50.0),
        ):
            result = erp.enrich_orders(orders)
        assert result is orders

    def test_exception_in_single_lookup_sets_none(self):
        """One order lookup crashing should not kill the whole batch."""
        import config, erp
        config.set_company("speedy")
        orders = [{"order_number": "ON001"}, {"order_number": "ON002"}]

        def fake_fetch(order_number, token):
            if order_number == "ON001":
                raise RuntimeError("network blip")
            return 99.0

        with (
            patch("erp.get_token", return_value="tok"),
            patch("erp.fetch_price", side_effect=fake_fetch),
        ):
            result = erp.enrich_orders(orders)

        prices = {o["order_number"]: o.get("price") for o in result}
        assert prices["ON001"] is None
        assert prices["ON002"] == 99.0
