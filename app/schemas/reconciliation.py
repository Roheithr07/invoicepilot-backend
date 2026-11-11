"""
Pydantic Schemas for Payment Reconciliation.
"""

from datetime import datetime
from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field


class ReconciliationRecordResponse(BaseModel):
    id: int
    payment_id: Optional[int] = None
    invoice_id: Optional[int] = None
    invoice_number: Optional[str] = None
    customer_name: Optional[str] = None
    paypal_reference: Optional[str] = None
    payment_amount: Optional[float] = None
    payment_currency: Optional[str] = None
    invoice_total: Optional[float] = None
    category: str
    confidence_score: float
    match_reasons: List[str] = []
    discrepancies: List[str] = []
    status: str
    reviewer_decision: Optional[str] = None
    reviewer_notes: Optional[str] = None
    reviewed_at: Optional[datetime] = None
    reviewed_by: Optional[str] = None
    created_at: datetime

    class Config:
        from_attributes = True


class ReconciliationReviewInput(BaseModel):
    decision: str = Field(..., description="RESOLVED, APPROVED, or REJECTED")
    notes: Optional[str] = Field(None, description="Resolution notes")
    reviewer_name: Optional[str] = Field(None, description="Name/title of reviewer")


class ReconciliationStatsResponse(BaseModel):
    total_reconciled: int
    matched_count: int
    partially_matched_count: int
    requires_review_count: int
    unmatched_count: int
    reconciliation_rate: float

