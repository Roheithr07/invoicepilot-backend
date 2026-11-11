"""
InvoicePilot AI — Phase 10 Production Reliability & Continuous Operations Tests.
Verifies document storage abstraction, download endpoint, path traversal protection,
exponential backoff, pre-retry remote verification, readiness probe, and backup integrity.
"""

import io
import os
import json
import pytest
from pathlib import Path
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.main import app
from app.services.storage import storage_service, StorageError
from app.services.resilience import (
    retry_with_backoff, verify_remote_before_retry, RetryBudgetExceeded
)
from app.services.backup_service import BackupService


client = TestClient(app)


def test_readiness_probe():
    """Verify Kubernetes / container readiness probe returns 200 and healthy storage."""
    resp = client.get("/ready")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ready"
    assert "timestamp" in data

    resp_api = client.get("/api/ready")
    assert resp_api.status_code == 200


def test_storage_service_validation_and_lifecycle():
    """Verify storage service validates magic bytes, persists safely, and streams downloads."""
    # 1. Valid PDF upload and download
    valid_pdf = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF"
    file_id, stored_name, full_path = storage_service.store_file(valid_pdf, "test_doc.pdf")
    assert file_id is not None
    assert stored_name.endswith(".pdf")
    assert os.path.exists(full_path)

    # 2. Retrieve file
    retrieved_bytes, mime = storage_service.retrieve_file(stored_name)
    assert retrieved_bytes == valid_pdf
    assert mime == "application/pdf"

    # 3. Path traversal attack attempt
    with pytest.raises(StorageError):
        storage_service.retrieve_file("../../../etc/passwd")

    with pytest.raises(StorageError):
        storage_service.retrieve_file("..\\..\\Windows\\System32\\cmd.exe")

    # 4. Clean up
    deleted = storage_service.delete_file(stored_name)
    assert deleted is True

    # 5. Missing file retrieve raises StorageError
    with pytest.raises(StorageError):
        storage_service.retrieve_file(stored_name)


def test_document_download_api_endpoints():
    """Verify document download API route streams valid file and rejects invalid upload_id."""
    # Upload via API
    pdf_content = b"%PDF-1.7\nInvoice Test Sample\n%%EOF"
    files = {"file": ("invoice_sample.pdf", pdf_content, "application/pdf")}
    resp = client.post("/api/invoices/upload", files=files)
    assert resp.status_code == 201
    upload_id = resp.json()["upload_id"]

    # Download via API
    dl_resp = client.get(f"/api/invoices/uploads/{upload_id}/download")
    assert dl_resp.status_code == 200
    assert dl_resp.headers["content-type"] == "application/pdf"
    assert dl_resp.content == pdf_content

    # Non-existent upload_id
    bad_resp = client.get("/api/invoices/uploads/non-existent-upload-id/download")
    assert bad_resp.status_code == 404


def test_retry_with_backoff_transient_failure():
    """Verify exponential backoff retries transient errors and succeeds on retry."""
    attempts = 0

    def flaky_operation():
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise ConnectionError("Transient network glitch")
        return "SUCCESS"

    result = retry_with_backoff(
        flaky_operation,
        max_retries=3,
        base_delay=0.01,
        backoff_factor=1.5,
        retryable_exceptions=(ConnectionError,),
        operation_name="test_flaky"
    )
    assert result == "SUCCESS"
    assert attempts == 3


def test_retry_with_backoff_exhausted():
    """Verify exponential backoff raises RetryBudgetExceeded when budget is exhausted."""
    def always_failing():
        raise TimeoutError("PayPal sandbox gateway timeout")

    with pytest.raises(RetryBudgetExceeded):
        retry_with_backoff(
            always_failing,
            max_retries=2,
            base_delay=0.01,
            retryable_exceptions=(TimeoutError,),
            operation_name="test_timeout"
        )


def test_verify_remote_before_retry_protection():
    """Verify pre-retry check halts duplicate dispatch if remote state already reflects completion."""
    # Case A: Remote status already SENT -> Action must NOT be re-executed
    action_called = False

    def check_remote_sent():
        return {"id": "INV2-TEST-123", "status": "SENT"}

    def dangerous_financial_send():
        nonlocal action_called
        action_called = True
        return {"result": "sent_again"}

    state, was_already_completed = verify_remote_before_retry(
        state_verifier=check_remote_sent,
        action=dangerous_financial_send,
        already_completed_predicate=lambda s: s.get("status") in ["SENT", "PAID"],
        operation_name="paypal_invoice_send"
    )
    assert was_already_completed is True
    assert action_called is False  # Guarded from duplicate dispatch!
    assert state["status"] == "SENT"

    # Case B: Remote status is DRAFT -> Action executes safely
    def check_remote_draft():
        return {"id": "INV2-TEST-123", "status": "DRAFT"}

    state2, was_already_completed2 = verify_remote_before_retry(
        state_verifier=check_remote_draft,
        action=dangerous_financial_send,
        already_completed_predicate=lambda s: s.get("status") in ["SENT", "PAID"],
        operation_name="paypal_invoice_send"
    )
    assert was_already_completed2 is False
    assert action_called is True


def test_database_backup_integrity_checksum(db_session: Session, tmp_path: Path):
    """Verify backup snapshot computes SHA-256 checksum and verifies record count."""
    backup_file = tmp_path / "test_snapshot.json"
    metadata = BackupService.export_database_snapshot(db_session, backup_file)
    assert backup_file.exists()
    assert "sha256_hash" in metadata
    assert len(metadata["sha256_hash"]) == 64
    assert "customers" in metadata["record_counts"]
    assert "invoices" in metadata["record_counts"]

