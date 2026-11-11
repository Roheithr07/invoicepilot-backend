"""
Audit Router for InvoicePilot AI.
Endpoints for running audits, inspecting calculation evidence, reviewing findings, and audit history.
"""

from datetime import datetime, timezone
from typing import Optional, List
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session
from sqlalchemy import desc

from app.database import get_db
from app.models.models import (
    Invoice, AuditResult, ActivityLog,
    AuditSeverity, AuditRecommendation, AuditReviewStatus
)
from app.schemas.audit import (
    AuditResultResponse, AuditListResponse, AuditReviewRequest,
    AuditStatsResponse, AuditFinding
)
from app.services.invoice_auditor import InvoiceAuditor
from app.services.ai_audit_explainer import AIAuditExplainer

router = APIRouter(tags=["AI Invoice Auditor"])


def build_audit_response(audit: AuditResult, invoice: Optional[Invoice] = None) -> AuditResultResponse:
    inv = invoice or audit.invoice
    inv_num = inv.invoice_number if inv else None
    c_name = inv.customer_name if inv else None

    # Parse issues into AuditFinding models
    raw_issues = audit.issues or []
    parsed_issues = []
    for item in raw_issues:
        if isinstance(item, dict):
            try:
                parsed_issues.append(AuditFinding(**item))
            except Exception:
                pass

    raw_ai_findings = audit.ai_findings or []
    parsed_ai_findings = []
    for item in raw_ai_findings:
        if isinstance(item, dict):
            try:
                parsed_ai_findings.append(AuditFinding(**item))
            except Exception:
                pass

    return AuditResultResponse(
        id=audit.id,
        invoice_id=audit.invoice_id,
        invoice_number=inv_num,
        customer_name=c_name,
        is_valid=audit.is_valid,
        overall_recommendation=AuditRecommendation(audit.overall_recommendation or "REVIEW"),
        max_severity=AuditSeverity(audit.max_severity or "INFO"),
        review_status=AuditReviewStatus(audit.review_status or "PENDING"),
        reviewer_decision=audit.reviewer_decision,
        resolution_notes=audit.resolution_notes,
        reviewed_at=audit.reviewed_at,
        calculated_subtotal=audit.calculated_subtotal,
        calculated_tax=audit.calculated_tax,
        calculated_total=audit.calculated_total,
        stated_subtotal=audit.stated_subtotal,
        stated_tax=audit.stated_tax,
        stated_total=audit.stated_total,
        issues=parsed_issues,
        passed_checks=audit.passed_checks or [],
        raw_evidence=audit.raw_evidence or {},
        ai_summary=audit.ai_summary,
        ai_findings=parsed_ai_findings,
        audited_at=audit.audited_at
    )


@router.post("/invoices/{invoice_id}/audit", response_model=AuditResultResponse, status_code=status.HTTP_201_CREATED)
def run_invoice_audit(invoice_id: int, db: Session = Depends(get_db)):
    """
    Executes a comprehensive financial audit on an invoice.
    1. Layer 1: Deterministic Python Decimal arithmetic and compliance rules.
    2. Layer 2: AI-assisted analysis and executive explanation.
    3. Persists audit snapshot into audit_results table preserving history.
    """
    invoice = db.query(Invoice).filter(Invoice.id == invoice_id, Invoice.is_deleted == False).first()
    if not invoice:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Invoice with ID {invoice_id} not found."
        )

    # 1. Deterministic Validation
    is_valid, findings, passed_checks, breakdown, evidence = InvoiceAuditor.audit(db, invoice)

    # 2. AI Explainer Layer
    ai_explanation = AIAuditExplainer.explain(
        invoice=invoice,
        deterministic_findings=findings,
        breakdown=breakdown,
        evidence=evidence,
        raw_document_text=invoice.raw_extracted_text
    )

    # Determine Maximum Severity
    sev_rank = {
        AuditSeverity.CRITICAL: 5,
        AuditSeverity.HIGH: 4,
        AuditSeverity.MEDIUM: 3,
        AuditSeverity.LOW: 2,
        AuditSeverity.INFO: 1
    }
    max_sev = AuditSeverity.INFO
    if findings:
        max_sev = max(findings, key=lambda f: sev_rank.get(f.severity, 0)).severity

    # Convert findings to JSON serializable dicts
    issues_json = [f.model_dump() for f in findings]
    ai_findings_json = [f.model_dump() for f in ai_explanation.findings]

    # 3. Persist Audit Result Record
    audit_record = AuditResult(
        invoice_id=invoice.id,
        is_valid=is_valid,
        issues=issues_json,
        passed_checks=passed_checks,
        calculated_subtotal=breakdown.calculated_subtotal,
        calculated_tax=breakdown.calculated_tax,
        calculated_total=breakdown.calculated_total,
        stated_subtotal=breakdown.stated_subtotal,
        stated_tax=breakdown.stated_tax,
        stated_total=breakdown.stated_total,
        raw_evidence=evidence,
        overall_recommendation=ai_explanation.overall_recommendation.value,
        max_severity=max_sev.value,
        review_status=AuditReviewStatus.PENDING.value,
        ai_summary=ai_explanation.summary,
        ai_findings=ai_findings_json,
        audited_at=datetime.now(timezone.utc)
    )
    db.add(audit_record)

    # Log Activity
    log = ActivityLog(
        action="INVOICE_AUDITED",
        entity_type="invoice",
        entity_id=str(invoice.id),
        description=(
            f"Audited invoice {invoice.invoice_number}: "
            f"Recommendation={ai_explanation.overall_recommendation.value}, "
            f"Valid={is_valid}, {len(findings)} issue(s) detected."
        )
    )
    db.add(log)
    db.commit()
    db.refresh(audit_record)

    return build_audit_response(audit_record, invoice)


@router.get("/invoices/{invoice_id}/audit", response_model=List[AuditResultResponse])
def get_invoice_audit_history(invoice_id: int, db: Session = Depends(get_db)):
    """
    Retrieves the complete audit history for a specific invoice, latest first.
    """
    invoice = db.query(Invoice).filter(Invoice.id == invoice_id).first()
    if not invoice:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Invoice with ID {invoice_id} not found."
        )

    audits = db.query(AuditResult).filter(
        AuditResult.invoice_id == invoice_id
    ).order_by(desc(AuditResult.audited_at)).all()

    return [build_audit_response(a, invoice) for a in audits]


@router.get("/audits", response_model=AuditListResponse)
def list_audits(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    recommendation: Optional[str] = Query(None, description="APPROVE, REVIEW, BLOCK"),
    severity: Optional[str] = Query(None, description="CRITICAL, HIGH, MEDIUM, LOW, INFO"),
    review_status: Optional[str] = Query(None, description="PENDING, APPROVED, REJECTED, RESOLVED"),
    db: Session = Depends(get_db)
):
    """
    Lists all audit records with pagination and filtering across all invoices.
    """
    query = db.query(AuditResult)

    if recommendation:
        query = query.filter(AuditResult.overall_recommendation == recommendation.upper().strip())
    if severity:
        query = query.filter(AuditResult.max_severity == severity.upper().strip())
    if review_status:
        query = query.filter(AuditResult.review_status == review_status.upper().strip())

    total = query.count()
    offset = (page - 1) * page_size
    records = query.order_by(desc(AuditResult.audited_at)).offset(offset).limit(page_size).all()

    items = [build_audit_response(r) for r in records]
    total_pages = (total + page_size - 1) // page_size if total > 0 else 1

    return AuditListResponse(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
        total_pages=total_pages
    )


@router.get("/audits/stats", response_model=AuditStatsResponse)
def get_audit_statistics(db: Session = Depends(get_db)):
    """
    Returns aggregate summary metrics for the AI Auditor dashboard.
    """
    total = db.query(AuditResult).count()
    if total == 0:
        return AuditStatsResponse(
            total_audits=0,
            passed_audits=0,
            review_required=0,
            blocked_critical=0,
            clean_pass_rate=100.0
        )

    passed = db.query(AuditResult).filter(AuditResult.overall_recommendation == "APPROVE").count()
    review = db.query(AuditResult).filter(AuditResult.overall_recommendation == "REVIEW").count()
    blocked = db.query(AuditResult).filter(AuditResult.overall_recommendation == "BLOCK").count()
    clean_rate = round((passed / total) * 100.0, 1)

    return AuditStatsResponse(
        total_audits=total,
        passed_audits=passed,
        review_required=review,
        blocked_critical=blocked,
        clean_pass_rate=clean_rate
    )


@router.get("/audits/{audit_id}", response_model=AuditResultResponse)
def get_audit_by_id(audit_id: int, db: Session = Depends(get_db)):
    """
    Retrieves full details of a specific audit evaluation.
    """
    audit = db.query(AuditResult).filter(AuditResult.id == audit_id).first()
    if not audit:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Audit record with ID {audit_id} not found."
        )
    return build_audit_response(audit)


@router.post("/audits/{audit_id}/review", response_model=AuditResultResponse)
def review_audit(
    audit_id: int,
    payload: AuditReviewRequest,
    db: Session = Depends(get_db)
):
    """
    Layer 3 Human Governance: Submit reviewer decision on an audit or individual finding.
    Supports: APPROVE, REJECT, RESOLVE with notes and human decision persistence.
    """
    audit = db.query(AuditResult).filter(AuditResult.id == audit_id).first()
    if not audit:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Audit record with ID {audit_id} not found."
        )

    action_norm = payload.action.upper().strip()
    status_map = {
        "APPROVE": "APPROVED",
        "APPROVED": "APPROVED",
        "REJECT": "REJECTED",
        "REJECTED": "REJECTED",
        "RESOLVE": "RESOLVED",
        "RESOLVED": "RESOLVED"
    }
    target_status = status_map.get(action_norm)
    if not target_status:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid action '{payload.action}'. Supported: APPROVE, REJECT, RESOLVE."
        )

    # If reviewing a specific individual finding
    if payload.finding_id:
        updated_issues = []
        found = False
        for f in (audit.issues or []):
            if isinstance(f, dict) and f.get("finding_id") == payload.finding_id:
                f["status"] = "RESOLVED" if target_status in ["APPROVED", "RESOLVED"] else "REJECTED"
                found = True
            updated_issues.append(f)
        audit.issues = updated_issues

        # Check if any remaining findings are open
        still_open = any(f.get("status") == "OPEN" for f in updated_issues if isinstance(f, dict))
        if not still_open:
            audit.review_status = "RESOLVED" if target_status in ["APPROVED", "RESOLVED"] else "REJECTED"
    else:
        # Whole audit level decision
        audit.review_status = target_status
        audit.reviewer_decision = f"{target_status} by {payload.reviewer_name}"

    audit.resolution_notes = payload.notes
    audit.reviewed_at = datetime.now(timezone.utc)

    # Log to Activity Log
    log = ActivityLog(
        action=f"AUDIT_{action_norm}",
        entity_type="audit",
        entity_id=str(audit.id),
        description=(
            f"Audit #{audit.id} for invoice #{audit.invoice_id} "
            f"reviewed with action '{action_norm}' by {payload.reviewer_name}."
        )
    )
    db.add(log)
    db.commit()
    db.refresh(audit)

    return build_audit_response(audit)
