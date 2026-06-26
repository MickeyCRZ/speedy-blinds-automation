"""
tests/test_config.py — Unit tests for config.py
================================================
Tests company switching, defaults, and that set_company() correctly mutates
all ERP_* module-level variables without leaking state between tests.
"""

import importlib
import sys

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _reload_config():
    """Force a fresh import of config so module-level state is reset."""
    if "config" in sys.modules:
        del sys.modules["config"]
    import config as cfg
    return cfg


# ---------------------------------------------------------------------------
# Company definitions
# ---------------------------------------------------------------------------

class TestCompanyDefinitions:
    def test_both_companies_present(self):
        cfg = _reload_config()
        assert "speedy" in cfg.COMPANY
        assert "inspira" in cfg.COMPANY

    def test_speedy_base_url(self):
        cfg = _reload_config()
        assert cfg.COMPANY["speedy"]["base_url"] == "https://admin.speedyblinds.cloud/api/v1"

    def test_inspira_base_url(self):
        cfg = _reload_config()
        assert cfg.COMPANY["inspira"]["base_url"] == "https://admin.inspirablinds.cloud/api/v1"

    def test_speedy_has_two_tenants(self):
        cfg = _reload_config()
        assert len(cfg.COMPANY["speedy"]["tenant_ids"]) == 2

    def test_inspira_has_exactly_one_tenant(self):
        cfg = _reload_config()
        assert cfg.COMPANY["inspira"]["tenant_ids"] == [1]

    def test_token_keys_are_different(self):
        cfg = _reload_config()
        assert cfg.COMPANY["speedy"]["token_key"] != cfg.COMPANY["inspira"]["token_key"]
        assert cfg.COMPANY["speedy"]["token_expires_key"] != cfg.COMPANY["inspira"]["token_expires_key"]

    def test_speedy_token_key(self):
        cfg = _reload_config()
        assert cfg.COMPANY["speedy"]["token_key"] == "ERP_TOKEN"

    def test_inspira_token_key(self):
        cfg = _reload_config()
        assert cfg.COMPANY["inspira"]["token_key"] == "INSPIRA_ERP_TOKEN"

    def test_all_companies_have_required_keys(self):
        cfg = _reload_config()
        required = {"label", "base_url", "tenant_ids", "token_key", "token_expires_key"}
        for key, company in cfg.COMPANY.items():
            assert required.issubset(company.keys()), f"Company '{key}' missing keys"


# ---------------------------------------------------------------------------
# Default state (before set_company is called)
# ---------------------------------------------------------------------------

class TestDefaults:
    def test_default_base_url_is_speedy(self):
        cfg = _reload_config()
        assert cfg.ERP_BASE_URL == cfg.COMPANY["speedy"]["base_url"]

    def test_default_tenant_ids_are_speedy(self):
        cfg = _reload_config()
        assert cfg.ERP_TENANT_IDS == cfg.COMPANY["speedy"]["tenant_ids"]

    def test_default_token_key_is_speedy(self):
        cfg = _reload_config()
        assert cfg.ERP_TOKEN_KEY == "ERP_TOKEN"

    def test_default_token_expires_key_is_speedy(self):
        cfg = _reload_config()
        assert cfg.ERP_TOKEN_EXPIRES_KEY == "ERP_TOKEN_EXPIRES_AT"


# ---------------------------------------------------------------------------
# set_company()
# ---------------------------------------------------------------------------

class TestSetCompany:
    def test_switch_to_inspira_changes_base_url(self):
        cfg = _reload_config()
        cfg.set_company("inspira")
        assert cfg.ERP_BASE_URL == "https://admin.inspirablinds.cloud/api/v1"

    def test_switch_to_inspira_changes_tenant_ids(self):
        cfg = _reload_config()
        cfg.set_company("inspira")
        assert cfg.ERP_TENANT_IDS == [1]

    def test_switch_to_inspira_changes_token_key(self):
        cfg = _reload_config()
        cfg.set_company("inspira")
        assert cfg.ERP_TOKEN_KEY == "INSPIRA_ERP_TOKEN"

    def test_switch_to_inspira_changes_token_expires_key(self):
        cfg = _reload_config()
        cfg.set_company("inspira")
        assert cfg.ERP_TOKEN_EXPIRES_KEY == "INSPIRA_ERP_TOKEN_EXPIRES_AT"

    def test_switch_to_speedy_restores_defaults(self):
        cfg = _reload_config()
        cfg.set_company("inspira")
        cfg.set_company("speedy")
        assert cfg.ERP_BASE_URL == "https://admin.speedyblinds.cloud/api/v1"
        assert cfg.ERP_TOKEN_KEY == "ERP_TOKEN"

    def test_switch_does_not_mutate_company_dict(self):
        """set_company() must not modify the COMPANY dict itself."""
        cfg = _reload_config()
        original_inspira_url = cfg.COMPANY["inspira"]["base_url"]
        cfg.set_company("speedy")
        assert cfg.COMPANY["inspira"]["base_url"] == original_inspira_url

    def test_invalid_company_raises_key_error(self):
        cfg = _reload_config()
        with pytest.raises(KeyError):
            cfg.set_company("nonexistent")
