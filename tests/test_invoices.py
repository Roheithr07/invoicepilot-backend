"""
Comprehensive Test Suite for Invoice CRUD, Document Upload & Extraction in InvoicePilot AI.
"""

import io
from decimal import Decimal
from fastapi.testclient import TestClient
from pypdf import PdfWriter

from app.main import app
from app.services.ai_extractor import AIExtractor

client = TestClient(app)


def create_sample_pdf(content_text: str = "Invoice #INV-TEST-001\nBill To: John Doe\nEmail: john@example.com\nTotal: $120.00") -> io.BytesIO:
    """Helper to generate a minimal valid in-memory PDF."""
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    stream = io.BytesIO()
    writer.write(stream)
    stream.seek(0)
    return stream


# ==============================================================================
# 1. Invoice CRUD Tests
# ==============================================================================

def test_create_and_read_invoice():
    """Verify invoice creation, auto-customer linking, and Decimal totals calculation."""
    payload = {
        "invoice_number": "INV-UNIT-001",
        "customer_name": "Test Client Inc",
        "customer_email": "billing@testclient.com",
        "currency": "USD",
        "due_date": "2026-04-15T00:00:00Z",
        "notes": "Testing backend decimal calculations",
        "items": [
            {
                "description": "API Integration Sprint",
                "quantity": 10.0,
                "unit_price": 150.00,
                "tax_rate": 10.0
            },
            {
                "description": "Documentation",
                "quantity": 1.0,
                "unit_price": 500.00,
                "tax_rate": 0.0
            }
        ]
    }

    # 1. Create Invoice
    response = client.post("/api/invoices", json=payload)
    assert response.status_code == 201, response.text
    data = response.json()
    assert data["invoice_number"] == "INV-UNIT-001"
    assert data["status"] == "DRAFT"
    # Subtotal: (10*150) + 500 = 2000.00
    assert data["subtotal"] == 2000.00
    # Tax: 1500 * 0.10 = 150.00
    assert data["tax_total"] == 150.00
    # Total: 2000 + 150 = 2150.00
    assert data["total_amount"] == 2150.00
    assert len(data["items"]) == 2
    assert data["customer"]["email"] == "billing@testclient.com"
    invoice_id = data["id"]

    # 2. Read Invoice by ID
    get_resp = client.get(f"/api/invoices/{invoice_id}")
    assert get_resp.status_code == 200
    assert get_resp.json()["id"] == invoice_id

    # 3. List Invoices with Search
    list_resp = client.get("/api/invoices?search=INV-UNIT-001")
    assert list_resp.status_code == 200
    list_data = list_resp.json()
    assert list_data["total"] >= 1
    assert any(inv["id"] == invoice_id for inv in list_data["items"])


def test_duplicate_invoice_number_rejection():
    """Verify that creating two invoices with the same invoice number returns 409 Conflict."""
    payload = {
        "invoice_number": "INV-DUP-TEST",
        "customer_name": "Dupe Client",
        "customer_email": "dupe@test.com",
        "currency": "USD",
        "items": [{"description": "Service", "quantity": 1, "unit_price": 100}]
    }

    r1 = client.post("/api/invoices", json=payload)
    assert r1.status_code == 201

    r2 = client.post("/api/invoices", json=payload)
    assert r2.status_code == 409
    assert "already exists" in r2.json()["detail"]


def test_update_and_delete_invoice():
    """Verify updating invoice line items and soft-deleting draft invoice."""
    # Create draft
    create_resp = client.post("/api/invoices", json={
        "invoice_number": "INV-DEL-TEST",
        "customer_name": "Delete Client",
        "customer_email": "del@test.com",
        "currency": "USD",
        "items": [{"description": "Item 1", "quantity": 2, "unit_price": 50}]
    })
    inv_id = create_resp.json()["id"]

    # Update item
    update_resp = client.put(f"/api/invoices/{inv_id}", json={
        "items": [{"description": "Updated Item", "quantity": 4, "unit_price": 50, "tax_rate": 10}]
    })
    assert update_resp.status_code == 200
    updated_data = update_resp.json()
    # 4 * 50 = 200 subtotal, 20 tax = 220 total
    assert updated_data["subtotal"] == 200.00
    assert updated_data["tax_total"] == 20.00
    assert updated_data["total_amount"] == 220.00

    # Delete draft
    del_resp = client.delete(f"/api/invoices/{inv_id}")
    assert del_resp.status_code == 200

    # Verify not returned in active list
    get_after = client.get(f"/api/invoices/{inv_id}")
    assert get_after.status_code == 404


# ==============================================================================
# 2. Document Upload & Security Tests
# ==============================================================================

def test_upload_valid_pdf():
    """Verify valid PDF upload returns upload_id and file metadata."""
    pdf_stream = create_sample_pdf()
    files = {"file": ("test_invoice.pdf", pdf_stream.getvalue(), "application/pdf")}
    
    resp = client.post("/api/invoices/upload", files=files)
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert "upload_id" in data
    assert data["status"] == "UPLOADED"
    assert data["file_size_bytes"] > 0


def test_upload_invalid_extension():
    """Verify unsupported file extensions are rejected with 400 Bad Request."""
    files = {"file": ("malicious.exe", b"MZ\x90\x00\x03\x00\x00\x00", "application/x-dosexec")}
    resp = client.post("/api/invoices/upload", files=files)
    assert resp.status_code == 400
    assert "Unsupported file extension" in resp.json()["detail"]


def test_upload_mismatched_magic_bytes():
    """Verify spoofed PDF with fake extension but wrong signature is rejected."""
    files = {"file": ("fake.pdf", b"NOT_A_REAL_PDF_HEADER_CONTENT", "application/pdf")}
    resp = client.post("/api/invoices/upload", files=files)
    assert resp.status_code == 400
    assert "File content does not match" in resp.json()["detail"]


# ==============================================================================
# 3. AI Extraction & Arithmetic Validation Tests
# ==============================================================================

def test_heuristic_extraction_arithmetic():
    """Verify deterministic extraction engine extracts text and checks calculations."""
    sample_text = """
    TAX INVOICE
    Invoice Number: INV-98765
    Date: 2026-03-01
    Due Date: 2026-03-15
    Bill To: Acquired Dynamics Ltd
    Email: contact@acquireddynamics.com

    Software License 1 1200.00 1200.00
    Support Retainer 1 300.00 300.00

    Subtotal: $1,500.00
    Tax: $150.00
    Total: $1,650.00
    """

    data = AIExtractor.extract_structured_data(sample_text, use_ai=False)
    assert data.invoice_number == "INV-98765"
    assert data.customer_email == "contact@acquireddynamics.com"
    assert data.customer_name == "Acquired Dynamics Ltd"
    assert data.subtotal == 1500.00
    assert data.tax_amount == 150.00
    assert data.total_amount == 1650.00
    assert data.confidence["total_amount"] >= 0.90
    assert len(data.missing_fields) == 0


def test_empty_document_fallback():
    """Verify empty raw text returns zero hallucination with clear missing fields."""
    data = AIExtractor.extract_structured_data("", use_ai=False)
    assert data.invoice_number is None
    assert "invoice_number" in data.missing_fields
    assert "Document contains no readable text" in data.warnings[0]


def test_end_to_end_upload_extract_and_save():
    """Verify the full 10-step lifecycle: Upload -> Extract -> Verify -> Save -> Fetch."""
    # 1. Upload valid document
    pdf_stream = create_sample_pdf()
    files = {"file": ("service_billing.pdf", pdf_stream.getvalue(), "application/pdf")}
    up_resp = client.post("/api/invoices/upload", files=files)
    assert up_resp.status_code == 201
    upload_id = up_resp.json()["upload_id"]

    # 2. Request Extraction
    ext_resp = client.post("/api/invoices/extract", json={"upload_id": upload_id, "use_ai": False})
    assert ext_resp.status_code == 200
    ext_data = ext_resp.json()
    assert ext_data["upload_id"] == upload_id
    assert "data" in ext_data

    # 3. Save confirmed invoice with linked upload_id
    save_resp = client.post("/api/invoices", json={
        "invoice_number": "INV-E2E-100",
        "customer_name": "E2E Enterprise",
        "customer_email": "finance@e2e-enterprise.com",
        "currency": "USD",
        "items": [
            {"description": "Extracted Cloud Audit", "quantity": 1, "unit_price": 1200.00, "tax_rate": 10.0}
        ],
        "document_upload_id": upload_id
    })
    assert save_resp.status_code == 201
    saved_invoice = save_resp.json()
    assert saved_invoice["invoice_number"] == "INV-E2E-100"
    assert saved_invoice["total_amount"] == 1320.00  # 1200 + 120 tax

    # 4. Fetch saved invoice and verify persistence
    get_resp = client.get(f"/api/invoices/{saved_invoice['id']}")
    assert get_resp.status_code == 200
    assert get_resp.json()["customer"]["name"] == "E2E Enterprise"
