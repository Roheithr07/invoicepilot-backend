"""
Pydantic Schemas for AI Smart Follow-ups & Reminders.
"""

from datetime import datetime
from typing import Optional, List
from pydantic import BaseModel, Field


class ReminderResponse(BaseModel):
    id: int
    invoice_id: int
    invoice_number: str
    customer_name: Optional[str] = None
    customer_email: str
    amount: float
    currency: str
    due_date: Optional[str] = None
    days_overdue: int = 0
    reminder_type: str
    priority_level: str = "MEDIUM"
    priority_reason: Optional[str] = None
    recommended_send_date: Optional[datetime] = None
    tone: str = "FRIENDLY"
    subject: str
    body: str
    status: str = "PENDING_APPROVAL"
    dispatch_method: Optional[str] = None
    dispatch_notes: Optional[str] = None
    is_approved: bool = False
    approved_at: Optional[datetime] = None
    sent_at: Optional[datetime] = None
    created_at: datetime

    class Config:
        from_attributes = True


class ReminderUpdateInput(BaseModel):
    subject: Optional[str] = None
    body: Optional[str] = None
    tone: Optional[str] = None


class ReminderApprovalInput(BaseModel):
    approver_name: Optional[str] = Field("Authorized Manager", description="Name of the user granting approval")


class ReminderDispatchInput(BaseModel):
    channel: str = Field("auto", description="'auto', 'paypal_reminder', or 'manual_copy'")

