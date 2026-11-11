"""
Live Dynamic Dashboard Router for InvoicePilot AI.
Calculates real-time financial metrics, collections, and reconciliation performance
using database queries strictly filtered by the authenticated user's business organization.
"""

from typing import List, Dict, Any, Optional
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import func, desc

from app.database import get_db
from app.models.models import (
    Invoice, Payment, ReconciliationRecord, Customer, ActivityLog,
    InvoiceStatus, PaymentStatus, User
)
from app.auth.dependencies import get_current_active_user, filter_by_business_scope

router = APIRouter(prefix="/dashboard", tags=["Live Dashboard Analytics"])


class ActivityLogItem(BaseModel):
    id: int
    action: str
    entity_type: str
    entity_id: Optional[str] = None
    description: str
    created_at: Optional[str] = None


class DashboardStatsResponse(BaseModel):
    business_id: Optional[int] = None
    business_name: Optional[str] = None
    total_invoices: int
    paid_invoices: int
    pending_invoices: int
    overdue_invoices: int
    total_revenue_collected: float
    total_outstanding_amount: float
    total_customers: int
    total_reconciled: int
    reconciliation_rate: float
    recent_activities: List[ActivityLogItem]


@router.get("/stats", response_model=DashboardStatsResponse)
def get_dashboard_stats(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """
    Computes live dashboard KPIs from persistent database records.
    Applies multi-tenant business filtering so users only see their own organization's metrics.
    """
    # 1. Base queries with multi-tenant filter
    inv_query = db.query(Invoice).filter(Invoice.is_deleted == False)
    inv_query = filter_by_business_scope(inv_query, Invoice, current_user)

    total_invoices = inv_query.count()
    paid_invoices = inv_query.filter(Invoice.status == InvoiceStatus.PAID).count()
    overdue_invoices = inv_query.filter(Invoice.status == InvoiceStatus.OVERDUE).count()
    pending_invoices = inv_query.filter(
        Invoice.status.in_([InvoiceStatus.DRAFT, InvoiceStatus.SENT, InvoiceStatus.UNPAID])
    ).count()

    # 2. Revenue collected & Outstanding amount
    collected_res = inv_query.filter(Invoice.status == InvoiceStatus.PAID).with_entities(
        func.coalesce(func.sum(Invoice.total_amount), 0.0)
    ).scalar()
    total_revenue_collected = round(float(collected_res or 0.0), 2)

    outstanding_res = inv_query.filter(
        Invoice.status.in_([InvoiceStatus.DRAFT, InvoiceStatus.SENT, InvoiceStatus.UNPAID, InvoiceStatus.OVERDUE])
    ).with_entities(
        func.coalesce(func.sum(Invoice.total_amount), 0.0)
    ).scalar()
    total_outstanding_amount = round(float(outstanding_res or 0.0), 2)

    # 3. Customers count
    cust_query = db.query(Customer)
    cust_query = filter_by_business_scope(cust_query, Customer, current_user)
    total_customers = cust_query.count()

    # 4. Reconciliation metrics
    recon_query = db.query(ReconciliationRecord)
    total_reconciled = recon_query.count()
    matched_count = recon_query.filter(ReconciliationRecord.category == "MATCHED").count()
    recon_rate = round((matched_count / total_reconciled * 100.0), 1) if total_reconciled > 0 else 100.0

    # 5. Recent Activities
    act_query = db.query(ActivityLog).order_by(desc(ActivityLog.created_at)).limit(10)
    recent_logs = act_query.all()
    activities = [
        ActivityLogItem(
            id=log.id,
            action=log.action,
            entity_type=log.entity_type,
            entity_id=log.entity_id,
            description=log.description,
            created_at=log.created_at.isoformat() if log.created_at else None
        )
        for log in recent_logs
    ]

    b_name = current_user.business.name if current_user.business else "Master System"

    return DashboardStatsResponse(
        business_id=current_user.business_id,
        business_name=b_name,
        total_invoices=total_invoices,
        paid_invoices=paid_invoices,
        pending_invoices=pending_invoices,
        overdue_invoices=overdue_invoices,
        total_revenue_collected=total_revenue_collected,
        total_outstanding_amount=total_outstanding_amount,
        total_customers=total_customers,
        total_reconciled=total_reconciled,
        reconciliation_rate=recon_rate,
        recent_activities=activities
    )

