"""
Pydantic Schemas for Invoice Management & Calculations in InvoicePilot AI.
"""

from datetime import datetime
from typing import List, Optional
from decimal import Decimal, ROUND_HALF_UP
from pydantic import BaseModel, Field, EmailStr, field_validator
from app.models.models import InvoiceStatus


def round_currency(val: Decimal) -> Decimal:
    """Rounds a Decimal value to 2 decimal places using standard financial bankers rounding."""
    return val.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


class InvoiceItemBase(BaseModel):
    description: str = Field(..., min_length=1, max_length=500, description="Line item description")
    quantity: Decimal = Field(default=Decimal("1.0"), gt=0, description="Quantity of units")
    unit_price: Decimal = Field(default=Decimal("0.0"), ge=0, description="Unit price per item")
    tax_rate: Decimal = Field(default=Decimal("0.0"), ge=0, le=100, description="Tax percentage rate (0-100)")


class InvoiceItemCreate(InvoiceItemBase):
    pass


class InvoiceItemUpdate(BaseModel):
    id: Optional[int] = None
    description: Optional[str] = Field(None, min_length=1, max_length=500)
    quantity: Optional[Decimal] = Field(None, gt=0)
    unit_price: Optional[Decimal] = Field(None, ge=0)
    tax_rate: Optional[Decimal] = Field(None, ge=0, le=100)


class InvoiceItemResponse(BaseModel):
    id: int
    invoice_id: int
    description: str
    quantity: float
    unit_price: float
    tax_rate: float
    tax_amount: float
    total: float

    model_config = {"from_attributes": True}


class CustomerEmbedded(BaseModel):
    id: Optional[int] = None
    name: str
    email: str
    phone: Optional[str] = None
    address: Optional[str] = None

    model_config = {"from_attributes": True}


class InvoiceCreate(BaseModel):
    invoice_number: str = Field(..., min_length=1, max_length=100, description="Unique invoice identifier")
    customer_name: str = Field(..., min_length=1, max_length=255, description="Client or company name")
    customer_email: EmailStr = Field(..., description="Client billing email address")
    customer_phone: Optional[str] = Field(None, max_length=50)
    customer_address: Optional[str] = None
    currency: str = Field(default="USD", min_length=3, max_length=3, description="ISO 4217 Currency code")
    issue_date: Optional[datetime] = None
    due_date: Optional[datetime] = None
    notes: Optional[str] = None
    items: List[InvoiceItemCreate] = Field(default_factory=list, description="Itemized billing breakdown")
    document_upload_id: Optional[str] = Field(None, description="Linked uploaded document ID if generated via OCR/AI")

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, v: str) -> str:
        return v.upper().strip()


class InvoiceUpdate(BaseModel):
    customer_name: Optional[str] = Field(None, min_length=1, max_length=255)
    customer_email: Optional[EmailStr] = None
    customer_phone: Optional[str] = None
    customer_address: Optional[str] = None
    currency: Optional[str] = Field(None, min_length=3, max_length=3)
    due_date: Optional[datetime] = None
    status: Optional[InvoiceStatus] = None
    notes: Optional[str] = None
    items: Optional[List[InvoiceItemUpdate]] = None


class InvoiceResponse(BaseModel):
    id: int
    invoice_number: str
    business_id: Optional[int] = None
    customer_id: Optional[int] = None
    customer_name: Optional[str] = None
    customer_email: Optional[str] = None
    customer: Optional[CustomerEmbedded] = None
    currency: str
    subtotal: float
    tax_total: float
    total_amount: float
    issue_date: datetime
    due_date: Optional[datetime]
    status: InvoiceStatus
    approved_by_user_id: Optional[int] = None
    approved_by_name: Optional[str] = None
    sent_by_user_id: Optional[int] = None
    sent_by_name: Optional[str] = None
    paypal_invoice_id: Optional[str] = None
    paypal_invoice_url: Optional[str] = None
    paypal_status: Optional[str] = None
    paypal_last_checked_at: Optional[datetime] = None
    paypal_draft_created_at: Optional[datetime] = None
    paypal_sent_at: Optional[datetime] = None
    paypal_error_detail: Optional[str] = None
    notes: Optional[str]
    is_demo: bool
    document_path: Optional[str]
    items: List[InvoiceItemResponse] = []
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class InvoiceListResponse(BaseModel):
    items: List[InvoiceResponse]
    total: int
    page: int
    page_size: int
    total_pages: int

