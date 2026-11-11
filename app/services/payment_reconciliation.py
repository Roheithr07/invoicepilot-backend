"""
Payment Reconciliation Engine for InvoicePilot AI.
Matches verified PayPal payments against local invoices using Decimal arithmetic,
multi-attribute matching, currency verification, and immutable audit logging.
"""

from decimal import Decimal, ROUND_HALF_UP
import logging
from typing import Dict, Any, List, Optional, Tuple
from datetime import datetime, timezone

from sqlalchemy.orm import Session
from sqlalchemy import or_, and_

from app.models.models import (
    Payment, Invoice, Customer, InvoiceStatus, PaymentStatus,
    ReconciliationRecord, ReconciliationCategory, ActivityLog
)

logger = logging.getLogger("InvoicePilot.PaymentReconciliation")

TWO_PLACES = Decimal("0.01")


def to_decimal(val: Any) -> Decimal:
    """Safely converts float, int, or string to 2-decimal place Decimal."""
    if val is None:
        return Decimal("0.00")
    try:
        return Decimal(str(val)).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
    except Exception:
        return Decimal("0.00")


class PaymentReconciliationEngine:
    """
    Automated and manual reconciliation engine between PayPal payments and invoices.
    """

    @classmethod
    def find_candidate_invoice(
        cls,
        db: Session,
        payment: Payment,
        paypal_invoice_id: Optional[str] = None,
        invoice_number: Optional[str] = None
    ) -> Optional[Invoice]:
        """
        Locates the targeted local invoice using strict criteria.
        Never matches solely by customer name or solely by amount.
        """
        # 1. Match by linked invoice_id on Payment
        if payment.invoice_id:
            inv = db.query(Invoice).filter(Invoice.id == payment.invoice_id, Invoice.is_deleted == False).first()
            if inv:
                return inv

        # 2. Match by official PayPal Invoice ID (e.g. INV2-XXXX)
        if paypal_invoice_id:
            inv = db.query(Invoice).filter(
                Invoice.paypal_invoice_id == paypal_invoice_id,
                Invoice.is_deleted == False
            ).first()
            if inv:
                return inv

        # 3. Match by explicit invoice number (e.g. INV-2026-001)
        if invoice_number:
            inv = db.query(Invoice).filter(
                Invoice.invoice_number == invoice_number,
                Invoice.is_deleted == False
            ).first()
            if inv:
                return inv

        return None

    @classmethod
    def evaluate_match(
        cls,
        db: Session,
        payment: Payment,
        invoice: Optional[Invoice]
    ) -> Tuple[ReconciliationCategory, float, List[str], List[str]]:
        """
        Evaluates the reconciliation category, confidence, match reasons, and discrepancies.
        """
        match_reasons = []
        discrepancies = []

        if not invoice:
            return (
                ReconciliationCategory.UNMATCHED,
                0.0,
                [],
                ["No corresponding invoice found with matching PayPal ID or invoice reference."]
            )

        # Check for Duplicate payment transaction reference
        if payment.paypal_payment_id:
            duplicate_payments = db.query(Payment).filter(
                Payment.paypal_payment_id == payment.paypal_payment_id,
                Payment.id != payment.id,
                Payment.is_reconciled == True
            ).all()
            if duplicate_payments:
                return (
                    ReconciliationCategory.DUPLICATE,
                    0.2,
                    [f"Found existing payment ID {duplicate_payments[0].id} with identical transaction reference."],
                    [f"Duplicate transaction reference '{payment.paypal_payment_id}' detected."]
                )

        # Check Currency
        pay_currency = (payment.currency or "USD").upper()
        inv_currency = (invoice.currency or "USD").upper()
        if pay_currency != inv_currency:
            discrepancies.append(
                f"Currency mismatch: Payment received in {pay_currency}, but invoice is denominated in {inv_currency}."
            )
            return (
                ReconciliationCategory.CURRENCY_MISMATCH,
                0.4,
                match_reasons,
                discrepancies
            )

        match_reasons.append(f"Currency match: {pay_currency}")

        # Primary Reference Match
        if invoice.paypal_invoice_id:
            match_reasons.append(f"Primary PayPal invoice identifier verified ({invoice.paypal_invoice_id})")
        match_reasons.append(f"Local invoice reference matched ({invoice.invoice_number})")

        # Decimal Amount Comparison
        pay_amount = to_decimal(payment.amount)
        inv_total = to_decimal(invoice.total_amount)

        # Calculate already paid amount on this invoice (excluding current payment)
        prior_payments = db.query(Payment).filter(
            Payment.invoice_id == invoice.id,
            Payment.id != payment.id,
            Payment.status == PaymentStatus.COMPLETED
        ).all()
        already_paid = sum((to_decimal(p.amount) for p in prior_payments), Decimal("0.00"))
        remaining_balance = inv_total - already_paid

        delta = pay_amount - remaining_balance

        if abs(delta) <= Decimal("0.01"):
            match_reasons.append(f"Exact financial match: Payment {pay_amount} matches remaining balance {remaining_balance} {inv_currency}")
            return (
                ReconciliationCategory.MATCHED,
                1.0,
                match_reasons,
                []
            )
        elif pay_amount < remaining_balance:
            discrepancies.append(
                f"Partial payment: Paid {pay_amount} {inv_currency} against remaining balance {remaining_balance} {inv_currency} (Shortfall: {remaining_balance - pay_amount} {inv_currency})"
            )
            return (
                ReconciliationCategory.PARTIALLY_MATCHED,
                0.85,
                match_reasons,
                discrepancies
            )
        else:
            # Overpayment or amount mismatch
            discrepancies.append(
                f"Amount mismatch: Payment {pay_amount} exceeds remaining balance {remaining_balance} {inv_currency} by {pay_amount - remaining_balance} {inv_currency}"
            )
            return (
                ReconciliationCategory.AMOUNT_MISMATCH,
                0.5,
                match_reasons,
                discrepancies
            )

    @classmethod
    def reconcile_payment(
        cls,
        db: Session,
        payment: Payment,
        paypal_invoice_id: Optional[str] = None,
        invoice_number: Optional[str] = None,
        auto_update_invoice_status: bool = True
    ) -> ReconciliationRecord:
        """
        Executes reconciliation for a single payment record, saves audit trail,
        and optionally updates local invoice status.
        """
        invoice = cls.find_candidate_invoice(db, payment, paypal_invoice_id, invoice_number)
        if invoice and payment.invoice_id != invoice.id:
            payment.invoice_id = invoice.id

        category, confidence, reasons, discrepancies = cls.evaluate_match(db, payment, invoice)

        # Determine auto reconciliation status
        status_val = "AUTO_RECONCILED" if category in (ReconciliationCategory.MATCHED, ReconciliationCategory.PARTIALLY_MATCHED) else "PENDING_REVIEW"

        rec_record = ReconciliationRecord(
            payment_id=payment.id,
            invoice_id=invoice.id if invoice else None,
            paypal_reference=payment.paypal_payment_id or payment.paypal_capture_id,
            category=category.value,
            confidence_score=confidence,
            match_reasons=reasons,
            discrepancies=discrepancies,
            status=status_val,
            created_at=datetime.now(timezone.utc)
        )
        db.add(rec_record)
        db.flush()

        # Update Payment Record
        payment.reconciliation_status = category.value
        payment.reconciliation_notes = "; ".join(reasons + discrepancies)

        if category == ReconciliationCategory.MATCHED:
            payment.is_reconciled = True
            payment.reconciled_at = datetime.now(timezone.utc)
            payment.status = PaymentStatus.COMPLETED

            if invoice and auto_update_invoice_status:
                invoice.status = InvoiceStatus.PAID
                invoice.paypal_status = "PAID"
                invoice.paypal_last_checked_at = datetime.now(timezone.utc)

        elif category == ReconciliationCategory.PARTIALLY_MATCHED:
            payment.is_reconciled = True
            payment.reconciled_at = datetime.now(timezone.utc)
            payment.status = PaymentStatus.PARTIALLY_PAID

            if invoice and auto_update_invoice_status:
                invoice.status = InvoiceStatus.UNPAID
                invoice.paypal_status = "PARTIALLY_PAID"
                invoice.paypal_last_checked_at = datetime.now(timezone.utc)

        elif category in (ReconciliationCategory.AMOUNT_MISMATCH, ReconciliationCategory.CURRENCY_MISMATCH, ReconciliationCategory.DUPLICATE):
            payment.is_reconciled = False
            payment.status = PaymentStatus.REQUIRES_REVIEW

        # Log to Activity Log
        log = ActivityLog(
            action="PAYMENT_RECONCILED",
            entity_type="payment",
            entity_id=str(payment.id),
            description=f"Payment {payment.id} reconciled with category {category.value} for invoice {invoice.invoice_number if invoice else 'N/A'}.",
            metadata_json={
                "reconciliation_id": rec_record.id,
                "category": category.value,
                "confidence": confidence,
                "amount": float(payment.amount),
                "currency": payment.currency
            }
        )
        db.add(log)
        db.commit()
        db.refresh(rec_record)
        return rec_record

    @classmethod
    def run_batch_reconciliation(cls, db: Session) -> Dict[str, Any]:
        """
        Scans all unreconciled payments and executes reconciliation matching.
        """
        unreconciled = db.query(Payment).filter(
            or_(
                Payment.is_reconciled == False,
                Payment.reconciliation_status.in_(["UNMATCHED", "REQUIRES_REVIEW"])
            )
        ).all()

        results = {
            "processed": len(unreconciled),
            "matched": 0,
            "partially_matched": 0,
            "requires_review": 0,
            "records": []
        }

        for payment in unreconciled:
            rec = cls.reconcile_payment(db, payment)
            results["records"].append({
                "reconciliation_id": rec.id,
                "payment_id": payment.id,
                "invoice_id": rec.invoice_id,
                "category": rec.category,
                "status": rec.status
            })
            if rec.category == ReconciliationCategory.MATCHED.value:
                results["matched"] += 1
            elif rec.category == ReconciliationCategory.PARTIALLY_MATCHED.value:
                results["partially_matched"] += 1
            else:
                results["requires_review"] += 1

        return results

    @classmethod
    def review_reconciliation(
        cls,
        db: Session,
        reconciliation_id: int,
        decision: str,
        notes: Optional[str] = None,
        reviewer_name: Optional[str] = None
    ) -> ReconciliationRecord:
        """
        Allows human reviewers to RESOLVE, APPROVE, or REJECT a flagged reconciliation match.
        """
        record = db.query(ReconciliationRecord).filter(ReconciliationRecord.id == reconciliation_id).first()
        if not record:
            raise ValueError(f"Reconciliation record ID {reconciliation_id} not found.")

        valid_decisions = ["RESOLVED", "APPROVED", "REJECTED"]
        if decision.upper() not in valid_decisions:
            raise ValueError(f"Invalid review decision '{decision}'. Must be one of {valid_decisions}.")

        record.reviewer_decision = decision.upper()
        record.reviewer_notes = notes or f"Reviewed by {reviewer_name or 'Compliance Officer'}"
        record.reviewed_at = datetime.now(timezone.utc)
        record.reviewed_by = reviewer_name or "Compliance Officer"
        record.status = decision.upper()

        payment = db.query(Payment).filter(Payment.id == record.payment_id).first()
        invoice = db.query(Invoice).filter(Invoice.id == record.invoice_id).first() if record.invoice_id else None

        if decision.upper() in ("RESOLVED", "APPROVED"):
            if payment:
                payment.is_reconciled = True
                payment.reconciled_at = datetime.now(timezone.utc)
                payment.status = PaymentStatus.COMPLETED
            if invoice:
                invoice.status = InvoiceStatus.PAID
                invoice.paypal_status = "PAID"
        elif decision.upper() == "REJECTED":
            if payment:
                payment.is_reconciled = False
                payment.status = PaymentStatus.FAILED

        # Log
        log = ActivityLog(
            action="RECONCILIATION_REVIEWED",
            entity_type="reconciliation",
            entity_id=str(record.id),
            description=f"Reconciliation {record.id} decision '{decision}' applied by {record.reviewed_by}.",
            metadata_json={"decision": decision, "notes": notes}
        )
        db.add(log)
        db.commit()
        db.refresh(record)
        return record

