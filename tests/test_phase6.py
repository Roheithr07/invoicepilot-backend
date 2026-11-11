import pytest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from datetime import datetime, timezone, timedelta
from decimal import Decimal

from app.config import settings
from app.models.models import (
    Invoice, Customer, InvoiceStatus, Payment, PaymentStatus,
    WebhookEvent, ReconciliationRecord, ReminderRecord, ActivityLog
)
from app.services.paypal_service import PayPalService
from app.services.payment_reconciliation import PaymentReconciliationEngine
from app.services.followup_engine import FollowUpEngine


# =========================================================================
# 1. PAYPAL WEBHOOK SIGNATURE & IDEMPOTENCY TESTS
# =========================================================================

def test_webhook_missing_signature_headers(client: TestClient, monkeypatch):
    """Webhook requests missing PayPal cryptographic signature headers must be rejected."""
    monkeypatch.setattr(settings, "PAYPAL_WEBHOOK_ID", "mock_wh_id_123")
    payload = {
        "id": "WH-TEST-001",
        "event_type": "INVOICING.INVOICE.PAID",
        "resource": {"id": "INV2-XYZ"}
    }
    # No headers sent
    resp = client.post("/api/webhooks/paypal", json=payload)
    assert resp.status_code == 400
    assert "headers" in resp.json()["detail"].lower()


def test_webhook_invalid_signature_rejected(client: TestClient):
    """Webhook requests where PayPal signature verification returns False must be rejected."""
    headers = {
        "PAYPAL-AUTH-ALGO": "SHA256withRSA",
        "PAYPAL-CERT-URL": "https://api.sandbox.paypal.com/cert",
        "PAYPAL-TRANSMISSION-ID": "trans-123",
        "PAYPAL-TRANSMISSION-SIG": "bad-sig",
        "PAYPAL-TRANSMISSION-TIME": "2026-11-10T10:00:00Z"
    }
    payload = {
        "id": "WH-TEST-INVALID-SIG",
        "event_type": "INVOICING.INVOICE.PAID",
        "resource": {"id": "INV2-XYZ"}
    }

    with patch.object(PayPalService, "verify_webhook_signature", return_value=False):
        resp = client.post("/api/webhooks/paypal", json=payload, headers=headers)
        assert resp.status_code == 400
        assert "signature verification failed" in resp.json()["detail"].lower()


def test_webhook_valid_signature_and_invoice_paid_processing(client: TestClient, db_session: Session):
    """Verified INVOICING.INVOICE.PAID event updates invoice to PAID and creates payment record."""
    # Create test invoice
    inv = Invoice(
        invoice_number=f"INV-WH-PAID-{datetime.now().timestamp()}",
        currency="USD",
        subtotal=500.0,
        tax_total=50.0,
        total_amount=550.0,
        status=InvoiceStatus.SENT,
        paypal_invoice_id=f"INV2-PAID-{datetime.now().timestamp()}",
        paypal_status="SENT"
    )
    db_session.add(inv)
    db_session.commit()
    db_session.refresh(inv)

    headers = {
        "PAYPAL-AUTH-ALGO": "SHA256withRSA",
        "PAYPAL-CERT-URL": "https://api.sandbox.paypal.com/cert",
        "PAYPAL-TRANSMISSION-ID": "trans-456",
        "PAYPAL-TRANSMISSION-SIG": "valid-sig",
        "PAYPAL-TRANSMISSION-TIME": "2026-11-10T10:00:00Z"
    }
    event_id = f"WH-PAID-{datetime.now().timestamp()}"
    payload = {
        "id": event_id,
        "event_type": "INVOICING.INVOICE.PAID",
        "resource_type": "invoices",
        "resource": {
            "id": inv.paypal_invoice_id,
            "number": inv.invoice_number,
            "status": "PAID",
            "payments": {
                "paid_amount": {
                    "currency_code": "USD",
                    "value": "550.00"
                }
            }
        }
    }

    with patch.object(PayPalService, "verify_webhook_signature", return_value=True):
        resp = client.post("/api/webhooks/paypal", json=payload, headers=headers)
        assert resp.status_code == 200
        assert resp.json()["status"] == "success"

    # Verify invoice transitioned to PAID
    db_session.refresh(inv)
    assert inv.status == InvoiceStatus.PAID
    assert inv.paypal_status == "PAID"

    # Verify WebhookEvent stored
    ev = db_session.query(WebhookEvent).filter(WebhookEvent.event_id == event_id).first()
    assert ev is not None
    assert ev.is_verified is True
    assert ev.processing_status == "PROCESSED"


def test_webhook_idempotency_duplicate_event_handling(client: TestClient, db_session: Session):
    """Submitting the exact same webhook event ID twice must not duplicate records."""
    headers = {
        "PAYPAL-AUTH-ALGO": "SHA256withRSA",
        "PAYPAL-CERT-URL": "https://api.sandbox.paypal.com/cert",
        "PAYPAL-TRANSMISSION-ID": "trans-789",
        "PAYPAL-TRANSMISSION-SIG": "valid-sig",
        "PAYPAL-TRANSMISSION-TIME": "2026-11-10T10:00:00Z"
    }
    event_id = f"WH-DUP-{datetime.now().timestamp()}"
    payload = {
        "id": event_id,
        "event_type": "INVOICING.INVOICE.SENT",
        "resource": {"id": "INV2-SOME-ID", "number": "INV-001"}
    }

    with patch.object(PayPalService, "verify_webhook_signature", return_value=True):
        # First delivery
        resp1 = client.post("/api/webhooks/paypal", json=payload, headers=headers)
        assert resp1.status_code == 200
        assert resp1.json()["status"] == "success"

        # Second delivery (Duplicate)
        resp2 = client.post("/api/webhooks/paypal", json=payload, headers=headers)
        assert resp2.status_code == 200
        assert resp2.json()["status"] == "already_processed"


def test_webhook_out_of_order_protection_does_not_downgrade_paid(client: TestClient, db_session: Session):
    """An older INVOICING.INVOICE.SENT event must NEVER downgrade an already PAID invoice."""
    inv = Invoice(
        invoice_number=f"INV-ORDER-{datetime.now().timestamp()}",
        currency="USD",
        subtotal=200.0,
        tax_total=0.0,
        total_amount=200.0,
        status=InvoiceStatus.PAID,
        paypal_invoice_id=f"INV2-ORDER-{datetime.now().timestamp()}",
        paypal_status="PAID"
    )
    db_session.add(inv)
    db_session.commit()
    db_session.refresh(inv)

    headers = {
        "PAYPAL-AUTH-ALGO": "SHA256withRSA",
        "PAYPAL-CERT-URL": "https://api.sandbox.paypal.com/cert",
        "PAYPAL-TRANSMISSION-ID": "trans-older",
        "PAYPAL-TRANSMISSION-SIG": "valid-sig",
        "PAYPAL-TRANSMISSION-TIME": "2026-11-10T10:00:00Z"
    }
    payload = {
        "id": f"WH-OLDER-SENT-{datetime.now().timestamp()}",
        "event_type": "INVOICING.INVOICE.SENT",
        "resource": {"id": inv.paypal_invoice_id, "number": inv.invoice_number}
    }

    with patch.object(PayPalService, "verify_webhook_signature", return_value=True):
        resp = client.post("/api/webhooks/paypal", json=payload, headers=headers)
        assert resp.status_code == 200

    db_session.refresh(inv)
    assert inv.status == InvoiceStatus.PAID
    assert inv.paypal_status == "PAID"


# =========================================================================
# 2. PAYMENT RECONCILIATION ENGINE TESTS
# =========================================================================

def test_reconciliation_exact_match(db_session: Session):
    """Payment with exact matching currency, amount, and reference reconciles as MATCHED."""
    inv = Invoice(
        invoice_number=f"INV-REC-EXACT-{datetime.now().timestamp()}",
        currency="USD",
        subtotal=400.0,
        tax_total=40.0,
        total_amount=440.0,
        status=InvoiceStatus.SENT,
        paypal_invoice_id=f"INV2-EXACT-{datetime.now().timestamp()}"
    )
    db_session.add(inv)
    db_session.commit()
    db_session.refresh(inv)

    payment = Payment(
        invoice_id=inv.id,
        paypal_payment_id=f"PAY-EXACT-{datetime.now().timestamp()}",
        amount=440.0,
        currency="USD",
        status=PaymentStatus.PENDING
    )
    db_session.add(payment)
    db_session.commit()
    db_session.refresh(payment)

    rec = PaymentReconciliationEngine.reconcile_payment(db_session, payment)
    assert rec.category == "MATCHED"
    assert rec.confidence_score == 1.0
    assert payment.is_reconciled is True
    assert payment.status == PaymentStatus.COMPLETED

    db_session.refresh(inv)
    assert inv.status == InvoiceStatus.PAID


def test_reconciliation_partial_payment(db_session: Session):
    """Payment for less than total balance reconciles as PARTIALLY_MATCHED."""
    inv = Invoice(
        invoice_number=f"INV-REC-PARTIAL-{datetime.now().timestamp()}",
        currency="USD",
        subtotal=1000.0,
        tax_total=0.0,
        total_amount=1000.0,
        status=InvoiceStatus.SENT,
        paypal_invoice_id=f"INV2-PARTIAL-{datetime.now().timestamp()}"
    )
    db_session.add(inv)
    db_session.commit()
    db_session.refresh(inv)

    payment = Payment(
        invoice_id=inv.id,
        paypal_payment_id=f"PAY-PARTIAL-{datetime.now().timestamp()}",
        amount=500.0,
        currency="USD",
        status=PaymentStatus.PENDING
    )
    db_session.add(payment)
    db_session.commit()
    db_session.refresh(payment)

    rec = PaymentReconciliationEngine.reconcile_payment(db_session, payment)
    assert rec.category == "PARTIALLY_MATCHED"
    assert payment.is_reconciled is True
    assert payment.status == PaymentStatus.PARTIALLY_PAID
    assert "Shortfall" in rec.discrepancies[0]


def test_reconciliation_currency_mismatch(db_session: Session):
    """Payment in EUR against a USD invoice flags CURRENCY_MISMATCH."""
    inv = Invoice(
        invoice_number=f"INV-REC-CURR-{datetime.now().timestamp()}",
        currency="USD",
        subtotal=300.0,
        tax_total=0.0,
        total_amount=300.0,
        status=InvoiceStatus.SENT
    )
    db_session.add(inv)
    db_session.commit()
    db_session.refresh(inv)

    payment = Payment(
        invoice_id=inv.id,
        paypal_payment_id=f"PAY-CURR-{datetime.now().timestamp()}",
        amount=300.0,
        currency="EUR",  # Mismatch
        status=PaymentStatus.PENDING
    )
    db_session.add(payment)
    db_session.commit()
    db_session.refresh(payment)

    rec = PaymentReconciliationEngine.reconcile_payment(db_session, payment)
    assert rec.category == "CURRENCY_MISMATCH"
    assert payment.is_reconciled is False
    assert payment.status == PaymentStatus.REQUIRES_REVIEW


def test_reconciliation_manual_review_and_resolution(client: TestClient, db_session: Session):
    """A flagged reconciliation record can be reviewed and resolved by a compliance officer."""
    inv = Invoice(
        invoice_number=f"INV-REC-REV-{datetime.now().timestamp()}",
        currency="USD",
        subtotal=250.0,
        tax_total=0.0,
        total_amount=250.0,
        status=InvoiceStatus.SENT
    )
    db_session.add(inv)
    db_session.commit()
    db_session.refresh(inv)

    payment = Payment(
        invoice_id=inv.id,
        paypal_payment_id=f"PAY-REV-{datetime.now().timestamp()}",
        amount=245.0,  # $5 short
        currency="USD",
        status=PaymentStatus.PENDING
    )
    db_session.add(payment)
    db_session.commit()
    db_session.refresh(payment)

    rec = PaymentReconciliationEngine.reconcile_payment(db_session, payment)

    # Review endpoint
    review_resp = client.post(
        f"/api/reconciliation/{rec.id}/review",
        json={
            "decision": "RESOLVED",
            "notes": "Approved minor $5 early-pay discount by merchant agreement.",
            "reviewer_name": "Chief Compliance Officer"
        }
    )
    assert review_resp.status_code == 200
    assert review_resp.json()["status"] == "RESOLVED"

    db_session.refresh(payment)
    assert payment.is_reconciled is True
    assert payment.status == PaymentStatus.COMPLETED


# =========================================================================
# 3. AI SMART FOLLOW-UP & REMINDER TESTS
# =========================================================================

def test_followup_overdue_invoice_generates_urgent_priority(db_session: Session):
    """Invoices overdue by more than 21 days receive URGENT priority and firm tone."""
    past_due = datetime.now(timezone.utc) - timedelta(days=25)
    cust = Customer(name="Acme Corp", email="billing@acme.com")
    db_session.add(cust)
    db_session.commit()

    inv = Invoice(
        invoice_number=f"INV-URGENT-{datetime.now().timestamp()}",
        customer_id=cust.id,
        currency="USD",
        subtotal=1500.0,
        tax_total=0.0,
        total_amount=1500.0,
        issue_date=past_due - timedelta(days=30),
        due_date=past_due,
        status=InvoiceStatus.SENT
    )
    db_session.add(inv)
    db_session.commit()
    db_session.refresh(inv)

    reminder = FollowUpEngine.generate_draft_for_invoice(db_session, inv)
    assert reminder is not None
    assert reminder.priority_level == "URGENT"
    assert reminder.tone == "URGENT"
    assert reminder.is_approved is False
    assert reminder.status == "PENDING_APPROVAL"
    assert "25 days overdue" in reminder.priority_reason


def test_followup_paid_invoice_is_strictly_excluded(db_session: Session):
    """Paid invoices must NEVER receive follow-up reminders."""
    cust = Customer(name="Prompt Client", email="prompt@client.com")
    db_session.add(cust)
    db_session.commit()

    inv = Invoice(
        invoice_number=f"INV-EXCLUDED-PAID-{datetime.now().timestamp()}",
        customer_id=cust.id,
        currency="USD",
        subtotal=100.0,
        tax_total=0.0,
        total_amount=100.0,
        status=InvoiceStatus.PAID,
        paypal_status="PAID"
    )
    db_session.add(inv)
    db_session.commit()
    db_session.refresh(inv)

    reminder = FollowUpEngine.generate_draft_for_invoice(db_session, inv)
    assert reminder is None


def test_followup_two_stage_approval_and_dispatch(client: TestClient, db_session: Session):
    """Reminders require explicit user approval before dispatch; unapproved dispatch is rejected."""
    due = datetime.now(timezone.utc) - timedelta(days=5)
    cust = Customer(name="Beta LLC", email="finance@betallc.com")
    db_session.add(cust)
    db_session.commit()

    inv = Invoice(
        invoice_number=f"INV-APPROVAL-{datetime.now().timestamp()}",
        customer_id=cust.id,
        currency="USD",
        subtotal=800.0,
        tax_total=0.0,
        total_amount=800.0,
        due_date=due,
        status=InvoiceStatus.SENT
    )
    db_session.add(inv)
    db_session.commit()
    db_session.refresh(inv)

    reminder = FollowUpEngine.generate_draft_for_invoice(db_session, inv)
    assert reminder.is_approved is False

    # Attempt dispatch without approval -> Must fail with HTTP 400
    fail_dispatch = client.post(f"/api/follow-ups/{reminder.id}/dispatch", json={"channel": "auto"})
    assert fail_dispatch.status_code == 400
    assert "explicitly approved" in fail_dispatch.json()["detail"].lower()

    # Step 1: Explicit Approval
    app_resp = client.post(
        f"/api/follow-ups/{reminder.id}/approve",
        json={"approver_name": "Operations Lead"}
    )
    assert app_resp.status_code == 200
    assert app_resp.json()["is_approved"] is True

    # Step 2: Dispatch
    dispatch_resp = client.post(f"/api/follow-ups/{reminder.id}/dispatch", json={"channel": "auto"})
    assert dispatch_resp.status_code == 200
    assert dispatch_resp.json()["status"] == "DISPATCHED"
    assert "dispatch_method" in dispatch_resp.json()
