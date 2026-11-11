"""
Pydantic Schemas for AI Invoice Auditing, Financial Verification, and Human Review Governance.
"""

from datetime import datetime
from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field
from app.models.models import AuditSeverity, AuditRecommendation, AuditReviewStatus


class AuditFinding(BaseModel):
    finding_id: str = Field(..., description="Unique finding code, e.g. ERR-MATH-001")
    category: str = Field(..., description="Category: ARITHMETIC, TAX, CUSTOMER, DATE, CURRENCY, DUPLICATE, ANOMALY")
    severity: AuditSeverity = Field(..., description="Severity level: INFO, LOW, MEDIUM, HIGH, CRITICAL")
    title: str = Field(..., description="Short finding title")
    description: str = Field(..., description="Detailed description of the finding")
    evidence: str = Field(..., description="Calculated vs stated evidence numbers or rule triggered")
    recommendation: str = Field(..., description="Suggested correction or next step")
    field: Optional[str] = Field(None, description="Affected invoice field name if applicable")
    is_ai_generated: bool = Field(default=False, description="Whether finding originated from AI explainer layer")
    status: str = Field(default="OPEN", description="Finding status: OPEN, RESOLVED, REJECTED")


class FinancialDiscrepancy(BaseModel):
    subtotal_diff: float = Field(0.0, description="Stated subtotal minus calculated subtotal")
    tax_diff: float = Field(0.0, description="Stated tax minus calculated tax")
    total_diff: float = Field(0.0, description="Stated grand total minus calculated grand total")
    has_math_error: bool = Field(False, description="True if any discrepancy exceeds 0.01")


class AuditCalculationBreakdown(BaseModel):
    calculated_subtotal: float
    calculated_tax: float
    calculated_total: float
    stated_subtotal: float
    stated_tax: float
    stated_total: float
    currency: str
    currency_decimals: int
    discrepancy: FinancialDiscrepancy


class AIAuditExplanation(BaseModel):
    summary: str = Field(..., description="Executive summary of the audit findings")
    findings: List[AuditFinding] = Field(default_factory=list, description="Categorized findings explained by AI")
    overall_recommendation: AuditRecommendation = Field(..., description="APPROVE, REVIEW, or BLOCK")


class AuditReviewRequest(BaseModel):
    action: str = Field(..., description="Review action: APPROVE, REJECT, RESOLVE")
    finding_id: Optional[str] = Field(None, description="Optional specific finding ID to resolve/reject")
    notes: Optional[str] = Field(None, description="Reviewer commentary or resolution rationale")
    reviewer_name: Optional[str] = Field(default="Compliance Officer", description="Auditor name or identity")


class AuditResultResponse(BaseModel):
    id: int
    invoice_id: int
    invoice_number: Optional[str] = None
    customer_name: Optional[str] = None
    is_valid: bool
    overall_recommendation: AuditRecommendation
    max_severity: AuditSeverity
    review_status: AuditReviewStatus
    reviewer_decision: Optional[str] = None
    resolution_notes: Optional[str] = None
    reviewed_at: Optional[datetime] = None
    
    calculated_subtotal: Optional[float] = None
    calculated_tax: Optional[float] = None
    calculated_total: Optional[float] = None
    stated_subtotal: Optional[float] = None
    stated_tax: Optional[float] = None
    stated_total: Optional[float] = None
    
    issues: List[AuditFinding] = []
    passed_checks: List[str] = []
    raw_evidence: Dict[str, Any] = {}
    
    ai_summary: Optional[str] = None
    ai_findings: List[AuditFinding] = []
    
    audited_at: datetime

    model_config = {"from_attributes": True}


class AuditListResponse(BaseModel):
    items: List[AuditResultResponse]
    total: int
    page: int
    page_size: int
    total_pages: int


class AuditStatsResponse(BaseModel):
    total_audits: int
    passed_audits: int
    review_required: int
    blocked_critical: int
    clean_pass_rate: float

