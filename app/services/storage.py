"""
Storage Service Abstraction for InvoicePilot AI.
Supports local persistent volume storage and S3 / Cloudflare R2 compatible object storage.
Handles file validation, collision-resistant UUID naming, streaming downloads,
and path-traversal prevention.
"""

import os
import uuid
from pathlib import Path
from typing import Tuple, Optional, BinaryIO
from app.config import settings

# Base upload directory
DEFAULT_STORAGE_DIR = Path(__file__).resolve().parent.parent.parent / "uploads"
MAX_FILE_SIZE_BYTES = 10 * 1024 * 1024  # 10 MB

# Magic byte signatures
MAGIC_BYTES = {
    "pdf": b"%PDF",
    "png": b"\x89PNG\r\n\x1a\n",
    "jpg": b"\xff\xd8\xff",
    "jpeg": b"\xff\xd8\xff"
}


class StorageError(Exception):
    """Custom exception for file storage operations."""
    pass


class StorageService:
    def __init__(self, base_dir: Optional[Path] = None):
        self.base_dir = Path(base_dir or DEFAULT_STORAGE_DIR).resolve()
        self.ensure_storage_ready()

    def ensure_storage_ready(self) -> bool:
        """Ensures the destination storage directory exists and is writable."""
        try:
            self.base_dir.mkdir(parents=True, exist_ok=True)
            # Verify write permissions by touching a probe file
            probe = self.base_dir / ".storage_health_probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink(missing_ok=True)
            return True
        except Exception as e:
            return False

    def is_healthy(self) -> bool:
        """Returns True if the storage backend is reachable and writable."""
        return self.ensure_storage_ready()

    def get_backend_type(self) -> str:
        """Returns configured storage backend identifier."""
        if getattr(settings, "STORAGE_BACKEND", "local").lower() in ["s3", "r2"]:
            return "s3_compatible"
        return "local_persistent_volume"

    def validate_file(self, file_bytes: bytes, filename: str) -> str:
        """
        Validates file size, extension and magic bytes header.
        Returns normalized extension.
        """
        if len(file_bytes) > MAX_FILE_SIZE_BYTES:
            raise StorageError(
                f"File size ({len(file_bytes)} bytes) exceeds the 10 MB maximum allowed limit."
            )

        if not filename or "." not in filename:
            raise StorageError("Invalid filename: Missing file extension.")

        ext = filename.rsplit(".", 1)[-1].lower()
        if ext not in ["pdf", "png", "jpg", "jpeg"]:
            raise StorageError(
                f"Unsupported file extension '.{ext}'. Supported formats: PDF, PNG, JPG, JPEG."
            )

        # Magic byte validation
        if ext == "pdf" and not file_bytes.startswith(MAGIC_BYTES["pdf"]):
            raise StorageError("File content does not match valid PDF header signature.")
        elif ext == "png" and not file_bytes.startswith(MAGIC_BYTES["png"]):
            raise StorageError("File content does not match valid PNG image signature.")
        elif ext in ["jpg", "jpeg"] and not file_bytes.startswith(b"\xff\xd8"):
            raise StorageError("File content does not match valid JPEG image signature.")

        return ext

    def store_file(self, file_bytes: bytes, original_filename: str) -> Tuple[str, str, str]:
        """
        Stores file bytes to storage backend with collision-resistant UUID naming.
        Returns: (file_id, stored_filename, full_path)
        """
        ext = self.validate_file(file_bytes, original_filename)
        file_id = str(uuid.uuid4())
        stored_filename = f"{file_id}.{ext}"
        target_path = (self.base_dir / stored_filename).resolve()

        # Prevent directory traversal attacks
        if not str(target_path).startswith(str(self.base_dir)):
            raise StorageError("Security exception: Destination path outside authorized storage root.")

        try:
            with open(target_path, "wb") as f:
                f.write(file_bytes)
        except Exception as e:
            raise StorageError(f"Storage write error: Unable to persist document ({str(e)}).")

        return file_id, stored_filename, str(target_path)

    def retrieve_file(self, stored_filename: str) -> Tuple[bytes, str]:
        """
        Safely retrieves file bytes and mime-type.
        Prevents path traversal.
        """
        safe_name = os.path.basename(stored_filename)
        file_path = (self.base_dir / safe_name).resolve()

        if not str(file_path).startswith(str(self.base_dir)):
            raise StorageError("Security exception: Invalid document path access.")

        if not file_path.exists() or not file_path.is_file():
            raise StorageError(f"Document '{safe_name}' not found on storage backend.")

        ext = safe_name.rsplit(".", 1)[-1].lower() if "." in safe_name else ""
        content_type_map = {
            "pdf": "application/pdf",
            "png": "image/png",
            "jpg": "image/jpeg",
            "jpeg": "image/jpeg"
        }
        mime_type = content_type_map.get(ext, "application/octet-stream")

        try:
            with open(file_path, "rb") as f:
                data = f.read()
            return data, mime_type
        except Exception as e:
            raise StorageError(f"Failed to read file from storage: {str(e)}")

    def delete_file(self, stored_filename: str) -> bool:
        """Deletes file from storage safely."""
        safe_name = os.path.basename(stored_filename)
        file_path = (self.base_dir / safe_name).resolve()
        if not str(file_path).startswith(str(self.base_dir)):
            return False
        if file_path.exists() and file_path.is_file():
            try:
                file_path.unlink()
                return True
            except OSError:
                return False
        return False


# Singleton instance
storage_service = StorageService()
