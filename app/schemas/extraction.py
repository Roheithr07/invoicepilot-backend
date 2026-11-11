"""
Pydantic Schemas for Document Upload & AI Invoice Extraction.
"""

from datetime import datetime, date
from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field


class DocumentUploadResponse(BaseModel):
    upload_id: str = Field(..., description="Unique token identifying the uploaded document")
    original_filename: str = Field(..., description="Original name of the uploaded file")
    file_size_bytes: int = Field(..., description="File size in bytes")
    content_type: str = Field(..., description="Validated MIME type")
    status: str = Field(default="UPLOADED", description="Current document processing status")
    uploaded_at: datetime = Field(default_factory=datetime.utcnow)
    message: str = Field(default="Document uploaded and validated successfully")


class ExtractedItem(BaseModel):
    description: str = Field(default="Line Item", description="Description of the goods or services")
    quantity: float = Field(default=1.0, description="Quantity extracted")
    unit_price: float = Field(default=0.0, description="Unit price extracted")
    tax_rate: Optional[float] = Field(default=None, description="Extracted tax rate in percentage")
    line_total: Optional[float] = Field(default=None, description="Extracted or computed line item total")


class ExtractedInvoiceData(BaseModel):
    invoice_number: Optional[str] = Field(default=None, description="Extracted invoice identifier")
    customer_name: Optional[str] = Field(default=None, description="Extracted customer / company name")
    customer_email: Optional[str] = Field(default=None, description="Extracted customer email address")
    invoice_date: Optional[str] = Field(default=None, description="Extracted issue date in YYYY-MM-DD")
    due_date: Optional[str] = Field(default=None, description="Extracted payment due date in YYYY-MM-DD")
    currency: str = Field(default="USD", description="Currency symbol or 3-letter code")
    items: List[ExtractedItem] = Field(default_factory=list, description="Extracted itemized bill items")
    subtotal: Optional[float] = Field(default=None, description="Extracted stated subtotal")
    tax_amount: Optional[float] = Field(default=None, description="Extracted stated tax amount")
    total_amount: Optional[float] = Field(default=None, description="Extracted final total amount")
    confidence: Dict[str, float] = Field(default_factory=dict, description="Field-level extraction confidence score (0.0 - 1.0)")
    missing_fields: List[str] = Field(default_factory=list, description="List of required invoice fields not found in document")
    warnings: List[str] = Field(default_factory=list, description="Data discrepancy or calculation warnings")
    raw_text: Optional[str] = Field(default=None, description="Truncated preview of raw extracted text")
    extraction_method: str = Field(default="heuristic_parser", description="Method utilized: pypdf_heuristic, gemini_ai, or manual_review")


class ExtractionRequest(BaseModel):
    upload_id: str = Field(..., description="Upload token from POST /api/invoices/upload")
    use_ai: bool = Field(default=True, description="Whether to trigger configured LLM extraction if API key present")


class ExtractionResponse(BaseModel):
    upload_id: str
    status: str
    data: ExtractedInvoiceData
    processed_at: datetime = Field(default_factory=datetime.utcnow)

