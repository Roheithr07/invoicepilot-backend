"""
InvoicePilot AI — Read-Only AI Financial Assistant Router.
Provides conversational business intelligence strictly grounded in live SQLite database records.
Never mutates data; rejects prompt injection and mutation attempts.
"""

import logging
from datetime import datetime, timezone
from typing import List, Optional, Dict, Any
from decimal import Decimal
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from sqlalchemy import func

from app.database import get_db
from app.config import settings
from app.models.models import Invoice, InvoiceStatus, Payment, PaymentStatus, ReminderRecord, Customer, ReconciliationRecord
from app.services.paypal_service import sanitize_sensitive_data

logger = logging.getLogger("InvoicePilot.Assistant")

router = APIRouter(prefix="/assistant", tags=["AI Financial Assistant"])


class AssistantQueryInput(BaseModel):
    question: str = Field(..., min_length=1, max_length=1000, description="User financial inquiry")


class AssistantQueryResponse(BaseModel):
    answer: str
    intent: str
    data_sources: List[str]
    context_data: Optional[Dict[str, Any]] = None
    timestamp: datetime


@router.post("/chat", response_model=AssistantQueryResponse, status_code=status.HTTP_200_OK)
def assistant_chat(
    payload: AssistantQueryInput,
    db: Session = Depends(get_db)
):
    """
    Constrained, read-only AI financial assistant.
    Analyzes user inquiries, queries live SQLite invoice/payment records,
    and returns verified, grounded financial intelligence.
    """
    raw_question = payload.question.strip()
    q_lower = raw_question.lower()

    # 1. Guardrail against injection or mutation commands
    mutation_keywords = ["drop", "delete", "update", "insert", "alter", "truncate", "grant", "revoke", "exec", "<script"]
    if any(k in q_lower for k in mutation_keywords) and any(op in q_lower for op in ["table", "database", "record", "from", "into"]):
        return AssistantQueryResponse(
            answer="Security Guardrail Alert: InvoicePilot AI Assistant operates in strict read-only analytics mode. Data manipulation or schema modification operations are barred by policy.",
            intent="SECURITY_BLOCK",
            data_sources=["SecurityGuardrailPolicy"],
            timestamp=datetime.now(timezone.utc)
        )

    # 2. Extract live database metrics
    now = datetime.now()
    
    # Invoices metrics
    all_invoices = db.query(Invoice).filter(Invoice.is_deleted == False).all()
    total_invoiced = sum(Decimal(str(i.total_amount or 0)) for i in all_invoices)
    
    paid_invoices = [i for i in all_invoices if i.status == InvoiceStatus.PAID]
    unpaid_invoices = [i for i in all_invoices if i.status not in (InvoiceStatus.PAID, InvoiceStatus.CANCELLED)]
    
    overdue_invoices = [
        i for i in unpaid_invoices 
        if i.due_date and (i.due_date if isinstance(i.due_date, datetime) else datetime.fromisoformat(str(i.due_date))) < now
    ]
    overdue_sum = sum(Decimal(str(i.total_amount or 0)) for i in overdue_invoices)
    pending_sum = sum(Decimal(str(i.total_amount or 0)) for i in unpaid_invoices)

    # Payments metrics
    completed_payments = db.query(Payment).filter(Payment.status == PaymentStatus.COMPLETED).all()
    total_revenue_collected = sum(Decimal(str(p.amount or 0)) for p in completed_payments)

    # Follow-ups metrics
    pending_reminders = db.query(ReminderRecord).filter(ReminderRecord.status == "PENDING_APPROVAL").all()
    
    # Reconciliation metrics
    total_reconciliations = db.query(ReconciliationRecord).count()
    matched_reconciliations = db.query(ReconciliationRecord).filter(ReconciliationRecord.category == "MATCHED").count()
    match_rate = round((matched_reconciliations / total_reconciliations * 100), 1) if total_reconciliations > 0 else 100.0

    # 3. Classify intent and generate grounded response
    data_sources = ["SQLite:invoices", "SQLite:payments"]
    context_data = {
        "total_invoiced": float(total_invoiced),
        "total_collected": float(total_revenue_collected),
        "outstanding_balance": float(pending_sum),
        "overdue_balance": float(overdue_sum),
        "overdue_count": len(overdue_invoices),
        "unpaid_count": len(unpaid_invoices),
        "pending_followups_count": len(pending_reminders),
        "reconciliation_match_rate": match_rate
    }

    # Intent routing
    if any(k in q_lower for k in ["overdue", "past due", "late"]):
        intent = "OVERDUE_INQUIRY"
        data_sources.append("SQLite:reminder_records")
        if overdue_invoices:
            inv_lines = [
                f"• Invoice {inv.invoice_number} ({inv.customer_name}): ${float(inv.total_amount):,.2f} USD (Due: {inv.due_date.strftime('%Y-%m-%d') if isinstance(inv.due_date, datetime) else str(inv.due_date)[:10]})"
                for inv in overdue_invoices[:5]
            ]
            answer = (
                f"According to verified database records:\n"
                f"You currently have {len(overdue_invoices)} overdue invoice(s) totaling ${float(overdue_sum):,.2f} USD.\n\n"
                + "\n".join(inv_lines) +
                f"\n\nThere are {len(pending_reminders)} draft reminder(s) pending your review in the Smart Follow-ups hub."
            )
        else:
            answer = "Good news! There are currently 0 overdue invoices in the database. All accounts are in good standing."

    elif any(k in q_lower for k in ["follow-up", "followup", "reminder"]):
        intent = "FOLLOWUP_INQUIRY"
        data_sources.append("SQLite:reminder_records")
        if pending_reminders:
            rem_lines = [
                f"• Invoice #{r.invoice_id} ({r.priority_level} Priority, Tone: {r.tone})"
                for r in pending_reminders[:5]
            ]
            answer = (
                f"You have {len(pending_reminders)} follow-up reminder draft(s) awaiting your explicit authorization:\n"
                + "\n".join(rem_lines) +
                f"\n\nYou can inspect, edit, and approve them on the Smart Follow-ups page before sending."
            )
        else:
            answer = f"No pending follow-ups currently require approval. {len(overdue_invoices)} overdue invoices are eligible for a new automated scan."

    elif any(k in q_lower for k in ["revenue", "collected", "paid", "received", "income"]):
        intent = "REVENUE_INQUIRY"
        answer = (
            f"Based on reconciled payments in SQLite:\n"
            f"• Verified Revenue Collected: ${float(total_revenue_collected):,.2f} USD\n"
            f"• Completed Transactions: {len(completed_payments)} payment record(s)\n"
            f"• Payment Reconciliation Match Rate: {match_rate}%\n"
            f"• Outstanding Receivables: ${float(pending_sum):,.2f} USD across {len(unpaid_invoices)} invoice(s)."
        )

    elif any(k in q_lower for k in ["pending", "unpaid", "outstanding", "receivable"]):
        intent = "PENDING_INQUIRY"
        client_breakdown = {}
        for inv in unpaid_invoices:
            cname = inv.customer_name or "Unknown Client"
            client_breakdown[cname] = client_breakdown.get(cname, Decimal("0.0")) + Decimal(str(inv.total_amount or 0))
        
        top_clients = sorted(client_breakdown.items(), key=lambda x: x[1], reverse=True)[:4]
        client_lines = [f"• {c}: ${float(amt):,.2f} USD" for c, amt in top_clients]
        
        answer = (
            f"You have {len(unpaid_invoices)} unpaid invoice(s) totaling ${float(pending_sum):,.2f} USD.\n\n"
            f"Top outstanding client balances:\n" + "\n".join(client_lines) +
            f"\n\nOf this balance, ${float(overdue_sum):,.2f} USD ({len(overdue_invoices)} invoices) is past its due date."
        )

    elif any(k in q_lower for k in ["customer", "client"]):
        intent = "CUSTOMER_INQUIRY"
        data_sources.append("SQLite:customers")
        total_customers = db.query(Customer).count()
        answer = (
            f"You have {total_customers} customer account(s) on file.\n"
            f"• Active Invoices: {len(all_invoices)}\n"
            f"• Settled Payments: {len(completed_payments)}\n"
            f"• Customer payment reliability ratings can be viewed on the Revenue Analytics page."
        )

    elif any(k in q_lower for k in ["audit", "compliance", "error", "discrepancy"]):
        intent = "AUDIT_INQUIRY"
        data_sources.append("SQLite:audit_results")
        answer = (
            f"InvoicePilot AI maintains a 3-Layer Financial Validation Engine:\n"
            f"• Layer 1: Deterministic Python Decimal banking calculations (subtotals, taxes, grand totals).\n"
            f"• Layer 2: Advisory AI explanation with prompt injection defense.\n"
            f"• Layer 3: Human governance review requiring explicit sign-off.\n\n"
            f"Invoices with unreviewed CRITICAL findings are automatically blocked from PayPal dispatch."
        )

    else:
        intent = "GENERAL_FINANCIAL_SUMMARY"
        answer = (
            f"Here is your real-time financial snapshot from SQLite:\n"
            f"• Total Invoiced: ${float(total_invoiced):,.2f} USD across {len(all_invoices)} invoices\n"
            f"• Total Verified Paid: ${float(total_revenue_collected):,.2f} USD\n"
            f"• Outstanding Receivables: ${float(pending_sum):,.2f} USD ({len(unpaid_invoices)} unpaid)\n"
            f"• Overdue Balance: ${float(overdue_sum):,.2f} USD ({len(overdue_invoices)} past due)\n"
            f"• Reconciled Match Rate: {match_rate}%\n"
            f"• Follow-up Reminders Pending Approval: {len(pending_reminders)}\n\n"
            f"Ask me about overdue invoices, payment reconciliation, or specific client balances!"
        )

    return AssistantQueryResponse(
        answer=answer,
        intent=intent,
        data_sources=data_sources,
        context_data=context_data,
        timestamp=datetime.now(timezone.utc)
    )

