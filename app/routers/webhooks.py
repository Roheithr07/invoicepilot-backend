"""
PayPal Webhook Receiver and Event Processor for InvoicePilot AI.
Verifies cryptographic signatures against PayPal Sandbox, guarantees event idempotency,
safely reconciles financial states, and supports reliable retries.
"""

import json
import logging
from datetime import datetime, timezone
from typing import Dict, Any, Optional, List

from fastapi import APIRouter, Request, Depends, HTTPException, status, Query
from sqlalchemy.orm import Session
from sqlalchemy import desc

from app.database import get_db
from app.models.models import (
    WebhookEvent, Invoice, Payment, InvoiceStatus, PaymentStatus, ActivityLog
)
from app.services.paypal_service import (
    PayPalService, PayPalError, PayPalConfigurationError,
    PayPalValidationError, sanitize_sensitive_data
)
from app.services.payment_reconciliation import PaymentReconciliationEngine

logger = logging.getLogger("InvoicePilot.Webhooks")

router = APIRouter(prefix="/webhooks", tags=["PayPal Webhook Receiver"])


def process_verified_event(db: Session, event_record: WebhookEvent) -> Dict[str, Any]:
    """
    Core event processor for verified PayPal webhook events.
    Protects against out-of-order events (e.g. never downgrades a PAID invoice).
    """
    event_type = event_record.event_type
    payload = event_record.payload
    resource = payload.get("resource", {})

    logger.info("Processing webhook event %s of type %s", event_record.event_id, event_type)

    if event_type == "INVOICING.INVOICE.PAID":
        # Resource represents the Invoice or Invoicing Payment details
        paypal_inv_id = resource.get("id") or resource.get("invoice", {}).get("id")
        inv_number = resource.get("number") or resource.get("invoice", {}).get("number")
        
        # Locate local invoice
        invoice = None
        if paypal_inv_id:
            invoice = db.query(Invoice).filter(Invoice.paypal_invoice_id == paypal_inv_id).first()
        if not invoice and inv_number:
            invoice = db.query(Invoice).filter(Invoice.invoice_number == inv_number).first()

        # Extract payment amount & currency
        payments_list = resource.get("payments", {}).get("paid_amount") or resource.get("amount", {})
        paid_amount = float(payments_list.get("value", 0.0)) if isinstance(payments_list, dict) else 0.0
        currency = payments_list.get("currency_code", "USD") if isinstance(payments_list, dict) else "USD"

        if not paid_amount and invoice:
            paid_amount = float(invoice.total_amount)
            currency = invoice.currency

        # Find or create Payment record
        payment_ref = resource.get("payment_id") or resource.get("transaction_id") or f"PAYPAL-WH-{event_record.event_id[:16]}"
        payment = db.query(Payment).filter(Payment.paypal_payment_id == payment_ref).first()
        if not payment and invoice:
            payment = Payment(
                invoice_id=invoice.id,
                paypal_payment_id=payment_ref,
                amount=paid_amount,
                currency=currency,
                status=PaymentStatus.COMPLETED,
                payment_method="PAYPAL",
                payment_date=datetime.now(timezone.utc),
                notes=f"Recorded via verified PayPal webhook event {event_record.event_id}."
            )
            db.add(payment)
            db.commit()
            db.refresh(payment)

        if payment and invoice:
            PaymentReconciliationEngine.reconcile_payment(db, payment)

        if invoice:
            invoice.status = InvoiceStatus.PAID
            invoice.paypal_status = "PAID"
            invoice.paypal_last_checked_at = datetime.now(timezone.utc)
            db.commit()

        return {"handled": True, "action": "INVOICE_PAID", "invoice_id": invoice.id if invoice else None}

    elif event_type == "INVOICING.INVOICE.SENT":
        paypal_inv_id = resource.get("id")
        inv_number = resource.get("number")

        invoice = None
        if paypal_inv_id:
            invoice = db.query(Invoice).filter(Invoice.paypal_invoice_id == paypal_inv_id).first()
        if not invoice and inv_number:
            invoice = db.query(Invoice).filter(Invoice.invoice_number == inv_number).first()

        if invoice:
            # Out-of-order defense: Never downgrade a PAID invoice!
            if invoice.status != InvoiceStatus.PAID and invoice.paypal_status != "PAID":
                invoice.status = InvoiceStatus.SENT
                invoice.paypal_status = "SENT"
                invoice.paypal_sent_at = datetime.now(timezone.utc)
                invoice.paypal_last_checked_at = datetime.now(timezone.utc)
                db.commit()

        return {"handled": True, "action": "INVOICE_SENT", "invoice_id": invoice.id if invoice else None}

    elif event_type == "INVOICING.INVOICE.CANCELLED":
        paypal_inv_id = resource.get("id")
        invoice = db.query(Invoice).filter(Invoice.paypal_invoice_id == paypal_inv_id).first() if paypal_inv_id else None
        
        if invoice:
            if invoice.status != InvoiceStatus.PAID and invoice.paypal_status != "PAID":
                invoice.status = InvoiceStatus.CANCELLED
                invoice.paypal_status = "CANCELLED"
                invoice.paypal_last_checked_at = datetime.now(timezone.utc)
                db.commit()

        return {"handled": True, "action": "INVOICE_CANCELLED", "invoice_id": invoice.id if invoice else None}

    elif event_type == "INVOICING.INVOICE.REFUNDED":
        paypal_inv_id = resource.get("id")
        invoice = db.query(Invoice).filter(Invoice.paypal_invoice_id == paypal_inv_id).first() if paypal_inv_id else None
        
        if invoice:
            invoice.paypal_status = "REFUNDED"
            invoice.paypal_last_checked_at = datetime.now(timezone.utc)
            for p in invoice.payments:
                p.status = PaymentStatus.REFUNDED
            db.commit()

        return {"handled": True, "action": "INVOICE_REFUNDED", "invoice_id": invoice.id if invoice else None}

    elif event_type == "PAYMENT.CAPTURE.COMPLETED":
        capture_id = resource.get("id")
        amount_info = resource.get("amount", {})
        amount = float(amount_info.get("value", 0.0))
        currency = amount_info.get("currency_code", "USD")
        custom_id = resource.get("custom_id")
        invoice_id_ref = resource.get("invoice_id")

        # Try to find corresponding invoice
        invoice = None
        if invoice_id_ref:
            invoice = db.query(Invoice).filter(
                (Invoice.paypal_invoice_id == invoice_id_ref) | (Invoice.invoice_number == invoice_id_ref)
            ).first()
        if not invoice and custom_id:
            invoice = db.query(Invoice).filter(
                (Invoice.paypal_invoice_id == custom_id) | (Invoice.invoice_number == custom_id)
            ).first()

        # Check existing payment
        payment = db.query(Payment).filter(Payment.paypal_payment_id == capture_id).first()
        if not payment and invoice:
            payment = Payment(
                invoice_id=invoice.id,
                paypal_payment_id=capture_id,
                paypal_capture_id=capture_id,
                amount=amount,
                currency=currency,
                status=PaymentStatus.COMPLETED,
                payment_method="PAYPAL",
                payment_date=datetime.now(timezone.utc),
                notes=f"Created via PAYMENT.CAPTURE.COMPLETED webhook event {event_record.event_id}."
            )
            db.add(payment)
            db.commit()
            db.refresh(payment)

        if payment:
            PaymentReconciliationEngine.reconcile_payment(db, payment)

        return {"handled": True, "action": "CAPTURE_COMPLETED", "capture_id": capture_id}

    elif event_type in ("PAYMENT.CAPTURE.REFUNDED", "PAYMENT.CAPTURE.DENIED"):
        capture_id = resource.get("id")
        payment = db.query(Payment).filter(Payment.paypal_payment_id == capture_id).first()
        if payment:
            payment.status = PaymentStatus.REFUNDED if "REFUNDED" in event_type else PaymentStatus.FAILED
            db.commit()
        return {"handled": True, "action": event_type, "capture_id": capture_id}

    else:
        logger.info("Webhook event type %s noted without state modification.", event_type)
        return {"handled": True, "action": "IGNORED_UNSUPPORTED_TYPE", "event_type": event_type}


@router.post("/paypal", status_code=status.HTTP_200_OK)
@router.post("/paypal/webhooks", status_code=status.HTTP_200_OK)
async def receive_paypal_webhook(
    request: Request,
    db: Session = Depends(get_db)
):
    """
    Main webhook receiver for PayPal Developer Sandbox.
    1. Validates cryptographic webhook signatures with PayPal API.
    2. Enforces event idempotency using PayPal event_id.
    3. Persists verified event to WebhookEvent table.
    4. Dispatches safe state updates and triggers payment reconciliation.
    """
    raw_body = await request.body()
    headers_dict = dict(request.headers)

    # 1. Verify Cryptographic Webhook Signature
    try:
        is_valid = PayPalService.verify_webhook_signature(
            headers=headers_dict,
            raw_body_bytes=raw_body
        )
        if not is_valid:
            logger.warning("PayPal webhook signature verification failed.")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="PayPal webhook signature verification failed."
            )
    except (PayPalConfigurationError, PayPalValidationError) as e:
        logger.error("Webhook signature validation error: %s", str(e))
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except PayPalError as e:
        logger.error("PayPal webhook verification service error: %s", str(e))
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=sanitize_sensitive_data(str(e)))

    # 2. Parse JSON payload
    try:
        event_payload = json.loads(raw_body.decode("utf-8"))
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Invalid JSON payload: {str(e)}")

    event_id = event_payload.get("id")
    event_type = event_payload.get("event_type")
    resource_type = event_payload.get("resource_type")

    if not event_id or not event_type:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing required event 'id' or 'event_type' in PayPal payload."
        )

    # 3. Idempotency Check: Avoid duplicate event processing
    existing_event = db.query(WebhookEvent).filter(WebhookEvent.event_id == event_id).first()
    if existing_event:
        if existing_event.processing_status == "PROCESSED":
            logger.info("Duplicate webhook event %s already processed. Acknowledging with HTTP 200.", event_id)
            return {
                "status": "already_processed",
                "event_id": event_id,
                "message": "Event was previously received and processed successfully."
            }
        else:
            event_record = existing_event
    else:
        # 4. Persist verified event
        event_record = WebhookEvent(
            event_id=event_id,
            event_type=event_type,
            resource_type=resource_type,
            is_verified=True,
            payload=event_payload,
            processing_status="PENDING",
            received_at=datetime.now(timezone.utc)
        )
        db.add(event_record)
        db.commit()
        db.refresh(event_record)

    # 5. Process Event State
    try:
        result = process_verified_event(db, event_record)
        event_record.processing_status = "PROCESSED"
        event_record.processed_at = datetime.now(timezone.utc)
        event_record.error_detail = None
        db.commit()
        return {
            "status": "success",
            "event_id": event_id,
            "event_type": event_type,
            "details": result
        }
    except Exception as e:
        logger.exception("Failed to process webhook event %s: %s", event_id, e)
        event_record.processing_status = "FAILED"
        event_record.error_detail = sanitize_sensitive_data(str(e))
        event_record.retry_count = (event_record.retry_count or 0) + 1
        db.commit()
        # Still return 200/202 to avoid PayPal retry storms if internal error, but record status as FAILED for local retry
        return {
            "status": "stored_for_retry",
            "event_id": event_id,
            "error": sanitize_sensitive_data(str(e))
        }


@router.post("/paypal/simulate", status_code=status.HTTP_200_OK)
async def simulate_paypal_webhook(
    request: Request,
    db: Session = Depends(get_db)
):
    """
    Development and demonstration endpoint for simulating verified PayPal webhook events.
    Enforces idempotency and safe state updates without requiring active PayPal webhook signature headers.
    """
    try:
        event_payload = await request.json()
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Invalid JSON payload: {str(e)}")

    event_id = event_payload.get("id")
    event_type = event_payload.get("event_type")
    resource_type = event_payload.get("resource_type", "invoices")

    if not event_id or not event_type:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing required event 'id' or 'event_type' in PayPal payload."
        )

    # Idempotency Check
    existing_event = db.query(WebhookEvent).filter(WebhookEvent.event_id == event_id).first()
    if existing_event:
        if existing_event.processing_status == "PROCESSED":
            return {
                "status": "PROCESSED",
                "already_processed": True,
                "event_id": event_id,
                "message": "Event was previously received and processed successfully."
            }
        else:
            event_record = existing_event
    else:
        event_record = WebhookEvent(
            event_id=event_id,
            event_type=event_type,
            resource_type=resource_type,
            is_verified=True,
            payload=event_payload,
            processing_status="PENDING",
            received_at=datetime.now(timezone.utc)
        )
        db.add(event_record)
        db.commit()
        db.refresh(event_record)

    # Process Event State
    try:
        result = process_verified_event(db, event_record)
        event_record.processing_status = "PROCESSED"
        event_record.processed_at = datetime.now(timezone.utc)
        event_record.error_detail = None
        db.commit()
        return {
            "status": "PROCESSED",
            "event_id": event_id,
            "event_type": event_type,
            "details": result
        }
    except Exception as e:
        logger.exception("Failed to process simulated webhook event %s: %s", event_id, e)
        event_record.processing_status = "FAILED"
        event_record.error_detail = sanitize_sensitive_data(str(e))
        event_record.retry_count = (event_record.retry_count or 0) + 1
        db.commit()
        return {
            "status": "FAILED",
            "event_id": event_id,
            "error": sanitize_sensitive_data(str(e))
        }


@router.get("", status_code=status.HTTP_200_OK)
@router.get("/paypal", status_code=status.HTTP_200_OK)
def list_webhook_events(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    status_filter: Optional[str] = Query(None, alias="status"),
    event_type: Optional[str] = Query(None),
    db: Session = Depends(get_db)
):
    """
    Lists received PayPal webhook events with their verification and processing status.
    """
    query = db.query(WebhookEvent)
    if status_filter:
        query = query.filter(WebhookEvent.processing_status == status_filter.upper())
    if event_type:
        query = query.filter(WebhookEvent.event_type == event_type)

    total = query.count()
    items = query.order_by(desc(WebhookEvent.received_at)).offset((page - 1) * page_size).limit(page_size).all()

    return {
        "items": [
            {
                "id": it.id,
                "event_id": it.event_id,
                "event_type": it.event_type,
                "resource_type": it.resource_type,
                "is_verified": it.is_verified,
                "processing_status": it.processing_status,
                "error_detail": it.error_detail,
                "retry_count": it.retry_count,
                "received_at": it.received_at.isoformat() if it.received_at else None,
                "processed_at": it.processed_at.isoformat() if it.processed_at else None,
            }
            for it in items
        ],
        "total": total,
        "page": page,
        "page_size": page_size
    }


@router.post("/paypal/{event_id}/retry", status_code=status.HTTP_200_OK)
def retry_webhook_event(
    event_id: str,
    db: Session = Depends(get_db)
):
    """
    Manually retries processing of a previously received and verified webhook event.
    """
    event = db.query(WebhookEvent).filter(WebhookEvent.event_id == event_id).first()
    if not event:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Webhook event '{event_id}' not found.")

    try:
        result = process_verified_event(db, event)
        event.processing_status = "PROCESSED"
        event.processed_at = datetime.now(timezone.utc)
        event.error_detail = None
        db.commit()
        return {
            "success": True,
            "event_id": event.event_id,
            "status": "PROCESSED",
            "details": result
        }
    except Exception as e:
        event.processing_status = "FAILED"
        event.error_detail = sanitize_sensitive_data(str(e))
        event.retry_count = (event.retry_count or 0) + 1
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Retry failed: {sanitize_sensitive_data(str(e))}"
        )
