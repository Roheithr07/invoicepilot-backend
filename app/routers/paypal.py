"""
PayPal REST API Router for InvoicePilot AI.
Endpoints for Sandbox connectivity health checks, configuration audits,
audit-guarded draft creation, explicit two-step invoice dispatch, and remote status reconciliation.
"""

from datetime import datetime, timezone
from typing import Optional, Dict, Any
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.config import settings
from app.models.models import (
    Invoice, ActivityLog, InvoiceStatus, AuditSeverity, User, UserRole
)
from app.auth.dependencies import get_current_user, verify_business_ownership
from app.services.websocket_manager import ws_manager
from app.schemas.paypal import (
    PayPalConfigStatus, PayPalHealthResponse, PayPalDraftRequest,
    PayPalSendRequest, PayPalOperationResponse, PayPalStatusResponse
)
from app.services.paypal_service import (
    PayPalService, PayPalError, PayPalConfigurationError,
    PayPalValidationError, PayPalDuplicateError, sanitize_sensitive_data
)
from app.services.invoice_auditor import InvoiceAuditor

router = APIRouter(prefix="/paypal", tags=["PayPal Invoicing Integration"])


def verify_audit_guardrails(db: Session, invoice: Invoice) -> None:
    """
    Enforces Phase 4 audit guardrails before external financial side-effects.
    Revalidates calculations, checks for unresolved HIGH or CRITICAL issues.
    """
    is_valid, findings, passed_checks, breakdown, evidence = InvoiceAuditor.audit(db, invoice)
    
    # Filter for active/unresolved HIGH or CRITICAL findings
    blocking = [
        f for f in findings
        if f.severity in (AuditSeverity.CRITICAL, AuditSeverity.HIGH) and getattr(f, "status", "OPEN") != "RESOLVED"
    ]

    if blocking:
        reasons = "; ".join([f"[{f.severity.value}] {f.title}: {f.description}" for f in blocking])
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"PayPal Action Blocked by Audit Engine: Invoice has {len(blocking)} unresolved critical finding(s). "
                f"Please review and correct the invoice in the AI Auditor workspace before dispatching. Details: {reasons}"
            )
        )


@router.get("/configuration-status", response_model=PayPalConfigStatus)
def get_configuration_status():
    """
    Returns the configuration readiness of PayPal Sandbox credentials without revealing secret values.
    Also reports the APIMatic Context Plugin compatibility status.
    """
    client_id = (settings.PAYPAL_CLIENT_ID or "").strip()
    client_secret = (settings.PAYPAL_CLIENT_SECRET or "").strip()
    
    is_id_set = bool(client_id and not client_id.startswith("your_"))
    is_sec_set = bool(client_secret and not client_secret.startswith("your_"))
    masked_id = PayPalService.get_masked_client_id()

    webhook_id = (settings.PAYPAL_WEBHOOK_ID or "").strip()
    is_wh_set = bool(webhook_id and not webhook_id.startswith("your_"))

    return PayPalConfigStatus(
        client_id_configured=is_id_set,
        client_secret_configured=is_sec_set,
        sandbox_mode=(settings.PAYPAL_MODE == "sandbox"),
        base_url=PayPalService.get_base_url(),
        masked_client_id=masked_id,
        webhook_id_configured=is_wh_set,
        apimatic_status=(
            "APIMatic Context Plugins installer supports Cursor, VS Code (Copilot MCP), and Claude Code. "
            "For Google Antigravity IDE, verified official PayPal Invoicing REST v2 schemas are utilized."
        )
    )


@router.get("/health", response_model=PayPalHealthResponse)
def check_paypal_health():
    """
    Performs a safe live OAuth 2.0 handshake check against the PayPal Developer Sandbox.
    Verifies that credentials are functional without exposing tokens or secrets.
    """
    health_data = PayPalService.verify_connectivity()
    return PayPalHealthResponse(**health_data)


# -------------------------------------------------------------------------
# Invoice Specific PayPal Endpoints (Under /api/invoices/{id}/paypal)
# -------------------------------------------------------------------------

invoices_paypal_router = APIRouter(prefix="/invoices", tags=["PayPal Invoicing Integration"])


@invoices_paypal_router.post("/{invoice_id}/paypal/draft", response_model=PayPalOperationResponse, status_code=status.HTTP_201_CREATED)
def create_paypal_draft(
    invoice_id: int,
    payload: Optional[PayPalDraftRequest] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Step A: Validates local invoice, runs audit guardrails, and creates an official
    PayPal draft invoice via POST /v2/invoicing/invoices.
    Stores returned PayPal invoice ID and sets local paypal_status to 'DRAFT'.
    """
    invoice = db.query(Invoice).filter(Invoice.id == invoice_id, Invoice.is_deleted == False).first()
    if not invoice:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Invoice ID {invoice_id} not found.")

    verify_business_ownership(invoice, current_user, "Invoice")

    if invoice.paypal_invoice_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Invoice already linked to PayPal draft (ID: {invoice.paypal_invoice_id}). Status: {invoice.paypal_status or 'DRAFT'}."
        )

    # 1. Enforce Audit Guardrails
    verify_audit_guardrails(db, invoice)

    # 2. Call PayPal Invoicing REST API v2
    try:
        remote_data = PayPalService.create_draft_invoice(invoice)
    except PayPalConfigurationError as e:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(e))
    except (PayPalValidationError, PayPalDuplicateError) as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except PayPalError as e:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=sanitize_sensitive_data(str(e)))
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Unexpected error: {sanitize_sensitive_data(str(e))}")

    # 3. Update Database State
    paypal_id = remote_data.get("id")
    paypal_status_val = remote_data.get("status", "DRAFT")
    
    # Extract payer view URL if present
    payer_url = None
    for link in remote_data.get("links", []):
        if link.get("rel") in ("payer-view", "view", "self"):
            payer_url = link.get("href")
            break

    invoice.paypal_invoice_id = paypal_id
    invoice.paypal_status = paypal_status_val
    invoice.paypal_invoice_url = payer_url
    invoice.paypal_draft_created_at = datetime.now(timezone.utc)
    invoice.paypal_last_checked_at = datetime.now(timezone.utc)
    invoice.paypal_error_detail = None

    # Log to ActivityLog
    log = ActivityLog(
        action="PAYPAL_DRAFT_CREATED",
        entity_type="invoice",
        entity_id=str(invoice.id),
        description=f"Created official PayPal draft invoice {paypal_id} for invoice {invoice.invoice_number}."
    )
    db.add(log)
    db.commit()
    db.refresh(invoice)

    # Broadcast real-time event
    ws_manager.broadcast_event(
        "invoice.draft_created",
        {
            "invoice_id": invoice.id,
            "invoice_number": invoice.invoice_number,
            "paypal_invoice_id": paypal_id,
            "paypal_status": paypal_status_val,
            "business_id": invoice.business_id,
        },
        business_id=invoice.business_id
    )

    return PayPalOperationResponse(
        success=True,
        local_invoice_id=invoice.id,
        invoice_number=invoice.invoice_number,
        paypal_invoice_id=paypal_id,
        paypal_status=paypal_status_val,
        paypal_invoice_url=payer_url,
        message=f"Successfully created PayPal draft invoice {paypal_id}."
    )


@invoices_paypal_router.post("/{invoice_id}/paypal/send", response_model=PayPalOperationResponse)
def send_paypal_invoice(
    invoice_id: int,
    payload: PayPalSendRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Step B: Explicit user approval before sending an invoice through PayPal.
    Requires payload.confirmed == True. Dispatches invoice via POST /v2/invoicing/invoices/{id}/send
    and updates verified status to SENT.
    Only BUSINESS_OWNER or SUPER_ADMIN may send an invoice through PayPal.
    """
    if current_user.role not in (UserRole.SUPER_ADMIN, UserRole.BUSINESS_OWNER):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Access Denied: Role '{current_user.role.value}' is not authorized to dispatch PayPal invoices. Invoicing dispatches require BUSINESS_OWNER or SUPER_ADMIN authorization."
        )

    if not payload.confirmed:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Explicit user confirmation is required before dispatching an invoice through PayPal."
        )

    invoice = db.query(Invoice).filter(Invoice.id == invoice_id, Invoice.is_deleted == False).first()
    if not invoice:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Invoice ID {invoice_id} not found.")

    verify_business_ownership(invoice, current_user, "Invoice")

    if not invoice.paypal_invoice_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invoice must first have an active PayPal draft created before it can be sent."
        )

    # Prevent accidental redundant sending
    if invoice.status == InvoiceStatus.SENT or invoice.paypal_status in ("SENT", "PAID", "SCHEDULED"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invoice has already been sent through PayPal (Status: {invoice.paypal_status}). Duplicate send prevented."
        )

    # 1. Enforce Audit Guardrails
    verify_audit_guardrails(db, invoice)

    # 2. Call PayPal Send Endpoint
    send_options = {
        "send_to_recipient": payload.send_to_recipient,
        "send_to_invoicer": payload.send_to_invoicer,
        "subject": payload.subject,
        "note": payload.note
    }

    try:
        verified_data = PayPalService.send_invoice(invoice.paypal_invoice_id, options=send_options)
    except PayPalConfigurationError as e:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(e))
    except PayPalError as e:
        invoice.paypal_error_detail = sanitize_sensitive_data(str(e))
        db.commit()
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=sanitize_sensitive_data(str(e)))
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Unexpected error: {sanitize_sensitive_data(str(e))}")

    # 3. Update Database State with Verified Response
    remote_status = verified_data.get("status", "SENT")
    payer_url = verified_data.get("_extracted_payer_url") or invoice.paypal_invoice_url

    invoice.status = InvoiceStatus.SENT
    invoice.paypal_status = remote_status
    if payer_url:
        invoice.paypal_invoice_url = payer_url
    invoice.paypal_sent_at = datetime.now(timezone.utc)
    invoice.paypal_last_checked_at = datetime.now(timezone.utc)
    invoice.paypal_error_detail = None
    invoice.sent_by_user_id = current_user.id
    invoice.sent_by_name = current_user.name

    # Log to ActivityLog
    log = ActivityLog(
        action="PAYPAL_INVOICE_SENT",
        entity_type="invoice",
        entity_id=str(invoice.id),
        description=(
            f"Dispatched invoice {invoice.invoice_number} via PayPal Sandbox (ID: {invoice.paypal_invoice_id}) by {current_user.name} ({current_user.role.value}). "
            f"Verified Remote Status: {remote_status}."
        )
    )
    db.add(log)
    db.commit()
    db.refresh(invoice)

    # Broadcast real-time event
    ws_manager.broadcast_event(
        "invoice.sent",
        {
            "invoice_id": invoice.id,
            "invoice_number": invoice.invoice_number,
            "paypal_invoice_id": invoice.paypal_invoice_id,
            "paypal_status": remote_status,
            "sent_by": current_user.name,
            "business_id": invoice.business_id,
        },
        business_id=invoice.business_id
    )

    return PayPalOperationResponse(
        success=True,
        local_invoice_id=invoice.id,
        invoice_number=invoice.invoice_number,
        paypal_invoice_id=invoice.paypal_invoice_id,
        paypal_status=remote_status,
        paypal_invoice_url=invoice.paypal_invoice_url,
        message=f"Invoice successfully sent to recipient via PayPal Sandbox. Verified remote status: {remote_status}."
    )


@invoices_paypal_router.get("/{invoice_id}/paypal/status", response_model=PayPalStatusResponse)
def get_paypal_invoice_status(
    invoice_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Retrieves and reconciles the live status of an invoice from the PayPal REST API.
    Updates local status and timestamps based on verified remote state.
    """
    invoice = db.query(Invoice).filter(Invoice.id == invoice_id, Invoice.is_deleted == False).first()
    if not invoice:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Invoice ID {invoice_id} not found.")

    verify_business_ownership(invoice, current_user, "Invoice")

    if not invoice.paypal_invoice_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invoice is not associated with a PayPal invoice ID."
        )

    try:
        remote_data = PayPalService.get_invoice_details(invoice.paypal_invoice_id)
    except PayPalConfigurationError as e:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(e))
    except PayPalError as e:
        # Keep last verified state on error
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Unable to retrieve status from PayPal: {sanitize_sensitive_data(str(e))}. Displaying last known verified state ({invoice.paypal_status or 'UNKNOWN'})."
        )
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Unexpected error: {sanitize_sensitive_data(str(e))}")

    remote_status = remote_data.get("status", "UNKNOWN").upper().strip()
    payer_url = remote_data.get("_extracted_payer_url") or invoice.paypal_invoice_url

    # Reconcile local status based strictly on verified remote response
    old_status = invoice.status
    if remote_status in ("PAID", "MARKED_AS_PAID"):
        invoice.status = InvoiceStatus.PAID
    elif remote_status == "SENT":
        invoice.status = InvoiceStatus.SENT
    elif remote_status == "CANCELLED":
        invoice.status = InvoiceStatus.CANCELLED

    invoice.paypal_status = remote_status
    if payer_url:
        invoice.paypal_invoice_url = payer_url
    invoice.paypal_last_checked_at = datetime.now(timezone.utc)
    invoice.paypal_error_detail = None

    if old_status != invoice.status:
        log = ActivityLog(
            action="PAYPAL_STATUS_RECONCILED",
            entity_type="invoice",
            entity_id=str(invoice.id),
            description=f"Reconciled invoice {invoice.invoice_number} from {old_status.value} to {invoice.status.value} based on PayPal status {remote_status}."
        )
        db.add(log)

    db.commit()
    db.refresh(invoice)

    # Extract amount if present
    amount_total = None
    amt_obj = remote_data.get("amount", {})
    if amt_obj.get("value"):
        try:
            amount_total = float(amt_obj["value"])
        except Exception:
            pass

    return PayPalStatusResponse(
        local_invoice_id=invoice.id,
        invoice_number=invoice.invoice_number,
        paypal_invoice_id=invoice.paypal_invoice_id,
        paypal_status=remote_status,
        local_status=invoice.status.value,
        is_paid=(invoice.status == InvoiceStatus.PAID),
        is_sent=(invoice.status == InvoiceStatus.SENT),
        last_checked_at=invoice.paypal_last_checked_at,
        paypal_invoice_url=invoice.paypal_invoice_url,
        amount_total=amount_total,
        currency=amt_obj.get("currency_code") or invoice.currency,
        raw_remote_summary={
            "id": remote_data.get("id"),
            "status": remote_status,
            "invoice_date": remote_data.get("detail", {}).get("invoice_date"),
            "due_date": remote_data.get("detail", {}).get("payment_term", {}).get("due_date")
        }
    )

