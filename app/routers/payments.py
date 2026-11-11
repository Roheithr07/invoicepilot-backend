"""
Payments Router for InvoicePilot AI.
Provides verified payment transaction search, status inspection, and reporting.
"""

from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session
from sqlalchemy import desc, or_

from app.database import get_db
from app.models.models import Payment, Invoice, Customer, PaymentStatus
from app.schemas.payment import PaymentResponse, PaymentListResponse, PaymentCreate

router = APIRouter(prefix="/payments", tags=["Payments & Transactions"])


@router.get("", response_model=PaymentListResponse, status_code=status.HTTP_200_OK)
def list_payments(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    status: Optional[str] = Query(None),
    invoice_id: Optional[int] = Query(None),
    search: Optional[str] = Query(None),
    reconciled: Optional[bool] = Query(None),
    db: Session = Depends(get_db)
):
    """
    Lists payments with pagination, status filtering, invoice linking, and search.
    Calculates total verified revenue collected.
    """
    query = db.query(Payment).join(Invoice, Payment.invoice_id == Invoice.id)

    if status and status.upper() != "ALL":
        query = query.filter(Payment.status == status.upper())

    if invoice_id:
        query = query.filter(Payment.invoice_id == invoice_id)

    if reconciled is not None:
        query = query.filter(Payment.is_reconciled == reconciled)

    if search:
        search_filter = f"%{search}%"
        query = query.outerjoin(Customer, Invoice.customer_id == Customer.id).filter(
            or_(
                Payment.paypal_payment_id.ilike(search_filter),
                Invoice.invoice_number.ilike(search_filter),
                Customer.name.ilike(search_filter),
                Payment.payer_email.ilike(search_filter)
            )
        )

    total = query.count()
    items = query.order_by(desc(Payment.payment_date)).offset((page - 1) * page_size).limit(page_size).all()

    # Calculate total collected from COMPLETED payments
    completed_payments = db.query(Payment).filter(Payment.status == PaymentStatus.COMPLETED).all()
    total_collected = sum((float(p.amount) for p in completed_payments), 0.0)

    results = []
    for p in items:
        inv = p.invoice
        cust_name = inv.customer_name if inv else None
        cust_email = inv.customer_email if inv else None
        results.append(
            PaymentResponse(
                id=p.id,
                invoice_id=p.invoice_id,
                invoice_number=inv.invoice_number if inv else "N/A",
                customer_name=cust_name,
                customer_email=cust_email,
                paypal_payment_id=p.paypal_payment_id,
                paypal_capture_id=p.paypal_capture_id,
                payer_email=p.payer_email,
                payer_name=p.payer_name,
                amount=float(p.amount),
                currency=p.currency,
                fee_amount=float(p.fee_amount or 0.0),
                net_amount=float(p.net_amount or 0.0),
                status=p.status.value if hasattr(p.status, "value") else str(p.status),
                payment_method=p.payment_method or "PAYPAL",
                payment_date=p.payment_date,
                is_reconciled=p.is_reconciled or False,
                reconciled_at=p.reconciled_at,
                reconciliation_status=p.reconciliation_status or "UNMATCHED",
                reconciliation_notes=p.reconciliation_notes,
                notes=p.notes
            )
        )

    total_pages = (total + page_size - 1) // page_size if total > 0 else 1

    return PaymentListResponse(
        items=results,
        total=total,
        page=page,
        page_size=page_size,
        total_pages=total_pages,
        total_amount_collected=round(total_collected, 2)
    )


@router.get("/{payment_id}", response_model=PaymentResponse, status_code=status.HTTP_200_OK)
def get_payment(
    payment_id: int,
    db: Session = Depends(get_db)
):
    """
    Retrieves full details for a single payment transaction.
    """
    payment = db.query(Payment).filter(Payment.id == payment_id).first()
    if not payment:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Payment ID {payment_id} not found.")

    inv = payment.invoice
    return PaymentResponse(
        id=payment.id,
        invoice_id=payment.invoice_id,
        invoice_number=inv.invoice_number if inv else "N/A",
        customer_name=inv.customer_name if inv else None,
        customer_email=inv.customer_email if inv else None,
        paypal_payment_id=payment.paypal_payment_id,
        paypal_capture_id=payment.paypal_capture_id,
        payer_email=payment.payer_email,
        payer_name=payment.payer_name,
        amount=float(payment.amount),
        currency=payment.currency,
        fee_amount=float(payment.fee_amount or 0.0),
        net_amount=float(payment.net_amount or 0.0),
        status=payment.status.value if hasattr(payment.status, "value") else str(payment.status),
        payment_method=payment.payment_method or "PAYPAL",
        payment_date=payment.payment_date,
        is_reconciled=payment.is_reconciled or False,
        reconciled_at=payment.reconciled_at,
        reconciliation_status=payment.reconciliation_status or "UNMATCHED",
        reconciliation_notes=payment.reconciliation_notes,
        notes=payment.notes
    )

