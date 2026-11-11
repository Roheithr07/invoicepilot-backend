"""
SQLAlchemy Database Models for InvoicePilot AI.
"""

from datetime import datetime, timezone
from typing import Optional
import enum
from sqlalchemy import (
    Column, Integer, String, Float, DateTime, ForeignKey, 
    Text, Boolean, Enum as SQLEnum, JSON
)
from sqlalchemy.orm import relationship
from app.database import Base


def utc_now():
    return datetime.now(timezone.utc)


class InvoiceStatus(str, enum.Enum):
    DRAFT = "DRAFT"
    SENT = "SENT"
    UNPAID = "UNPAID"
    PAID = "PAID"
    OVERDUE = "OVERDUE"
    CANCELLED = "CANCELLED"


class PaymentStatus(str, enum.Enum):
    PENDING = "PENDING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    REFUNDED = "REFUNDED"
    PARTIALLY_PAID = "PARTIALLY_PAID"
    CANCELLED = "CANCELLED"
    REQUIRES_REVIEW = "REQUIRES_REVIEW"


class ReconciliationCategory(str, enum.Enum):
    MATCHED = "MATCHED"
    PARTIALLY_MATCHED = "PARTIALLY_MATCHED"
    UNMATCHED = "UNMATCHED"
    DUPLICATE = "DUPLICATE"
    AMOUNT_MISMATCH = "AMOUNT_MISMATCH"
    CURRENCY_MISMATCH = "CURRENCY_MISMATCH"
    REQUIRES_REVIEW = "REQUIRES_REVIEW"


class AuditSeverity(str, enum.Enum):
    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    INFO = "INFO"


class AuditRecommendation(str, enum.Enum):
    APPROVE = "APPROVE"
    REVIEW = "REVIEW"
    BLOCK = "BLOCK"


class AuditReviewStatus(str, enum.Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    RESOLVED = "RESOLVED"


class UserRole(str, enum.Enum):
    SUPER_ADMIN = "SUPER_ADMIN"
    BUSINESS_OWNER = "BUSINESS_OWNER"
    ACCOUNTANT = "ACCOUNTANT"
    STAFF = "STAFF"


class Business(Base):
    __tablename__ = "businesses"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False, index=True)
    registration_number = Column(String(100), nullable=True)
    currency = Column(String(10), default="USD")
    created_at = Column(DateTime(timezone=True), default=utc_now)
    updated_at = Column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)

    users = relationship("User", back_populates="business")
    customers = relationship("Customer", back_populates="business")
    invoices = relationship("Invoice", back_populates="business")


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String(255), unique=True, index=True, nullable=False)
    name = Column(String(255), nullable=False)
    hashed_password = Column(String(255), nullable=True)  # nullable for initial legacy compatibility
    role = Column(SQLEnum(UserRole), default=UserRole.STAFF, nullable=False, index=True)
    business_id = Column(Integer, ForeignKey("businesses.id"), nullable=True, index=True)
    company_name = Column(String(255), nullable=True)
    paypal_merchant_id = Column(String(255), nullable=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), default=utc_now)
    updated_at = Column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)

    business = relationship("Business", back_populates="users")
    invoices = relationship("Invoice", back_populates="user", cascade="all, delete-orphan", foreign_keys="[Invoice.user_id]")


class Customer(Base):
    __tablename__ = "customers"

    id = Column(Integer, primary_key=True, index=True)
    business_id = Column(Integer, ForeignKey("businesses.id"), nullable=True, index=True)
    name = Column(String(255), nullable=False, index=True)
    email = Column(String(255), nullable=False, index=True)
    phone = Column(String(50), nullable=True)
    address = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=utc_now)
    updated_at = Column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)

    business = relationship("Business", back_populates="customers")
    invoices = relationship("Invoice", back_populates="customer")


class Invoice(Base):
    __tablename__ = "invoices"

    id = Column(Integer, primary_key=True, index=True)
    invoice_number = Column(String(100), unique=True, index=True, nullable=False)
    business_id = Column(Integer, ForeignKey("businesses.id"), nullable=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    customer_id = Column(Integer, ForeignKey("customers.id"), nullable=True, index=True)
    
    # Financials
    currency = Column(String(10), default="USD", nullable=False)
    subtotal = Column(Float, default=0.0, nullable=False)
    tax_total = Column(Float, default=0.0, nullable=False)
    total_amount = Column(Float, default=0.0, nullable=False)
    
    # Dates
    issue_date = Column(DateTime(timezone=True), default=utc_now, nullable=False)
    due_date = Column(DateTime(timezone=True), nullable=True)
    
    # Status & PayPal reference
    status = Column(SQLEnum(InvoiceStatus), default=InvoiceStatus.DRAFT, nullable=False, index=True)
    paypal_invoice_id = Column(String(255), unique=True, nullable=True, index=True)
    paypal_invoice_url = Column(String(1024), nullable=True)
    paypal_status = Column(String(50), nullable=True)
    paypal_last_checked_at = Column(DateTime(timezone=True), nullable=True)
    paypal_draft_created_at = Column(DateTime(timezone=True), nullable=True)
    paypal_sent_at = Column(DateTime(timezone=True), nullable=True)
    paypal_error_detail = Column(Text, nullable=True)
    
    notes = Column(Text, nullable=True)
    is_demo = Column(Boolean, default=False)
    is_deleted = Column(Boolean, default=False, index=True)
    document_path = Column(String(500), nullable=True)
    raw_extracted_text = Column(Text, nullable=True)
    
    # Governance & Approval Attribution
    approved_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    approved_by_name = Column(String(255), nullable=True)
    sent_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    sent_by_name = Column(String(255), nullable=True)
    
    created_at = Column(DateTime(timezone=True), default=utc_now)
    updated_at = Column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)

    # Relationships
    user = relationship("User", back_populates="invoices", foreign_keys=[user_id])
    business = relationship("Business", back_populates="invoices")
    customer = relationship("Customer", back_populates="invoices")
    items = relationship("InvoiceItem", back_populates="invoice", cascade="all, delete-orphan")
    payments = relationship("Payment", back_populates="invoice")
    audit_results = relationship("AuditResult", back_populates="invoice", cascade="all, delete-orphan")
    reminders = relationship("ReminderRecord", back_populates="invoice", cascade="all, delete-orphan")
    reconciliations = relationship("ReconciliationRecord", back_populates="invoice")

    @property
    def customer_name(self) -> Optional[str]:
        return self.customer.name if self.customer else None

    @customer_name.setter
    def customer_name(self, value: Optional[str]):
        if not self.customer:
            self.customer = Customer(name=value or "", email="")
        else:
            self.customer.name = value or ""

    @property
    def customer_email(self) -> Optional[str]:
        return self.customer.email if self.customer else None

    @customer_email.setter
    def customer_email(self, value: Optional[str]):
        if not self.customer:
            self.customer = Customer(name="", email=value or "")
        else:
            self.customer.email = value or ""


class InvoiceItem(Base):
    __tablename__ = "invoice_items"

    id = Column(Integer, primary_key=True, index=True)
    invoice_id = Column(Integer, ForeignKey("invoices.id"), nullable=False, index=True)
    description = Column(String(500), nullable=False)
    quantity = Column(Float, default=1.0, nullable=False)
    unit_price = Column(Float, default=0.0, nullable=False)
    tax_rate = Column(Float, default=0.0)
    tax_amount = Column(Float, default=0.0)
    total = Column(Float, default=0.0, nullable=False)

    invoice = relationship("Invoice", back_populates="items")


class Payment(Base):
    __tablename__ = "payments"

    id = Column(Integer, primary_key=True, index=True)
    invoice_id = Column(Integer, ForeignKey("invoices.id"), nullable=False, index=True)
    paypal_payment_id = Column(String(255), unique=True, nullable=True, index=True)
    paypal_capture_id = Column(String(255), nullable=True, index=True)
    amount = Column(Float, nullable=False)
    currency = Column(String(10), default="USD", nullable=False)
    fee_amount = Column(Float, default=0.0)
    net_amount = Column(Float, default=0.0)
    status = Column(SQLEnum(PaymentStatus), default=PaymentStatus.PENDING, nullable=False)
    payment_method = Column(String(100), default="PAYPAL")
    payer_email = Column(String(255), nullable=True)
    payer_name = Column(String(255), nullable=True)
    payment_date = Column(DateTime(timezone=True), default=utc_now)
    
    # Phase 6 Reconciliation Fields
    is_reconciled = Column(Boolean, default=False)
    reconciled_at = Column(DateTime(timezone=True), nullable=True)
    reconciliation_status = Column(String(50), default="UNMATCHED", index=True)
    reconciliation_notes = Column(Text, nullable=True)
    notes = Column(Text, nullable=True)

    invoice = relationship("Invoice", back_populates="payments")
    reconciliations = relationship("ReconciliationRecord", back_populates="payment")


class AuditResult(Base):
    __tablename__ = "audit_results"

    id = Column(Integer, primary_key=True, index=True)
    invoice_id = Column(Integer, ForeignKey("invoices.id"), nullable=False, index=True)
    is_valid = Column(Boolean, nullable=False)
    issues = Column(JSON, default=list)  # List of detected errors/warnings
    passed_checks = Column(JSON, default=list)
    calculated_subtotal = Column(Float, nullable=True)
    calculated_tax = Column(Float, nullable=True)
    calculated_total = Column(Float, nullable=True)
    stated_subtotal = Column(Float, nullable=True)
    stated_tax = Column(Float, nullable=True)
    stated_total = Column(Float, nullable=True)
    raw_evidence = Column(JSON, default=dict)
    
    # Phase 4 Audit Governance & AI Explanations
    overall_recommendation = Column(String(20), default="REVIEW", nullable=False)
    max_severity = Column(String(20), default="INFO", nullable=False)
    review_status = Column(String(20), default="PENDING", nullable=False)
    reviewer_decision = Column(String(50), nullable=True)
    resolution_notes = Column(Text, nullable=True)
    reviewed_at = Column(DateTime(timezone=True), nullable=True)
    ai_summary = Column(Text, nullable=True)
    ai_findings = Column(JSON, default=list)
    
    audited_at = Column(DateTime(timezone=True), default=utc_now)

    invoice = relationship("Invoice", back_populates="audit_results")


class ReminderRecord(Base):
    __tablename__ = "reminder_records"

    id = Column(Integer, primary_key=True, index=True)
    invoice_id = Column(Integer, ForeignKey("invoices.id"), nullable=False, index=True)
    recipient_email = Column(String(255), nullable=False)
    subject = Column(String(500), nullable=False)
    body = Column(Text, nullable=False)
    reminder_type = Column(String(50), default="DUE_SOON")  # DUE_SOON, OVERDUE, URGENT
    
    # Phase 6 Follow-up Prioritization & Governance Fields
    priority_level = Column(String(50), default="MEDIUM")  # URGENT, HIGH, MEDIUM, LOW
    priority_reason = Column(Text, nullable=True)
    recommended_send_date = Column(DateTime(timezone=True), nullable=True)
    tone = Column(String(50), default="FRIENDLY")  # FRIENDLY, POLITE, FIRM, URGENT
    status = Column(String(50), default="PENDING_APPROVAL", index=True)  # PENDING_APPROVAL, APPROVED, DISPATCHED, REJECTED
    dispatch_method = Column(String(50), default="MANUAL_COPY")  # MANUAL_COPY, PAYPAL_REMINDER, EMAIL
    dispatch_notes = Column(Text, nullable=True)
    approved_at = Column(DateTime(timezone=True), nullable=True)
    approved_by = Column(String(255), nullable=True)

    is_approved = Column(Boolean, default=False)
    sent_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=utc_now)

    invoice = relationship("Invoice", back_populates="reminders")


class WebhookEvent(Base):
    __tablename__ = "webhook_events"

    id = Column(Integer, primary_key=True, index=True)
    event_id = Column(String(255), unique=True, index=True, nullable=False)
    event_type = Column(String(255), index=True, nullable=False)
    resource_type = Column(String(100), nullable=True)
    is_verified = Column(Boolean, default=False)
    payload = Column(JSON, nullable=False)
    received_at = Column(DateTime(timezone=True), default=utc_now)
    processed_at = Column(DateTime(timezone=True), nullable=True)
    
    # Phase 6 Reliability & Idempotency
    processing_status = Column(String(50), default="PENDING", index=True)  # PENDING, PROCESSED, FAILED, IGNORED
    error_detail = Column(Text, nullable=True)
    retry_count = Column(Integer, default=0)


class ReconciliationRecord(Base):
    __tablename__ = "reconciliation_records"

    id = Column(Integer, primary_key=True, index=True)
    payment_id = Column(Integer, ForeignKey("payments.id"), nullable=True, index=True)
    invoice_id = Column(Integer, ForeignKey("invoices.id"), nullable=True, index=True)
    paypal_reference = Column(String(255), nullable=True, index=True)
    category = Column(String(50), nullable=False, index=True)  # MATCHED, PARTIALLY_MATCHED, UNMATCHED, DUPLICATE, AMOUNT_MISMATCH, CURRENCY_MISMATCH, REQUIRES_REVIEW
    confidence_score = Column(Float, default=1.0)
    match_reasons = Column(JSON, default=list)
    discrepancies = Column(JSON, default=list)
    status = Column(String(50), default="AUTO_RECONCILED", index=True)  # AUTO_RECONCILED, PENDING_REVIEW, RESOLVED, REJECTED
    reviewer_decision = Column(String(50), nullable=True)
    reviewer_notes = Column(Text, nullable=True)
    reviewed_at = Column(DateTime(timezone=True), nullable=True)
    reviewed_by = Column(String(255), nullable=True)
    created_at = Column(DateTime(timezone=True), default=utc_now)

    payment = relationship("Payment", back_populates="reconciliations")
    invoice = relationship("Invoice", back_populates="reconciliations")


class DocumentUpload(Base):
    __tablename__ = "document_uploads"

    id = Column(Integer, primary_key=True, index=True)
    upload_id = Column(String(64), unique=True, index=True, nullable=False)
    original_filename = Column(String(255), nullable=False)
    stored_filename = Column(String(255), nullable=False)
    file_path = Column(String(500), nullable=False)
    file_size = Column(Integer, nullable=False)
    content_type = Column(String(100), nullable=False)
    extracted_text = Column(Text, nullable=True)
    extraction_status = Column(String(50), default="PENDING", index=True)  # PENDING, COMPLETED, FAILED
    extraction_data = Column(JSON, nullable=True)
    error_message = Column(Text, nullable=True)
    invoice_id = Column(Integer, ForeignKey("invoices.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), default=utc_now)


class ActivityLog(Base):
    __tablename__ = "activity_logs"

    id = Column(Integer, primary_key=True, index=True)
    action = Column(String(100), nullable=False, index=True)
    entity_type = Column(String(50), nullable=False)  # invoice, payment, ai, paypal
    entity_id = Column(String(100), nullable=True)
    description = Column(Text, nullable=False)
    metadata_json = Column(JSON, default=dict)
    created_at = Column(DateTime(timezone=True), default=utc_now)

