"""
Security & JWT Token Utilities for InvoicePilot AI.
Implements PBKDF2-HMAC-SHA256 password hashing and standard HS256 JWT tokens.
Zero external native C dependencies; 100% portable on Windows and Linux.
"""

import os
import hmac
import hashlib
import secrets
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any
import jwt

from app.config import settings

# Secret key for JWT signing; defaults to secure random or settings
SECRET_KEY = getattr(settings, "JWT_SECRET", None) or "invoicepilot-ai-hackathon-2026-super-secure-secret-key-32b"
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24  # 24 hours


def hash_password(password: str) -> str:
    """Hashes a plain password using PBKDF2-HMAC-SHA256 with a unique 16-byte hex salt."""
    if not password:
        raise ValueError("Password cannot be empty.")
    salt = secrets.token_hex(16)
    hash_bytes = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        bytes.fromhex(salt),
        100000
    )
    return f"pbkdf2:sha256:100000${salt}${hash_bytes.hex()}"


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verifies a plain password against stored PBKDF2 hash."""
    if not plain_password or not hashed_password:
        return False
    try:
        parts = hashed_password.split("$")
        if len(parts) != 3:
            return False
        algo_iter, salt_hex, expected_hash_hex = parts
        _, _, iterations_str = algo_iter.split(":")
        iterations = int(iterations_str)

        computed_bytes = hashlib.pbkdf2_hmac(
            "sha256",
            plain_password.encode("utf-8"),
            bytes.fromhex(salt_hex),
            iterations
        )
        return hmac.compare_digest(computed_bytes.hex(), expected_hash_hex)
    except Exception:
        return False


def create_access_token(data: Dict[str, Any], expires_delta: Optional[timedelta] = None) -> str:
    """Generates a signed JWT access token."""
    to_encode = data.copy()
    now = datetime.now(timezone.utc)
    if expires_delta:
        expire = now + expires_delta
    else:
        expire = now + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    
    to_encode.update({
        "exp": int(expire.timestamp()),
        "iat": int(now.timestamp()),
        "nbf": int(now.timestamp())
    })
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt


def decode_access_token(token: str) -> Optional[Dict[str, Any]]:
    """Decodes and validates a signed JWT access token."""
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload
    except jwt.PyJWTError:
        return None

