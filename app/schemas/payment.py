"""
Pydantic Schemas for Payment Records.
"""

from datetime import datetime
from typing import Optional, List
from pydantic import BaseModel, Field


class PaymentBase(BaseModel):
    amount: float
    currency: str = "USD"
    payment_method: str = "PAYPAL"
    notes: Optional[str] = None


class PaymentCreate(PaymentBase):
    invoice_id: int
    paypal_payment_id: Optional[str] = None
    paypal_capture_id: Optional[str] = None
    payer_email: Optional[str] = None
    payer_name: Optional[str] = None
    status: str = "COMPLETED"


class PaymentResponse(PaymentBase):
    id: int
    invoice_id: int
    invoice_number: Optional[str] = None
    customer_name: Optional[str] = None
    customer_email: Optional[str] = None
    paypal_payment_id: Optional[str] = None
    paypal_capture_id: Optional[str] = None
    payer_email: Optional[str] = None
    payer_name: Optional[str] = None
    fee_amount: float = 0.0
    net_amount: float = 0.0
    status: str
    payment_date: datetime
    is_reconciled: bool = False
    reconciled_at: Optional[datetime] = None
    reconciliation_status: Optional[str] = "UNMATCHED"
    reconciliation_notes: Optional[str] = None

    class Config:
        from_attributes = True


class PaymentListResponse(BaseModel):
    items: List[PaymentResponse]
    total: int
    page: int
    page_size: int
    total_pages: int
    total_amount_collected: float = 0.0

