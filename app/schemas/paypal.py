"""
Pydantic Schemas for PayPal Sandbox Integration.
Supports OAuth status, draft creation, dispatch confirmation, status tracking, and error governance.
"""

from datetime import datetime
from typing import Optional, Dict, Any, List
from pydantic import BaseModel, Field


class PayPalConfigStatus(BaseModel):
    client_id_configured: bool = Field(..., description="Whether PAYPAL_CLIENT_ID is present in environment")
    client_secret_configured: bool = Field(..., description="Whether PAYPAL_CLIENT_SECRET is present in environment")
    sandbox_mode: bool = Field(default=True, description="Always true in sandbox mode")
    base_url: str = Field(..., description="Configured PayPal REST API base URL")
    masked_client_id: Optional[str] = Field(None, description="Partially masked Client ID (e.g. AbC1...9xYz)")
    webhook_id_configured: bool = Field(default=False, description="Whether PAYPAL_WEBHOOK_ID is configured")
    apimatic_status: str = Field(
        default="Not integrated natively in Antigravity IDE (Documented restriction; official PayPal v2 schemas used).",
        description="Compatibility status of APIMatic Context Plugin"
    )


class PayPalHealthResponse(BaseModel):
    status: str = Field(..., description="'connected', 'misconfigured', 'authentication_failed', or 'error'")
    mode: str = Field(default="sandbox")
    authenticated: bool = Field(..., description="True if OAuth 2.0 handshake succeeded")
    token_cached: bool = Field(..., description="True if an unexpired access token is currently cached in memory")
    token_expires_in_seconds: Optional[int] = Field(None, description="Remaining seconds until access token expiration")
    message: str = Field(..., description="Safe human-readable status description")
    connection_state: Optional[str] = Field(
        default=None,
        description="Explicit three-state classification: 'missing_configuration', 'authentication_failed', or 'sandbox_authenticated_successfully'"
    )


class PayPalDraftRequest(BaseModel):
    memo: Optional[str] = Field(None, description="Optional internal reference note for merchant")
    note: Optional[str] = Field(None, description="Optional note displayed to the recipient on the invoice")


class PayPalSendRequest(BaseModel):
    confirmed: bool = Field(..., description="Explicit human confirmation required before external dispatch")
    subject: Optional[str] = Field(None, description="Optional custom email subject for PayPal dispatch")
    note: Optional[str] = Field(None, description="Optional custom message included in the PayPal delivery email")
    send_to_recipient: bool = Field(default=True, description="Whether to send delivery email to client")
    send_to_invoicer: bool = Field(default=False, description="Whether to send copy to merchant")


class PayPalOperationResponse(BaseModel):
    success: bool
    local_invoice_id: int
    invoice_number: str
    paypal_invoice_id: Optional[str] = None
    paypal_status: Optional[str] = None
    paypal_invoice_url: Optional[str] = None
    message: str
    details: Optional[Dict[str, Any]] = None
    operation_timestamp: datetime = Field(default_factory=datetime.utcnow)


class PayPalStatusResponse(BaseModel):
    local_invoice_id: int
    invoice_number: str
    paypal_invoice_id: str
    paypal_status: str
    local_status: str
    is_paid: bool
    is_sent: bool
    last_checked_at: datetime
    paypal_invoice_url: Optional[str] = None
    amount_total: Optional[float] = None
    currency: Optional[str] = None
    raw_remote_summary: Optional[Dict[str, Any]] = None

