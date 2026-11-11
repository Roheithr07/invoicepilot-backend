"""
Reconciliation Router for InvoicePilot AI.
Provides reconciliation triggers, categorized match listings, and human review actions.
"""

from typing import Optional, List
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session
from sqlalchemy import desc

from app.database import get_db
from app.models.models import ReconciliationRecord, Payment, Invoice, Customer
from app.schemas.reconciliation import (
    ReconciliationRecordResponse, ReconciliationReviewInput, ReconciliationStatsResponse
)
from app.services.payment_reconciliation import PaymentReconciliationEngine

router = APIRouter(prefix="/reconciliation", tags=["Payment Reconciliation Engine"])


@router.get("", status_code=status.HTTP_200_OK)
def list_reconciliation_records(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    category: Optional[str] = Query(None),
    status_filter: Optional[str] = Query(None, alias="status"),
    db: Session = Depends(get_db)
):
    """
    Lists reconciliation records with filtering by category (MATCHED, PARTIALLY_MATCHED, etc.)
    and review status.
    """
    query = db.query(ReconciliationRecord)

    if category and category.upper() != "ALL":
        query = query.filter(ReconciliationRecord.category == category.upper())

    if status_filter and status_filter.upper() != "ALL":
        query = query.filter(ReconciliationRecord.status == status_filter.upper())

    total = query.count()
    items = query.order_by(desc(ReconciliationRecord.created_at)).offset((page - 1) * page_size).limit(page_size).all()

    results = []
    for r in items:
        inv = r.invoice
        pay = r.payment
        results.append({
            "id": r.id,
            "payment_id": r.payment_id,
            "invoice_id": r.invoice_id,
            "invoice_number": inv.invoice_number if inv else "N/A",
            "customer_name": inv.customer_name if inv else None,
            "paypal_reference": r.paypal_reference,
            "payment_amount": float(pay.amount) if pay else None,
            "payment_currency": pay.currency if pay else None,
            "invoice_total": float(inv.total_amount) if inv else None,
            "category": r.category,
            "confidence_score": r.confidence_score,
            "match_reasons": r.match_reasons or [],
            "discrepancies": r.discrepancies or [],
            "status": r.status,
            "reviewer_decision": r.reviewer_decision,
            "reviewer_notes": r.reviewer_notes,
            "reviewed_at": r.reviewed_at.isoformat() if r.reviewed_at else None,
            "reviewed_by": r.reviewed_by,
            "created_at": r.created_at.isoformat() if r.created_at else None
        })

    return {
        "items": results,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": (total + page_size - 1) // page_size if total > 0 else 1
    }


@router.get("/stats", response_model=ReconciliationStatsResponse, status_code=status.HTTP_200_OK)
def get_reconciliation_stats(db: Session = Depends(get_db)):
    """
    Returns summary metrics for the reconciliation engine.
    """
    total = db.query(ReconciliationRecord).count()
    matched = db.query(ReconciliationRecord).filter(ReconciliationRecord.category == "MATCHED").count()
    partially_matched = db.query(ReconciliationRecord).filter(ReconciliationRecord.category == "PARTIALLY_MATCHED").count()
    requires_review = db.query(ReconciliationRecord).filter(
        ReconciliationRecord.category.in_(["REQUIRES_REVIEW", "AMOUNT_MISMATCH", "CURRENCY_MISMATCH", "DUPLICATE"])
    ).count()
    unmatched = db.query(ReconciliationRecord).filter(ReconciliationRecord.category == "UNMATCHED").count()

    rate = (matched / total * 100.0) if total > 0 else 100.0

    return ReconciliationStatsResponse(
        total_reconciled=total,
        matched_count=matched,
        partially_matched_count=partially_matched,
        requires_review_count=requires_review,
        unmatched_count=unmatched,
        reconciliation_rate=round(rate, 1)
    )


@router.post("/run", status_code=status.HTTP_200_OK)
def run_batch_reconciliation(db: Session = Depends(get_db)):
    """
    Executes automated reconciliation across all pending unreconciled payments.
    """
    result = PaymentReconciliationEngine.run_batch_reconciliation(db)
    return {
        "success": True,
        "message": f"Processed {result['processed']} unreconciled payments.",
        "details": result
    }


@router.get("/{reconciliation_id}", status_code=status.HTTP_200_OK)
def get_reconciliation_record(
    reconciliation_id: int,
    db: Session = Depends(get_db)
):
    """
    Retrieves full audit breakdown for a single reconciliation record.
    """
    r = db.query(ReconciliationRecord).filter(ReconciliationRecord.id == reconciliation_id).first()
    if not r:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Reconciliation record ID {reconciliation_id} not found.")

    inv = r.invoice
    pay = r.payment
    return {
        "id": r.id,
        "payment_id": r.payment_id,
        "invoice_id": r.invoice_id,
        "invoice_number": inv.invoice_number if inv else "N/A",
        "customer_name": inv.customer_name if inv else None,
        "paypal_reference": r.paypal_reference,
        "payment_amount": float(pay.amount) if pay else None,
        "payment_currency": pay.currency if pay else None,
        "invoice_total": float(inv.total_amount) if inv else None,
        "category": r.category,
        "confidence_score": r.confidence_score,
        "match_reasons": r.match_reasons or [],
        "discrepancies": r.discrepancies or [],
        "status": r.status,
        "reviewer_decision": r.reviewer_decision,
        "reviewer_notes": r.reviewer_notes,
        "reviewed_at": r.reviewed_at.isoformat() if r.reviewed_at else None,
        "reviewed_by": r.reviewed_by,
        "created_at": r.created_at.isoformat() if r.created_at else None
    }


@router.post("/{reconciliation_id}/review", status_code=status.HTTP_200_OK)
def review_reconciliation_record(
    reconciliation_id: int,
    payload: ReconciliationReviewInput,
    db: Session = Depends(get_db)
):
    """
    Human review endpoint allowing approval, resolution, or rejection of a flagged reconciliation.
    """
    try:
        updated = PaymentReconciliationEngine.review_reconciliation(
            db=db,
            reconciliation_id=reconciliation_id,
            decision=payload.decision,
            notes=payload.notes,
            reviewer_name=payload.reviewer_name
        )
        return {
            "success": True,
            "reconciliation_id": updated.id,
            "decision": updated.reviewer_decision,
            "status": updated.status,
            "message": f"Reconciliation {reconciliation_id} successfully marked as {updated.status}."
        }
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))

