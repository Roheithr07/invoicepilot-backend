"""
Document Upload & Text Extraction Service for InvoicePilot AI.
Handles secure file storage, magic byte verification, and PDF/OCR extraction.
"""

import os
import uuid
import shutil
from typing import Tuple, Optional
from pypdf import PdfReader


UPLOAD_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

MAX_FILE_SIZE_BYTES = 10 * 1024 * 1024  # 10 MB

# Magic byte signatures for supported file types
MAGIC_BYTES = {
    "pdf": b"%PDF",
    "png": b"\x89PNG\r\n\x1a\n",
    "jpg": b"\xff\xd8\xff",
    "jpeg": b"\xff\xd8\xff"
}


class DocumentExtractionError(Exception):
    """Custom exception for document extraction failures."""
    pass


class DocumentExtractor:
    @staticmethod
    def validate_file_signature(file_bytes: bytes, filename: str) -> str:
        """
        Validates both file extension and magic byte headers.
        Returns the confirmed normalized file extension (pdf, png, jpg, jpeg).
        """
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if ext not in ["pdf", "png", "jpg", "jpeg"]:
            raise DocumentExtractionError(
                f"Unsupported file extension '.{ext}'. Supported formats: PDF, PNG, JPG, JPEG."
            )

        if ext == "pdf" and not file_bytes.startswith(MAGIC_BYTES["pdf"]):
            raise DocumentExtractionError("File content does not match valid PDF header signature.")
        elif ext == "png" and not file_bytes.startswith(MAGIC_BYTES["png"]):
            raise DocumentExtractionError("File content does not match valid PNG image signature.")
        elif ext in ["jpg", "jpeg"] and not file_bytes.startswith(b"\xff\xd8"):
            raise DocumentExtractionError("File content does not match valid JPEG image signature.")

        return ext

    @staticmethod
    def save_upload(file_bytes: bytes, original_filename: str) -> Tuple[str, str, str]:
        """
        Saves uploaded file bytes to secure storage with a collision-resistant UUID filename.
        Returns (upload_id, stored_filename, absolute_file_path).
        """
        from app.services.storage import storage_service, StorageError
        try:
            return storage_service.store_file(file_bytes, original_filename)
        except StorageError as e:
            raise DocumentExtractionError(str(e))

    @staticmethod
    def extract_text_from_pdf(file_path: str) -> str:
        """
        Extracts embedded textual layers from PDF documents using PyPDF.
        """
        try:
            reader = PdfReader(file_path)
            extracted_pages = []
            for i, page in enumerate(reader.pages):
                text = page.extract_text()
                if text:
                    extracted_pages.append(text.strip())

            full_text = "\n\n".join(extracted_pages).strip()
            return full_text
        except Exception as e:
            raise DocumentExtractionError(f"Failed to parse PDF document structure: {str(e)}")

    @staticmethod
    def extract_text(file_path: str, ext: str) -> Tuple[str, str]:
        """
        Main extraction entrypoint.
        Returns (extracted_text, extraction_method).
        """
        if ext == "pdf":
            text = DocumentExtractor.extract_text_from_pdf(file_path)
            if text:
                return text, "pypdf_native"
            # PDF exists but has no embedded text (scanned PDF)
            return (
                "",
                "scanned_pdf_requires_ocr"
            )
        elif ext in ["png", "jpg", "jpeg"]:
            # Image OCR abstraction check
            # Since Tesseract is not installed on this host, return clean actionable notice
            return (
                "",
                "image_ocr_unavailable"
            )
        else:
            raise DocumentExtractionError(f"Extraction not supported for file type: {ext}")

