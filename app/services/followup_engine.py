"""
AI Smart Follow-up Engine for InvoicePilot AI.
Analyzes invoice payment chronology, calculates explainable priority scores,
generates personalized reminder drafts, and enforces strict two-stage human approval.
"""

from datetime import datetime, timezone, timedelta
from decimal import Decimal
import logging
from typing import Dict, Any, List, Optional, Tuple

from sqlalchemy.orm import Session
from sqlalchemy import and_, or_

from app.models.models import (
    Invoice, Customer, InvoiceStatus, ReminderRecord, ActivityLog, Payment
)
from app.services.paypal_service import PayPalService, PayPalError

logger = logging.getLogger("InvoicePilot.FollowUpEngine")


class FollowUpEngine:
    """
    Intelligent payment follow-up and reminder generator.
    Strictly human-in-the-loop: reminders require explicit approval before dispatch.
    """

    @classmethod
    def analyze_invoice_urgency(
        cls,
        invoice: Invoice,
        now: Optional[datetime] = None
    ) -> Tuple[str, str, str, datetime]:
        """
        Determines priority_level, priority_reason, suggested tone, and recommended_send_date.
        Never makes unsupported claims; calculation is 100% deterministic and explainable.
        """
        current_time = now or datetime.now(timezone.utc)
        
        # Check Due Date
        if not invoice.due_date:
            return (
                "LOW",
                "Invoice has no explicit payment due date recorded.",
                "FRIENDLY",
                current_time
            )

        due_date = invoice.due_date
        if due_date.tzinfo is None:
            due_date = due_date.replace(tzinfo=timezone.utc)

        delta = current_time - due_date
        days_overdue = delta.days

        amount_val = float(invoice.total_amount)
        currency_val = invoice.currency or "USD"

        # Count prior reminders
        prior_reminders_count = len(invoice.reminders) if invoice.reminders else 0

        if days_overdue > 21:
            priority = "URGENT"
            tone = "URGENT"
            reason = f"Invoice is {days_overdue} days overdue ({amount_val:,.2f} {currency_val}) with {prior_reminders_count} prior reminder(s)."
            rec_date = current_time
        elif days_overdue >= 8:
            priority = "HIGH"
            tone = "FIRM"
            reason = f"Invoice is {days_overdue} days overdue for {amount_val:,.2f} {currency_val}."
            rec_date = current_time
        elif days_overdue >= 1:
            priority = "MEDIUM"
            tone = "POLITE"
            reason = f"Invoice is {days_overdue} day(s) overdue ({amount_val:,.2f} {currency_val})."
            rec_date = current_time
        elif days_overdue >= -3:
            # Approaching due date within 3 days
            priority = "LOW"
            tone = "FRIENDLY"
            days_until = abs(days_overdue)
            reason = f"Invoice payment is due in {days_until} day(s) on {due_date.strftime('%B %d, %Y')}."
            rec_date = current_time
        else:
            # Further out than 3 days
            priority = "LOW"
            tone = "FRIENDLY"
            days_until = abs(days_overdue)
            reason = f"Invoice payment is due in {days_until} days."
            rec_date = due_date - timedelta(days=2)

        # Elevate priority if invoice is high value (> $5,000)
        if amount_val >= 5000 and priority in ("LOW", "MEDIUM") and days_overdue >= 0:
            priority = "HIGH"
            reason += f" Elevated to HIGH priority due to substantial contract balance (${amount_val:,.2f})."

        return priority, reason, tone, rec_date

    @classmethod
    def generate_reminder_content(
        cls,
        invoice: Invoice,
        tone: str,
        priority: str
    ) -> Tuple[str, str]:
        """
        Produces personalized, professional reminder subject and body.
        Uses verified factual fields: invoice number, customer name, due date, amount, currency.
        """
        inv_num = invoice.invoice_number
        client_name = invoice.customer_name or invoice.customer.name if invoice.customer else "Valued Client"
        amount = f"${invoice.total_amount:,.2f} {invoice.currency}"
        due_str = invoice.due_date.strftime("%B %d, %Y") if invoice.due_date else "upon receipt"

        if tone == "FRIENDLY":
            subject = f"Friendly Reminder: Invoice {inv_num} Due Soon"
            body = (
                f"Dear {client_name},\n\n"
                f"We hope this message finds you well.\n\n"
                f"This is a gentle reminder that invoice {inv_num} for {amount} is scheduled for payment on {due_str}.\n\n"
                f"You can review the invoice and complete payment easily through our secure PayPal portal.\n"
                f"Please let us know if you have any questions or require any adjustments.\n\n"
                f"Thank you for your business!\n\n"
                f"Best regards,\n"
                f"Accounts Receivable Team"
            )
        elif tone == "POLITE":
            subject = f"Payment Reminder: Invoice {inv_num} is Now Overdue"
            body = (
                f"Dear {client_name},\n\n"
                f"We are writing regarding invoice {inv_num} for {amount}, which was due on {due_str}.\n\n"
                f"According to our records, this payment remains outstanding. Please take a moment to remit payment via PayPal at your earliest convenience.\n\n"
                f"If payment has already been sent, please disregard this notice or reply with the transaction reference.\n\n"
                f"Sincerely,\n"
                f"Finance & Billing Support"
            )
        elif tone == "FIRM":
            subject = f"Urgent Notice: Outstanding Payment for Invoice {inv_num}"
            body = (
                f"Dear {client_name},\n\n"
                f"Our records indicate that invoice {inv_num} for {amount} is significantly overdue (Due Date: {due_str}).\n\n"
                f"To maintain your account in good standing and ensure uninterrupted service, please settle the outstanding balance today via PayPal.\n\n"
                f"If you are experiencing any billing or payment issues, please contact our team immediately so we can assist you.\n\n"
                f"Respectfully,\n"
                f"Accounts Management"
            )
        else:  # URGENT
            subject = f"FINAL NOTICE: Immediate Action Required for Overdue Invoice {inv_num}"
            body = (
                f"Dear {client_name},\n\n"
                f"This is an urgent notice regarding overdue invoice {inv_num} in the amount of {amount}, which expired on {due_str}.\n\n"
                f"Despite prior notices, we have not yet received payment. Please remit the full balance immediately via our secure PayPal payment link to avoid administrative suspension of services.\n\n"
                f"If you need to discuss a structured payment arrangement, contact our accounting team immediately.\n\n"
                f"Urgent Billing Department"
            )

        return subject, body

    @classmethod
    def generate_draft_for_invoice(
        cls,
        db: Session,
        invoice: Invoice
    ) -> Optional[ReminderRecord]:
        """
        Creates or updates a pending reminder draft for an eligible invoice.
        Strictly excludes PAID and CANCELLED invoices.
        """
        # Exclude paid, cancelled, or soft-deleted invoices
        if invoice.status in (InvoiceStatus.PAID, InvoiceStatus.CANCELLED) or invoice.is_deleted:
            return None

        # Exclude if PayPal status is verified as PAID
        if invoice.paypal_status in ("PAID", "CANCELLED"):
            return None

        recipient_email = invoice.customer_email or (invoice.customer.email if invoice.customer else None)
        if not recipient_email:
            logger.warning("Invoice %s lacks customer email; cannot create reminder.", invoice.invoice_number)
            return None

        # Check if an un-dispatched reminder already exists to avoid spam
        existing_draft = db.query(ReminderRecord).filter(
            ReminderRecord.invoice_id == invoice.id,
            ReminderRecord.status.in_(["PENDING_APPROVAL", "APPROVED"])
        ).first()

        priority, reason, tone, rec_date = cls.analyze_invoice_urgency(invoice)
        subject, body = cls.generate_reminder_content(invoice, tone, priority)

        if existing_draft:
            # Refresh draft with latest calculations
            existing_draft.priority_level = priority
            existing_draft.priority_reason = reason
            existing_draft.tone = tone
            existing_draft.recommended_send_date = rec_date
            existing_draft.subject = subject
            existing_draft.body = body
            db.commit()
            db.refresh(existing_draft)
            return existing_draft

        # Create new reminder record
        reminder = ReminderRecord(
            invoice_id=invoice.id,
            recipient_email=recipient_email,
            subject=subject,
            body=body,
            reminder_type="OVERDUE" if priority in ("HIGH", "URGENT") else "DUE_SOON",
            priority_level=priority,
            priority_reason=reason,
            recommended_send_date=rec_date,
            tone=tone,
            status="PENDING_APPROVAL",
            is_approved=False,
            created_at=datetime.now(timezone.utc)
        )
        db.add(reminder)
        db.commit()
        db.refresh(reminder)

        # Log
        log = ActivityLog(
            action="FOLLOWUP_DRAFT_CREATED",
            entity_type="reminder",
            entity_id=str(reminder.id),
            description=f"Generated follow-up draft for invoice {invoice.invoice_number} ({priority} priority).",
            metadata_json={"priority": priority, "tone": tone, "invoice_id": invoice.id}
        )
        db.add(log)
        db.commit()
        return reminder

    @classmethod
    def scan_and_generate_followups(cls, db: Session) -> List[ReminderRecord]:
        """
        Scans all unpaid invoices in SQLite and generates tailored reminder drafts.
        """
        eligible_invoices = db.query(Invoice).filter(
            Invoice.status.in_([InvoiceStatus.SENT, InvoiceStatus.UNPAID, InvoiceStatus.OVERDUE, InvoiceStatus.DRAFT]),
            Invoice.is_deleted == False
        ).all()

        drafts = []
        for inv in eligible_invoices:
            # Only consider invoices that have due dates or are overdue/due soon
            if inv.paypal_status in ("PAID", "CANCELLED"):
                continue
            draft = cls.generate_draft_for_invoice(db, inv)
            if draft:
                drafts.append(draft)

        return drafts

    @classmethod
    def approve_reminder(
        cls,
        db: Session,
        reminder_id: int,
        approver_name: Optional[str] = None
    ) -> ReminderRecord:
        """
        Stage B Step 1: Explicit human approval for a pending reminder draft.
        """
        reminder = db.query(ReminderRecord).filter(ReminderRecord.id == reminder_id).first()
        if not reminder:
            raise ValueError(f"Reminder record ID {reminder_id} not found.")

        reminder.is_approved = True
        reminder.status = "APPROVED"
        reminder.approved_at = datetime.now(timezone.utc)
        reminder.approved_by = approver_name or "Authorized Billing Manager"

        log = ActivityLog(
            action="FOLLOWUP_APPROVED",
            entity_type="reminder",
            entity_id=str(reminder.id),
            description=f"Reminder {reminder.id} for invoice {reminder.invoice.invoice_number} approved by {reminder.approved_by}.",
            metadata_json={"reminder_id": reminder.id, "approved_by": reminder.approved_by}
        )
        db.add(log)
        db.commit()
        db.refresh(reminder)
        return reminder

    @classmethod
    def dispatch_reminder(
        cls,
        db: Session,
        reminder_id: int,
        channel: str = "auto"
    ) -> Dict[str, Any]:
        """
        Stage B Step 2: Dispatches an approved reminder.
        Never dispatches unapproved reminders.
        If email SMTP is not configured, provides clear notice and marks as MANUAL_COPY.
        If PayPal invoice is active, dispatches via PayPal Sandbox Remind API.
        """
        reminder = db.query(ReminderRecord).filter(ReminderRecord.id == reminder_id).first()
        if not reminder:
            raise ValueError(f"Reminder record ID {reminder_id} not found.")

        if not reminder.is_approved:
            raise ValueError("Reminder must be explicitly approved by a user before it can be dispatched.")

        invoice = reminder.invoice
        dispatch_method = "MANUAL_COPY"
        dispatch_notes = "Dispatch channel not configured; message prepared for manual copying and client communication."
        paypal_dispatched = False

        # Attempt PayPal Sandbox invoice reminder if invoice has an active PayPal draft/sent ID
        if invoice.paypal_invoice_id and invoice.paypal_status in ("SENT", "UNPAID", "OVERDUE"):
            try:
                PayPalService.send_invoice_reminder(
                    paypal_invoice_id=invoice.paypal_invoice_id,
                    subject=reminder.subject,
                    note=reminder.body[:400]
                )
                dispatch_method = "PAYPAL_REMINDER"
                dispatch_notes = f"Dispatched officially through PayPal Invoicing Sandbox for invoice {invoice.paypal_invoice_id}."
                paypal_dispatched = True
            except Exception as e:
                logger.warning("PayPal reminder dispatch failed, falling back to manual copy: %s", e)
                dispatch_notes = f"PayPal reminder failed ({str(e)}). Retained as manual copy draft."

        reminder.status = "DISPATCHED"
        reminder.dispatch_method = dispatch_method
        reminder.dispatch_notes = dispatch_notes
        reminder.sent_at = datetime.now(timezone.utc)

        log = ActivityLog(
            action="FOLLOWUP_DISPATCHED",
            entity_type="reminder",
            entity_id=str(reminder.id),
            description=f"Reminder {reminder.id} dispatched via {dispatch_method} for invoice {invoice.invoice_number}.",
            metadata_json={"dispatch_method": dispatch_method, "paypal_dispatched": paypal_dispatched}
        )
        db.add(log)
        db.commit()
        db.refresh(reminder)

        return {
            "success": True,
            "reminder_id": reminder.id,
            "invoice_number": invoice.invoice_number,
            "status": reminder.status,
            "dispatch_method": dispatch_method,
            "dispatch_notes": dispatch_notes,
            "sent_at": reminder.sent_at.isoformat(),
            "message": f"Reminder successfully recorded and dispatched via {dispatch_method}."
        }

