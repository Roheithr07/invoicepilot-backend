"""
Invoice & Document Extraction API Router for InvoicePilot AI.
"""

import os
import asyncio
from typing import Optional
from datetime import datetime
from math import ceil
from fastapi import APIRouter, Depends, UploadFile, File, Form, Query, status, HTTPException, Response
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.models import DocumentUpload, User, UserRole, ActivityLog, InvoiceStatus
from app.schemas.invoice import (
    InvoiceCreate, InvoiceUpdate, InvoiceResponse, InvoiceListResponse
)
from app.schemas.extraction import (
    DocumentUploadResponse, ExtractionRequest, ExtractionResponse, ExtractedInvoiceData
)
from app.services.invoice_service import InvoiceService
from app.services.document_extractor import DocumentExtractor, DocumentExtractionError
from app.services.ai_extractor import AIExtractor
from app.services.storage import storage_service, StorageError
from app.services.websocket_manager import ws_manager
from app.auth.dependencies import (
    get_current_user, get_current_active_user, require_permission,
    verify_business_ownership
)

router = APIRouter(prefix="/invoices", tags=["Invoices & AI Extraction"])


# ==============================================================================
# 1. Invoice CRUD Endpoints
# ==============================================================================

@router.post("", response_model=InvoiceResponse, status_code=status.HTTP_201_CREATED)
def create_invoice(
    payload: InvoiceCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Creates a new invoice record with backend-calculated totals and duplicate prevention.
    Enforces 'invoices.create' permission and binds record to authenticated user's business.
    """
    if current_user.role not in (UserRole.SUPER_ADMIN, UserRole.BUSINESS_OWNER, UserRole.STAFF):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Access Denied: Role '{current_user.role.value}' cannot create invoices."
        )

    b_id = current_user.business_id or 1
    invoice = InvoiceService.create_invoice(db, payload, business_id=b_id, user_id=current_user.id)
    
    # Broadcast real-time event
    try:
        ws_manager.broadcast_event(
            "invoice.created",
            {
                "invoice_id": invoice.id,
                "invoice_number": invoice.invoice_number,
                "customer_name": invoice.customer_name,
                "total_amount": invoice.total_amount,
                "currency": invoice.currency,
                "status": invoice.status.value
            },
            business_id=b_id
        )
    except Exception:
        pass

    return invoice


@router.get("", response_model=InvoiceListResponse, status_code=status.HTTP_200_OK)
def list_invoices(
    page: int = Query(default=1, ge=1, description="Page number"),
    page_size: int = Query(default=20, ge=1, le=100, description="Items per page"),
    status: Optional[str] = Query(default=None, description="Status filter (e.g. DRAFT, SENT, PAID, OVERDUE)"),
    search: Optional[str] = Query(default=None, description="Search term for invoice #, customer name or email"),
    customer_id: Optional[int] = Query(default=None, description="Filter by customer ID"),
    sort_by: str = Query(default="due_date", description="Field to sort by (due_date, issue_date, total_amount, invoice_number)"),
    sort_asc: bool = Query(default=False, description="Ascending sort order"),
    date_from: Optional[datetime] = Query(default=None, description="Filter from issue date"),
    date_to: Optional[datetime] = Query(default=None, description="Filter to issue date"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Lists invoices with search, status filtering, date ranges, sorting, and pagination.
    Applies multi-tenant isolation so users only see their own business invoices.
    """
    b_id = None if current_user.role == UserRole.SUPER_ADMIN else current_user.business_id

    items, total = InvoiceService.get_invoices(
        db=db,
        page=page,
        page_size=page_size,
        status_filter=status,
        search=search,
        customer_id=customer_id,
        sort_by=sort_by,
        sort_asc=sort_asc,
        date_from=date_from,
        date_to=date_to,
        business_id=b_id
    )
    total_pages = ceil(total / page_size) if total > 0 else 1

    return InvoiceListResponse(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
        total_pages=total_pages
    )


@router.get("/{invoice_id}", response_model=InvoiceResponse, status_code=status.HTTP_200_OK)
def get_invoice(
    invoice_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Retrieves full invoice details by ID with tenant boundary validation.
    """
    invoice = InvoiceService.get_invoice_by_id(db, invoice_id)
    verify_business_ownership(invoice, current_user, "Invoice")
    return invoice


@router.put("/{invoice_id}", response_model=InvoiceResponse, status_code=status.HTTP_200_OK)
def update_invoice(
    invoice_id: int,
    payload: InvoiceUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Updates an invoice and recalculates financial figures with tenant boundary checks.
    """
    if current_user.role not in (UserRole.SUPER_ADMIN, UserRole.BUSINESS_OWNER, UserRole.ACCOUNTANT, UserRole.STAFF):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access Denied.")

    existing = InvoiceService.get_invoice_by_id(db, invoice_id)
    verify_business_ownership(existing, current_user, "Invoice")

    invoice = InvoiceService.update_invoice(db, invoice_id, payload)

    try:
        ws_manager.broadcast_event(
            "invoice.updated",
            {
                "invoice_id": invoice.id,
                "invoice_number": invoice.invoice_number,
                "total_amount": invoice.total_amount,
                "status": invoice.status.value
            },
            business_id=invoice.business_id
        )
    except Exception:
        pass

    return invoice


@router.delete("/{invoice_id}", status_code=status.HTTP_200_OK)
def delete_invoice(
    invoice_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Soft-deletes a draft or cancelled invoice with role and tenant boundary check.
    """
    if current_user.role not in (UserRole.SUPER_ADMIN, UserRole.BUSINESS_OWNER):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Permission Denied: Only BUSINESS_OWNER or SUPER_ADMIN can delete invoices."
        )

    existing = InvoiceService.get_invoice_by_id(db, invoice_id)
    verify_business_ownership(existing, current_user, "Invoice")

    InvoiceService.delete_invoice(db, invoice_id)
    return {"message": f"Invoice {invoice_id} successfully deleted.", "status": "deleted"}


# ==============================================================================
# 2. Financial Governance: Human Approval & Rejection Endpoints
# ==============================================================================

@router.post("/{invoice_id}/approve", response_model=InvoiceResponse, status_code=status.HTTP_200_OK)
def approve_invoice(
    invoice_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Human-in-the-loop invoice approval.
    Enforces 'invoices.approve' permission (BUSINESS_OWNER or SUPER_ADMIN).
    Rejects unauthorized roles (e.g. STAFF) with HTTP 403 Forbidden.
    """
    if current_user.role not in (UserRole.SUPER_ADMIN, UserRole.BUSINESS_OWNER):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Permission Denied: Role '{current_user.role.value}' cannot approve invoices. Approval requires BUSINESS_OWNER or SUPER_ADMIN authority."
        )

    invoice = InvoiceService.get_invoice_by_id(db, invoice_id)
    verify_business_ownership(invoice, current_user, "Invoice")

    invoice.status = InvoiceStatus.SENT
    invoice.approved_by_user_id = current_user.id
    invoice.approved_by_name = current_user.name

    log = ActivityLog(
        action="INVOICE_APPROVED",
        entity_type="invoice",
        entity_id=str(invoice.id),
        description=f"Invoice {invoice.invoice_number} approved by {current_user.name} ({current_user.role.value})."
    )
    db.add(log)
    db.commit()
    db.refresh(invoice)

    try:
        ws_manager.broadcast_event(
            "invoice.approved",
            {
                "invoice_id": invoice.id,
                "invoice_number": invoice.invoice_number,
                "approved_by": current_user.name,
                "status": invoice.status.value
            },
            business_id=invoice.business_id
        )
    except Exception:
        pass

    return invoice


@router.post("/{invoice_id}/reject", response_model=InvoiceResponse, status_code=status.HTTP_200_OK)
def reject_invoice(
    invoice_id: int,
    notes: Optional[str] = Query(None, description="Reason for rejection"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Rejects an invoice and transitions state to CANCELLED.
    Enforces 'invoices.approve' permission.
    """
    if current_user.role not in (UserRole.SUPER_ADMIN, UserRole.BUSINESS_OWNER):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Permission Denied: Role '{current_user.role.value}' cannot reject invoices."
        )

    invoice = InvoiceService.get_invoice_by_id(db, invoice_id)
    verify_business_ownership(invoice, current_user, "Invoice")

    invoice.status = InvoiceStatus.CANCELLED
    if notes:
        invoice.notes = f"{invoice.notes or ''}\n[Rejected by {current_user.name}]: {notes}".strip()

    log = ActivityLog(
        action="INVOICE_REJECTED",
        entity_type="invoice",
        entity_id=str(invoice.id),
        description=f"Invoice {invoice.invoice_number} rejected by {current_user.name}: {notes or 'No reason provided'}."
    )
    db.add(log)
    db.commit()
    db.refresh(invoice)

    try:
        ws_manager.broadcast_event(
            "invoice.updated",
            {"invoice_id": invoice.id, "invoice_number": invoice.invoice_number, "status": invoice.status.value},
            business_id=invoice.business_id
        )
    except Exception:
        pass

    return invoice


# ==============================================================================
# 2. Document Upload & Text Extraction
# ==============================================================================

@router.post("/upload", response_model=DocumentUploadResponse, status_code=status.HTTP_201_CREATED)
async def upload_document(
    file: UploadFile = File(..., description="PDF or image billing document (max 10 MB)"),
    db: Session = Depends(get_db)
):
    """
    Uploads an invoice document (PDF, PNG, JPG, JPEG) with magic-byte verification.
    Stores the file securely and registers an upload token.
    """
    try:
        content = await file.read()
        upload_id, stored_filename, file_path = DocumentExtractor.save_upload(
            content, file.filename or "uploaded_invoice"
        )
    except DocumentExtractionError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"An error occurred while storing the file: {str(e)}"
        )

    # Register in database
    doc_upload = DocumentUpload(
        upload_id=upload_id,
        original_filename=file.filename or stored_filename,
        stored_filename=stored_filename,
        file_path=file_path,
        file_size=len(content),
        content_type=file.content_type or "application/octet-stream",
        extraction_status="UPLOADED"
    )
    db.add(doc_upload)
    db.commit()
    db.refresh(doc_upload)

    return DocumentUploadResponse(
        upload_id=doc_upload.upload_id,
        original_filename=doc_upload.original_filename,
        file_size_bytes=doc_upload.file_size,
        content_type=doc_upload.content_type,
        status="UPLOADED",
        message="Document uploaded and validated successfully."
    )


@router.post("/extract", response_model=ExtractionResponse, status_code=status.HTTP_200_OK)
def extract_invoice_data(payload: ExtractionRequest, db: Session = Depends(get_db)):
    """
    Extracts structured invoice information from an uploaded document.
    Performs OCR/text reading and structured parsing with arithmetic validation.
    """
    doc_upload = db.query(DocumentUpload).filter(
        DocumentUpload.upload_id == payload.upload_id
    ).first()

    if not doc_upload:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Upload session '{payload.upload_id}' not found."
        )

    ext = doc_upload.stored_filename.rsplit(".", 1)[-1].lower()
    
    # 1. Extract raw text from file
    try:
        raw_text, text_method = DocumentExtractor.extract_text(doc_upload.file_path, ext)
        doc_upload.extracted_text = raw_text
    except DocumentExtractionError as e:
        doc_upload.extraction_status = "FAILED"
        doc_upload.error_message = str(e)
        db.commit()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    # Check for scanned PDF or missing OCR
    if text_method == "scanned_pdf_requires_ocr":
        warning = (
            "This PDF appears to be a scanned image without an embedded digital text layer. "
            "To parse scanned documents, Tesseract OCR is required on the host system."
        )
        data = ExtractedInvoiceData(
            invoice_number=None,
            currency="USD",
            warnings=[warning],
            missing_fields=["invoice_number", "customer_name", "total_amount"],
            extraction_method="scanned_pdf_no_ocr"
        )
        doc_upload.extraction_status = "REQUIRES_MANUAL_REVIEW"
        doc_upload.extraction_data = data.model_dump()
        db.commit()
        return ExtractionResponse(upload_id=payload.upload_id, status="REQUIRES_MANUAL_REVIEW", data=data)

    if text_method == "image_ocr_unavailable":
        warning = (
            "Image OCR engine (Tesseract) is not detected on the server environment. "
            "To parse PNG/JPG scans, install Tesseract OCR. For native PDF files with embedded text, processing is fully supported."
        )
        data = ExtractedInvoiceData(
            invoice_number=None,
            currency="USD",
            warnings=[warning],
            missing_fields=["invoice_number", "customer_name", "total_amount"],
            extraction_method="image_ocr_unavailable"
        )
        doc_upload.extraction_status = "REQUIRES_MANUAL_REVIEW"
        doc_upload.extraction_data = data.model_dump()
        db.commit()
        return ExtractionResponse(upload_id=payload.upload_id, status="REQUIRES_MANUAL_REVIEW", data=data)

    # 2. Run structured extraction (LLM or pattern parser)
    data = AIExtractor.extract_structured_data(raw_text, use_ai=payload.use_ai)
    
    doc_upload.extraction_status = "COMPLETED"
    doc_upload.extraction_data = data.model_dump()
    db.commit()

    return ExtractionResponse(
        upload_id=payload.upload_id,
        status="COMPLETED",
        data=data
    )


@router.get("/uploads/{upload_id}/download", status_code=status.HTTP_200_OK)
def download_uploaded_document(upload_id: str, db: Session = Depends(get_db)):
    """
    Downloads or streams an uploaded billing document by upload_id.
    Includes mime-type negotiation and path traversal protection.
    """
    doc_upload = db.query(DocumentUpload).filter(DocumentUpload.upload_id == upload_id).first()
    if not doc_upload:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Upload session '{upload_id}' not found."
        )

    try:
        content_bytes, mime_type = storage_service.retrieve_file(doc_upload.stored_filename)
        return Response(
            content=content_bytes,
            media_type=mime_type,
            headers={
                "Content-Disposition": f'inline; filename="{doc_upload.original_filename}"',
                "X-Content-Type-Options": "nosniff"
            }
        )
    except StorageError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to retrieve document: {str(e)}")


@router.get("/{invoice_id}/document", status_code=status.HTTP_200_OK)
def download_invoice_document(invoice_id: int, db: Session = Depends(get_db)):
    """
    Retrieves the original source document attached to a specific invoice.
    """
    invoice = InvoiceService.get_invoice_by_id(db, invoice_id)
    if not invoice.document_path:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Invoice #{invoice.invoice_number} does not have an attached source document."
        )

    filename = os.path.basename(invoice.document_path)
    try:
        content_bytes, mime_type = storage_service.retrieve_file(filename)
        return Response(
            content=content_bytes,
            media_type=mime_type,
            headers={
                "Content-Disposition": f'inline; filename="{filename}"',
                "X-Content-Type-Options": "nosniff"
            }
        )
    except StorageError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))


