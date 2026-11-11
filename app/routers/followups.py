"""
Smart Follow-ups & Reminders Router for InvoicePilot AI.
Provides follow-up draft generation, priority inspection, editing, and strict two-stage human approval.
"""

from typing import Optional, List
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session
from sqlalchemy import desc

from app.database import get_db
from app.models.models import ReminderRecord, Invoice, Customer
from app.schemas.followup import (
    ReminderResponse, ReminderUpdateInput, ReminderApprovalInput, ReminderDispatchInput
)
from app.services.followup_engine import FollowUpEngine

router = APIRouter(prefix="/follow-ups", tags=["AI Smart Follow-ups"])


@router.get("", response_model=List[ReminderResponse], status_code=status.HTTP_200_OK)
def list_followups(
    status_filter: Optional[str] = Query(None, alias="status"),
    priority: Optional[str] = Query(None),
    invoice_id: Optional[int] = Query(None),
    db: Session = Depends(get_db)
):
    """
    Lists follow-up reminders filtered by status (PENDING_APPROVAL, APPROVED, DISPATCHED) or priority.
    """
    query = db.query(ReminderRecord).join(Invoice, ReminderRecord.invoice_id == Invoice.id)

    if status_filter and status_filter.upper() != "ALL":
        query = query.filter(ReminderRecord.status == status_filter.upper())

    if priority and priority.upper() != "ALL":
        query = query.filter(ReminderRecord.priority_level == priority.upper())

    if invoice_id:
        query = query.filter(ReminderRecord.invoice_id == invoice_id)

    items = query.order_by(desc(ReminderRecord.created_at)).all()

    results = []
    now = datetime.now(timezone.utc)
    for r in items:
        inv = r.invoice
        due_str = inv.due_date.strftime("%Y-%m-%d") if inv.due_date else None
        
        days_overdue = 0
        if inv.due_date:
            due = inv.due_date if inv.due_date.tzinfo else inv.due_date.replace(tzinfo=timezone.utc)
            days_overdue = (now - due).days

        results.append(
            ReminderResponse(
                id=r.id,
                invoice_id=r.invoice_id,
                invoice_number=inv.invoice_number,
                customer_name=inv.customer_name,
                customer_email=r.recipient_email,
                amount=float(inv.total_amount),
                currency=inv.currency,
                due_date=due_str,
                days_overdue=days_overdue,
                reminder_type=r.reminder_type,
                priority_level=r.priority_level or "MEDIUM",
                priority_reason=r.priority_reason,
                recommended_send_date=r.recommended_send_date,
                tone=r.tone or "FRIENDLY",
                subject=r.subject,
                body=r.body,
                status=r.status or "PENDING_APPROVAL",
                dispatch_method=r.dispatch_method,
                dispatch_notes=r.dispatch_notes,
                is_approved=r.is_approved or False,
                approved_at=r.approved_at,
                sent_at=r.sent_at,
                created_at=r.created_at
            )
        )

    return results


@router.post("/generate", status_code=status.HTTP_200_OK)
def generate_followup_drafts(db: Session = Depends(get_db)):
    """
    Scans unpaid invoices in SQLite and generates intelligent, prioritized reminder drafts.
    """
    drafts = FollowUpEngine.scan_and_generate_followups(db)
    return {
        "success": True,
        "generated_count": len(drafts),
        "message": f"Generated or refreshed {len(drafts)} smart follow-up reminder draft(s)."
    }


@router.get("/{reminder_id}", response_model=ReminderResponse, status_code=status.HTTP_200_OK)
def get_followup(
    reminder_id: int,
    db: Session = Depends(get_db)
):
    """
    Retrieves full details for a single follow-up reminder.
    """
    r = db.query(ReminderRecord).filter(ReminderRecord.id == reminder_id).first()
    if not r:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Reminder ID {reminder_id} not found.")

    inv = r.invoice
    due_str = inv.due_date.strftime("%Y-%m-%d") if inv.due_date else None
    now = datetime.now(timezone.utc)
    days_overdue = 0
    if inv.due_date:
        due = inv.due_date if inv.due_date.tzinfo else inv.due_date.replace(tzinfo=timezone.utc)
        days_overdue = (now - due).days

    return ReminderResponse(
        id=r.id,
        invoice_id=r.invoice_id,
        invoice_number=inv.invoice_number,
        customer_name=inv.customer_name,
        customer_email=r.recipient_email,
        amount=float(inv.total_amount),
        currency=inv.currency,
        due_date=due_str,
        days_overdue=days_overdue,
        reminder_type=r.reminder_type,
        priority_level=r.priority_level or "MEDIUM",
        priority_reason=r.priority_reason,
        recommended_send_date=r.recommended_send_date,
        tone=r.tone or "FRIENDLY",
        subject=r.subject,
        body=r.body,
        status=r.status or "PENDING_APPROVAL",
        dispatch_method=r.dispatch_method,
        dispatch_notes=r.dispatch_notes,
        is_approved=r.is_approved or False,
        approved_at=r.approved_at,
        sent_at=r.sent_at,
        created_at=r.created_at
    )


@router.put("/{reminder_id}", response_model=ReminderResponse, status_code=status.HTTP_200_OK)
def update_followup(
    reminder_id: int,
    payload: ReminderUpdateInput,
    db: Session = Depends(get_db)
):
    """
    Allows user to edit the reminder subject, body, or tone before approval.
    """
    r = db.query(ReminderRecord).filter(ReminderRecord.id == reminder_id).first()
    if not r:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Reminder ID {reminder_id} not found.")

    if r.status == "DISPATCHED":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot edit a reminder that has already been dispatched.")

    if payload.subject is not None:
        r.subject = payload.subject
    if payload.body is not None:
        r.body = payload.body
    if payload.tone is not None:
        r.tone = payload.tone

    db.commit()
    db.refresh(r)
    return get_followup(reminder_id, db)


@router.post("/{reminder_id}/approve", status_code=status.HTTP_200_OK)
def approve_followup(
    reminder_id: int,
    payload: Optional[ReminderApprovalInput] = None,
    db: Session = Depends(get_db)
):
    """
    Stage B: Explicit human approval for a pending reminder.
    """
    approver = payload.approver_name if payload else "Authorized Manager"
    try:
        updated = FollowUpEngine.approve_reminder(db, reminder_id, approver_name=approver)
        return {
            "success": True,
            "reminder_id": updated.id,
            "status": updated.status,
            "is_approved": updated.is_approved,
            "message": f"Reminder {reminder_id} successfully approved by {approver}."
        }
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@router.post("/{reminder_id}/dispatch", status_code=status.HTTP_200_OK)
def dispatch_followup(
    reminder_id: int,
    payload: Optional[ReminderDispatchInput] = None,
    db: Session = Depends(get_db)
):
    """
    Stage B: Dispatches an approved reminder.
    """
    channel = payload.channel if payload else "auto"
    try:
        res = FollowUpEngine.dispatch_reminder(db, reminder_id, channel=channel)
        return res
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))

