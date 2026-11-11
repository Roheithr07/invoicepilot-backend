"""
Automated Test Suite for Phase 5:
PayPal Developer Sandbox Invoicing Integration, OAuth 2.0 Caching,
Pydantic v2 Schema Mapping, Two-Step Dispatch Governance, Audit Guardrails, and Secret Redaction.
"""

import pytest
import time
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone, timedelta
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
import httpx

from app.config import settings
from app.models.models import (
    Invoice, InvoiceItem, Customer, AuditResult,
    InvoiceStatus, AuditSeverity
)
from app.services.paypal_service import (
    PayPalService, PayPalError, PayPalConfigurationError,
    PayPalAuthenticationError, PayPalValidationError, PayPalDuplicateError,
    sanitize_sensitive_data
)
from app.schemas.paypal import PayPalSendRequest


# -------------------------------------------------------------
# 1. Configuration & Redaction Tests
# -------------------------------------------------------------

def test_missing_credentials_configuration(monkeypatch):
    """When credentials are not set, service raises PayPalConfigurationError and reports status."""
    monkeypatch.setattr(settings, "PAYPAL_CLIENT_ID", "")
    monkeypatch.setattr(settings, "PAYPAL_CLIENT_SECRET", "")
    PayPalService.clear_token_cache()

    assert PayPalService.is_configured() is False
    assert PayPalService.get_masked_client_id() is None

    health = PayPalService.verify_connectivity()
    assert health["status"] == "misconfigured"
    assert health["authenticated"] is False

    with pytest.raises(PayPalConfigurationError) as exc_info:
        PayPalService.get_access_token()
    assert "not configured" in str(exc_info.value)


def test_secret_and_token_redaction(monkeypatch):
    """Access tokens and client secrets must be sanitized from error strings and logs."""
    test_secret = "super_secret_paypal_sandbox_key_12345"
    monkeypatch.setattr(settings, "PAYPAL_CLIENT_SECRET", test_secret)

    sample_log = (
        f"Authorization: Basic dGVzdDp7dGVzdF9zZWNyZXR9 | "
        f"Bearer A21AAJ9_test_access_token_xyz987 | "
        f"Client secret is {test_secret} | "
        f'{{"access_token": "A21AAJ9_raw_token_value"}}'
    )

    sanitized = sanitize_sensitive_data(sample_log)
    assert test_secret not in sanitized
    assert "A21AAJ9_test_access_token_xyz987" not in sanitized
    assert "A21AAJ9_raw_token_value" not in sanitized
    assert "[REDACTED_SECRET]" in sanitized
    assert "[REDACTED_TOKEN]" in sanitized


# -------------------------------------------------------------
# 2. OAuth 2.0 Token Generation & Caching Tests
# -------------------------------------------------------------

def test_oauth_token_caching_and_refresh(monkeypatch):
    """Access token is cached in-memory and refreshed when approaching expiration buffer."""
    monkeypatch.setattr(settings, "PAYPAL_CLIENT_ID", "mock_client_id_1234567890")
    monkeypatch.setattr(settings, "PAYPAL_CLIENT_SECRET", "mock_client_secret_xyz987654321")
    PayPalService.clear_token_cache()

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "access_token": "A21AA_mock_token_initial",
        "token_type": "Bearer",
        "expires_in": 3600
    }

    with patch("httpx.Client.post", return_value=mock_resp) as mock_post:
        # First call: hits OAuth endpoint
        token1 = PayPalService.get_access_token()
        assert token1 == "A21AA_mock_token_initial"
        assert mock_post.call_count == 1

        # Second call: returns cached token without HTTP request
        token2 = PayPalService.get_access_token()
        assert token2 == "A21AA_mock_token_initial"
        assert mock_post.call_count == 1  # Unchanged!

        # Force refresh simulation by advancing expiration time past buffer
        PayPalService._token_expires_at = time.time() + 100  # < 300s buffer!
        mock_resp.json.return_value = {
            "access_token": "A21AA_mock_token_refreshed",
            "token_type": "Bearer",
            "expires_in": 3600
        }
        token3 = PayPalService.get_access_token()
        assert token3 == "A21AA_mock_token_refreshed"
        assert mock_post.call_count == 2


def test_oauth_failure_handling(monkeypatch):
    """OAuth 401 returns structured PayPalAuthenticationError."""
    monkeypatch.setattr(settings, "PAYPAL_CLIENT_ID", "invalid_id")
    monkeypatch.setattr(settings, "PAYPAL_CLIENT_SECRET", "invalid_secret")
    PayPalService.clear_token_cache()

    mock_resp = MagicMock()
    mock_resp.status_code = 401
    mock_resp.json.return_value = {
        "error": "invalid_client",
        "error_description": "Client Authentication failed"
    }

    with patch("httpx.Client.post", return_value=mock_resp):
        with pytest.raises(PayPalAuthenticationError) as exc_info:
            PayPalService.get_access_token()
        assert "HTTP 401" in str(exc_info.value)
        assert "Client Authentication failed" in str(exc_info.value)


# -------------------------------------------------------------
# 3. Payload Mapping & Draft Creation Tests
# -------------------------------------------------------------

def test_map_invoice_to_paypal_v2_schema(db_session: Session):
    """Local Invoice and InvoiceItems are strictly mapped to PayPal v2 Invoicing schema."""
    cust = Customer(name="Acme Enterprise", email="billing@acme.com")
    db_session.add(cust)
    db_session.flush()

    inv = Invoice(
        invoice_number="INV-PP-MAP-001",
        customer_id=cust.id,
        currency="USD",
        subtotal=400.0,
        tax_total=40.0,
        total_amount=440.0,
        issue_date=datetime(2026, 11, 10, tzinfo=timezone.utc),
        due_date=datetime(2026, 11, 24, tzinfo=timezone.utc),
        notes="Net 14 days payment agreement."
    )
    inv.customer = cust
    db_session.add(inv)
    db_session.flush()

    item = InvoiceItem(
        invoice_id=inv.id,
        description="DevOps Infrastructure Setup",
        quantity=2.0,
        unit_price=200.0,
        tax_rate=10.0,
        tax_amount=40.0,
        total=440.0
    )
    inv.items = [item]
    db_session.add(item)
    db_session.commit()

    payload = PayPalService.map_invoice_to_paypal_v2(inv, merchant_name="InvoicePilot Test", merchant_email="merchant@test.io")

    # Verify Detail
    assert payload["detail"]["invoice_number"] == "INV-PP-MAP-001"
    assert payload["detail"]["currency_code"] == "USD"
    assert payload["detail"]["invoice_date"] == "2026-11-10"
    assert payload["detail"]["payment_term"]["due_date"] == "2026-11-24"

    # Verify Invoicer
    assert payload["invoicer"]["email_address"] == "merchant@test.io"
    assert payload["invoicer"]["name"]["given_name"] == "InvoicePilot"

    # Verify Primary Recipient
    assert payload["primary_recipients"][0]["billing_info"]["email_address"] == "billing@acme.com"
    assert payload["primary_recipients"][0]["billing_info"]["name"]["given_name"] == "Acme Enterprise"

    # Verify Item
    assert len(payload["items"]) == 1
    assert payload["items"][0]["name"] == "DevOps Infrastructure Setup"
    assert payload["items"][0]["quantity"] == "2"
    assert payload["items"][0]["unit_amount"]["value"] == "200.00"
    assert payload["items"][0]["tax"]["percent"] == "10"


def test_create_draft_invoice_success(db_session: Session, monkeypatch):
    """Creates PayPal draft invoice via POST /v2/invoicing/invoices and returns ID."""
    monkeypatch.setattr(settings, "PAYPAL_CLIENT_ID", "mock_id_val")
    monkeypatch.setattr(settings, "PAYPAL_CLIENT_SECRET", "mock_secret_val")
    PayPalService.clear_token_cache()

    cust = Customer(name="Nova Global", email="finance@novaglobal.com")
    db_session.add(cust)
    db_session.flush()

    inv = Invoice(
        invoice_number="INV-PP-DRAFT-01",
        customer_id=cust.id,
        currency="USD",
        subtotal=100.0,
        total_amount=100.0,
        issue_date=datetime.now(timezone.utc)
    )
    inv.customer = cust
    item = InvoiceItem(invoice_id=1, description="Support Subscription", quantity=1, unit_price=100.0, total=100.0)
    inv.items = [item]

    mock_oauth_resp = MagicMock(status_code=200)
    mock_oauth_resp.json.return_value = {"access_token": "mock_token_abc", "expires_in": 3600}

    mock_create_resp = MagicMock(status_code=201)
    mock_create_resp.json.return_value = {
        "id": "INV2-MOCK-DRAFT-9988",
        "status": "DRAFT",
        "links": [
            {"rel": "self", "href": "https://api-m.sandbox.paypal.com/v2/invoicing/invoices/INV2-MOCK-DRAFT-9988"},
            {"rel": "send", "href": "https://api-m.sandbox.paypal.com/v2/invoicing/invoices/INV2-MOCK-DRAFT-9988/send"}
        ]
    }

    def mock_post(url, *args, **kwargs):
        if "oauth2/token" in str(url):
            return mock_oauth_resp
        if "invoicing/invoices" in str(url):
            return mock_create_resp
        return MagicMock(status_code=404)

    with patch("httpx.Client.post", side_effect=mock_post):
        res = PayPalService.create_draft_invoice(inv)
        assert res["id"] == "INV2-MOCK-DRAFT-9988"
        assert res["status"] == "DRAFT"


def test_duplicate_draft_prevention(db_session: Session):
    """Attempting to create draft when paypal_invoice_id already exists raises PayPalDuplicateError."""
    inv = Invoice(
        invoice_number="INV-PP-DUP-01",
        paypal_invoice_id="INV2-EXISTING-DRAFT",
        currency="USD",
        subtotal=100.0,
        total_amount=100.0
    )
    with pytest.raises(PayPalDuplicateError) as exc_info:
        PayPalService.create_draft_invoice(inv)
    assert "already has an associated PayPal draft" in str(exc_info.value)


# -------------------------------------------------------------
# 4. Two-Step Dispatch & Approval Guardrail Tests
# -------------------------------------------------------------

def test_send_invoice_requires_explicit_confirmation(client: TestClient, db_session: Session):
    """Sending without confirmed=True returns HTTP 400."""
    inv = Invoice(
        invoice_number="INV-SEND-NOCONF",
        paypal_invoice_id="INV2-DRAFT-READY",
        status=InvoiceStatus.DRAFT,
        currency="USD",
        subtotal=200.0,
        total_amount=200.0
    )
    cust = Customer(name="Test Client", email="client@test.com")
    inv.customer = cust
    item = InvoiceItem(description="Consulting", quantity=1, unit_price=200.0, total=200.0)
    inv.items = [item]
    db_session.add_all([cust, inv, item])
    db_session.commit()

    resp = client.post(f"/api/invoices/{inv.id}/paypal/send", json={"confirmed": False})
    assert resp.status_code == 400
    assert "Explicit user confirmation is required" in resp.json()["detail"]


def test_send_invoice_success_and_status_update(client: TestClient, db_session: Session, monkeypatch):
    """With confirmed=True, dispatches invoice and sets local state to SENT."""
    monkeypatch.setattr(settings, "PAYPAL_CLIENT_ID", "mock_id")
    monkeypatch.setattr(settings, "PAYPAL_CLIENT_SECRET", "mock_sec")
    PayPalService.clear_token_cache()

    cust = Customer(name="Solaris Ltd", email="billing@solaris.io")
    db_session.add(cust)
    db_session.flush()

    inv = Invoice(
        invoice_number="INV-SEND-SUCCESS",
        customer_id=cust.id,
        paypal_invoice_id="INV2-SOLARIS-001",
        status=InvoiceStatus.DRAFT,
        currency="USD",
        subtotal=300.0,
        tax_total=0.0,
        total_amount=300.0,
        issue_date=datetime.now(timezone.utc),
        due_date=datetime.now(timezone.utc) + timedelta(days=10)
    )
    inv.customer = cust
    item = InvoiceItem(description="Solar Grid Software", quantity=1, unit_price=300.0, tax_rate=0.0, total=300.0)
    inv.items = [item]
    db_session.add_all([inv, item])
    db_session.commit()

    verified_data = {
        "id": "INV2-SOLARIS-001",
        "status": "SENT",
        "_extracted_payer_url": "https://www.sandbox.paypal.com/invoice/p/#INV2-SOLARIS-001",
        "links": [{"rel": "payer-view", "href": "https://www.sandbox.paypal.com/invoice/p/#INV2-SOLARIS-001"}]
    }

    with patch.object(PayPalService, "send_invoice", return_value=verified_data):
        resp = client.post(f"/api/invoices/{inv.id}/paypal/send", json={"confirmed": True})
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["paypal_status"] == "SENT"
        assert "sandbox.paypal.com" in data["paypal_invoice_url"]

        # Verify local invoice state was updated
        db_session.refresh(inv)
        assert inv.status == InvoiceStatus.SENT
        assert inv.paypal_status == "SENT"
        assert inv.paypal_sent_at is not None


# -------------------------------------------------------------
# 5. Audit Guardrails Integration Tests
# -------------------------------------------------------------

def test_audit_guardrails_block_flawed_invoice_draft(client: TestClient, db_session: Session):
    """Invoice with critical audit errors (e.g. total math mismatch) is blocked from draft creation."""
    cust = Customer(name="Broken Corp", email="broken@corp.com")
    db_session.add(cust)
    db_session.flush()

    # Grand total mismatch: subtotal 100 + tax 0 != total 250!
    inv = Invoice(
        invoice_number="INV-FLAWED-AUDIT",
        customer_id=cust.id,
        currency="USD",
        subtotal=100.0,
        tax_total=0.0,
        total_amount=250.0,  # Critical mismatch!
        issue_date=datetime.now(timezone.utc)
    )
    inv.customer = cust
    item = InvoiceItem(description="Web Hosting", quantity=1, unit_price=100.0, tax_rate=0.0, total=100.0)
    inv.items = [item]
    db_session.add_all([inv, item])
    db_session.commit()

    resp = client.post(f"/api/invoices/{inv.id}/paypal/draft")
    assert resp.status_code == 422
    assert "Blocked by Audit Engine" in resp.json()["detail"]
    assert "Grand Total Mismatch" in resp.json()["detail"]


# -------------------------------------------------------------
# 6. Status Reconciliation Tests
# -------------------------------------------------------------

def test_get_paypal_status_reconciles_paid_state(client: TestClient, db_session: Session, monkeypatch):
    """GET /api/invoices/{id}/paypal/status updates local status to PAID when PayPal returns PAID."""
    monkeypatch.setattr(settings, "PAYPAL_CLIENT_ID", "mock_id")
    monkeypatch.setattr(settings, "PAYPAL_CLIENT_SECRET", "mock_sec")
    PayPalService.clear_token_cache()

    inv = Invoice(
        invoice_number="INV-RECON-PAID",
        paypal_invoice_id="INV2-PAID-001",
        status=InvoiceStatus.SENT,
        paypal_status="SENT",
        currency="USD",
        subtotal=500.0,
        total_amount=500.0
    )
    db_session.add(inv)
    db_session.commit()

    remote_data = {
        "id": "INV2-PAID-001",
        "status": "PAID",
        "amount": {"currency_code": "USD", "value": "500.00"},
        "_extracted_payer_url": "https://www.sandbox.paypal.com/invoice/p/#INV2-PAID-001",
        "links": []
    }

    with patch.object(PayPalService, "get_invoice_details", return_value=remote_data):
        resp = client.get(f"/api/invoices/{inv.id}/paypal/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["paypal_status"] == "PAID"
        assert data["local_status"] == "PAID"
        assert data["is_paid"] is True

        db_session.refresh(inv)
        assert inv.status == InvoiceStatus.PAID
        assert inv.paypal_status == "PAID"
        assert inv.paypal_last_checked_at is not None


# -------------------------------------------------------------
# 7. APIMatic Context Plugin Validation Tests
# -------------------------------------------------------------

def test_apimatic_guidance_401_clears_token_cache(monkeypatch):
    """
    APIMatic python-authentication rule:
    On an HTTP 401 response from PayPal REST API, cached token must be invalidated
    so the next call does not persist an expired or revoked credentials state.
    """
    monkeypatch.setattr(settings, "PAYPAL_CLIENT_ID", "mock_id")
    monkeypatch.setattr(settings, "PAYPAL_CLIENT_SECRET", "mock_sec")
    
    # Pre-populate token cache
    PayPalService._cached_token = "stale_or_revoked_token_123"
    PayPalService._token_expires_at = time.time() + 3600

    mock_401_resp = MagicMock(status_code=401)
    mock_401_resp.headers = {"paypal-debug-id": "trace-debug-401-xyz"}
    mock_401_resp.json.return_value = {
        "error": "invalid_token",
        "error_description": "Token signature revoked or expired remotely"
    }

    with patch("httpx.Client.get", return_value=mock_401_resp):
        with pytest.raises(PayPalError) as exc_info:
            PayPalService.get_invoice_details("INV2-TEST-401")
        assert "HTTP 401" in str(exc_info.value)
        assert "PayPal Debug ID: trace-debug-401-xyz" in str(exc_info.value)

    # Token cache must now be cleared
    assert PayPalService._cached_token is None
    assert PayPalService._token_expires_at == 0.0


def test_apimatic_guidance_debug_id_and_validation_details_extraction(monkeypatch):
    """
    APIMatic python-error-handling rule:
    Structured error responses from PayPal Invoicing v2 must extract field-level details
    and paypal-debug-id into PayPalError.details while ensuring secrets remain redacted.
    """
    monkeypatch.setattr(settings, "PAYPAL_CLIENT_ID", "mock_id")
    monkeypatch.setattr(settings, "PAYPAL_CLIENT_SECRET", "mock_sec")
    PayPalService._cached_token = "valid_cached_token"
    PayPalService._token_expires_at = time.time() + 3600

    mock_400_resp = MagicMock(status_code=400)
    mock_400_resp.headers = {"paypal-debug-id": "dbg_987654321"}
    mock_400_resp.json.return_value = {
        "name": "INVALID_REQUEST",
        "message": "Request is not well-formed, syntactically incorrect, or violates schema.",
        "debug_id": "dbg_987654321",
        "details": [
            {
                "field": "/items/0/unit_amount/value",
                "value": "-10.00",
                "issue": "AMOUNT_CANNOT_BE_NEGATIVE",
                "description": "Item amount cannot be negative."
            }
        ]
    }

    test_inv = Invoice(
        id=99,
        invoice_number="INV-ERR-APIMATIC",
        customer_email="test@client.com",
        customer_name="Test Client",
        currency="USD",
        items=[InvoiceItem(description="Item", quantity=1, unit_price=-10.0, tax_rate=0.0)]
    )

    with patch("httpx.Client.post", return_value=mock_400_resp):
        with pytest.raises(PayPalValidationError) as exc_info:
            PayPalService.create_draft_invoice(test_inv)
        
        err = exc_info.value
        assert err.status_code == 400
        assert "PayPal Debug ID: dbg_987654321" in str(err)
        assert "/items/0/unit_amount/value" in str(err)
        assert err.details.get("debug_id") == "dbg_987654321"
        assert len(err.details.get("details", [])) == 1


def test_paypal_health_three_state_distinction(monkeypatch):
    """
    Verifies that verify_connectivity() explicitly distinguishes:
    1. Missing configuration
    2. Authentication failed
    3. Sandbox authenticated successfully
    """
    PayPalService.clear_token_cache()

    # State 1: Missing Configuration
    monkeypatch.setattr(settings, "PAYPAL_CLIENT_ID", "")
    monkeypatch.setattr(settings, "PAYPAL_CLIENT_SECRET", "")
    health_missing = PayPalService.verify_connectivity()
    assert health_missing["status"] == "misconfigured"
    assert health_missing["connection_state"] == "missing_configuration"
    assert health_missing["authenticated"] is False

    # State 2: Authentication Failed
    monkeypatch.setattr(settings, "PAYPAL_CLIENT_ID", "mock_bad_id")
    monkeypatch.setattr(settings, "PAYPAL_CLIENT_SECRET", "mock_bad_secret")
    mock_401 = MagicMock(status_code=401)
    mock_401.json.return_value = {"error": "invalid_client", "error_description": "Client Authentication failed"}
    with patch("httpx.Client.post", return_value=mock_401):
        health_failed = PayPalService.verify_connectivity()
        assert health_failed["status"] == "authentication_failed"
        assert health_failed["connection_state"] == "authentication_failed"
        assert health_failed["authenticated"] is False
        assert "Authentication failed" in health_failed["message"]

    # State 3: Sandbox Authenticated Successfully
    PayPalService.clear_token_cache()
    mock_200 = MagicMock(status_code=200)
    mock_200.json.return_value = {"access_token": "mock_token_ok", "expires_in": 32400}
    with patch("httpx.Client.post", return_value=mock_200):
        health_success = PayPalService.verify_connectivity()
        assert health_success["status"] == "connected"
        assert health_success["connection_state"] == "sandbox_authenticated_successfully"
        assert health_success["authenticated"] is True
        assert health_success["token_cached"] is True
        assert "Sandbox authenticated successfully" in health_success["message"]


