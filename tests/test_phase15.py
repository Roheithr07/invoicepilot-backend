"""
Phase 15 Automated Test Suite — Multi-Sponsor Integration, Free AI Provider & Security Guardrails.
PayPal AI Hackathon 2026.
"""

import pytest
from fastapi.testclient import TestClient
from app.main import app
from app.services.ai_provider import AIProviderManager, OllamaProvider, DeterministicProvider
from app.services.zapier_service import ZapierService
from app.services.ai_extractor import AIExtractor

client = TestClient(app)


def test_ai_provider_manager_and_health():
    """Verifies that the free AI provider abstraction resolves correctly and returns compliant status."""
    provider = AIProviderManager.get_provider()
    assert provider is not None

    health = AIProviderManager.get_health()
    assert health["free_tier_compliant"] is True
    assert "deterministic_fallback_ready" in health
    assert health["deterministic_fallback_ready"] is True
    assert "cost" in health
    assert "FREE" in health["cost"]


def test_deterministic_provider_offline_resilience():
    """Verifies deterministic heuristic parser functions with zero network dependencies."""
    raw_invoice = (
        "Invoice Number: INV-2026-TEST\n"
        "Date: 2026-03-25\n"
        "Customer: Global Test Client (test@example.com)\n"
        "Item: Software Architecture Consulting - $1000.00\n"
        "Total: $1000.00"
    )
    result = AIExtractor.extract_structured_data(raw_invoice, use_ai=False)
    assert result is not None
    assert result.invoice_number == "INV-2026-TEST"
    assert result.total_amount == 1000.0
    assert result.currency == "USD"


def test_zapier_service_configuration_and_masking():
    """Verifies Zapier webhook configuration, URL masking, and outbound event governance."""
    # Test update
    test_webhook = "https://hooks.zapier.com/hooks/catch/1234567/abcdefg/"
    config = ZapierService.update_config(test_webhook, enabled=True)
    assert config["configured"] is True
    assert config["enabled"] is True
    assert test_webhook != config["webhook_url_masked"]
    assert "..." in config["webhook_url_masked"]

    # Test dispatch skipped on unregistered event
    res = ZapierService.dispatch_event("unknown.event", {"foo": "bar"})
    assert res["status"] == "skipped"

    # Reset config
    ZapierService.update_config("", enabled=False)


def test_api_ai_health_endpoint():
    """Verifies GET /api/ai/health returns 200 OK with free AI provider metrics."""
    response = client.get("/api/ai/health")
    assert response.status_code == 200
    data = response.json()
    assert "provider" in data
    assert "deterministic_fallback_ready" in data
    assert data["free_tier_compliant"] is True


def test_api_integrations_status_endpoint():
    """Verifies GET /api/integrations/status returns 200 OK with all 5 sponsors and PayPal."""
    response = client.get("/api/integrations/status")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "operational"
    assert "sponsors" in data

    sponsors = data["sponsors"]
    assert "apimatic" in sponsors
    assert sponsors["apimatic"]["classification"] == "VERIFIED AND USED"
    assert "postman" in sponsors
    assert sponsors["postman"]["classification"] == "VERIFIED AND USED"
    assert "ag_grid" in sponsors
    assert sponsors["ag_grid"]["classification"] == "VERIFIED AND USED"
    assert "render" in sponsors
    assert sponsors["render"]["classification"] == "CONFIGURATION PREPARED"
    assert "zapier" in sponsors
    assert sponsors["zapier"]["classification"] == "VERIFIED AND USED"

    assert "ai_engine" in data
    assert "paypal_sandbox" in data
    assert data["paypal_sandbox"]["mode"] == "sandbox"

