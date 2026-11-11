"""
PayPal Developer Sandbox Invoicing REST API Service for InvoicePilot AI.
Provides OAuth 2.0 token caching, Pydantic model mapping to PayPal v2 schema,
draft creation, explicit two-step dispatch, remote status reconciliation, and secret redaction.
"""

import base64
import json
import logging
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Dict, Any, Optional, Tuple
import httpx

from app.config import settings
from app.models.models import Invoice

logger = logging.getLogger("InvoicePilot.PayPalService")


class PayPalError(Exception):
    """Base exception for PayPal REST API operations."""
    def __init__(self, message: str, status_code: Optional[int] = None, details: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.details = details or {}


class PayPalConfigurationError(PayPalError):
    """Raised when PayPal Sandbox credentials or settings are missing or invalid."""
    pass


class PayPalAuthenticationError(PayPalError):
    """Raised when OAuth 2.0 client-credentials authentication fails."""
    pass


class PayPalValidationError(PayPalError):
    """Raised when invoice payload fails local or PayPal remote schema validation."""
    pass


class PayPalDuplicateError(PayPalError):
    """Raised when an operation would create duplicate remote drafts or send redundant dispatches."""
    pass


def sanitize_sensitive_data(text: str) -> str:
    """Redacts tokens, secrets, and auth headers from text or error strings."""
    if not text:
        return ""
    # Redact Bearer access tokens
    text = re.sub(r"Bearer\s+([A-Za-z0-9_\-\.]+)", r"Bearer [REDACTED_TOKEN]", text)
    # Redact Basic auth hashes
    text = re.sub(r"Basic\s+([A-Za-z0-9+/=]+)", r"Basic [REDACTED_CREDENTIALS]", text)
    # Redact access_token JSON field
    text = re.sub(r'"access_token"\s*:\s*"[^"]+"', '"access_token": "[REDACTED_TOKEN]"', text)
    # Redact client_secret if present
    if settings.PAYPAL_CLIENT_SECRET:
        text = text.replace(settings.PAYPAL_CLIENT_SECRET, "[REDACTED_SECRET]")
    # Redact webhook_id if present
    if settings.PAYPAL_WEBHOOK_ID and not settings.PAYPAL_WEBHOOK_ID.startswith("your_"):
        text = text.replace(settings.PAYPAL_WEBHOOK_ID, "[REDACTED_WEBHOOK_ID]")
    return text


class PayPalService:
    """
    Client for PayPal Invoicing REST API v2.
    """
    _cached_token: Optional[str] = None
    _token_expires_at: float = 0.0

    @classmethod
    def get_base_url(cls) -> str:
        return (settings.PAYPAL_BASE_URL or "https://api-m.sandbox.paypal.com").rstrip("/")

    @classmethod
    def is_configured(cls) -> bool:
        client_id = (settings.PAYPAL_CLIENT_ID or "").strip()
        client_secret = (settings.PAYPAL_CLIENT_SECRET or "").strip()
        return bool(client_id and client_secret and not client_id.startswith("your_"))

    @classmethod
    def is_webhook_configured(cls) -> bool:
        webhook_id = (settings.PAYPAL_WEBHOOK_ID or "").strip()
        return bool(webhook_id and not webhook_id.startswith("your_"))

    @classmethod
    def get_masked_client_id(cls) -> Optional[str]:
        client_id = (settings.PAYPAL_CLIENT_ID or "").strip()
        if not client_id or client_id.startswith("your_"):
            return None
        if len(client_id) <= 8:
            return "******"
        return f"{client_id[:4]}...{client_id[-4:]}"

    @classmethod
    def get_masked_webhook_id(cls) -> Optional[str]:
        webhook_id = (settings.PAYPAL_WEBHOOK_ID or "").strip()
        if not webhook_id or webhook_id.startswith("your_"):
            return None
        if len(webhook_id) <= 6:
            return "******"
        return f"{webhook_id[:3]}...{webhook_id[-3:]}"

    @classmethod
    def clear_token_cache(cls):
        """Clears the cached access token."""
        cls._cached_token = None
        cls._token_expires_at = 0.0

    @classmethod
    def get_access_token(cls, force_refresh: bool = False) -> str:
        """
        Retrieves a valid OAuth 2.0 access token using client-credentials grant.
        Caches token in memory and automatically refreshes 300 seconds before expiration.
        """
        now = time.time()
        # Return cached token if valid with at least 5 minutes remaining
        if not force_refresh and cls._cached_token and (cls._token_expires_at - now > 300):
            return cls._cached_token

        client_id = (settings.PAYPAL_CLIENT_ID or "").strip()
        client_secret = (settings.PAYPAL_CLIENT_SECRET or "").strip()

        if not cls.is_configured():
            raise PayPalConfigurationError(
                "PayPal Developer Sandbox credentials are not configured. "
                "Please configure PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET in the backend environment."
            )

        token_url = f"{cls.get_base_url()}/v1/oauth2/token"
        auth_header = base64.b64encode(f"{client_id}:{client_secret}".encode("utf-8")).decode("utf-8")

        headers = {
            "Authorization": f"Basic {auth_header}",
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json"
        }
        data = {"grant_type": "client_credentials"}

        try:
            with httpx.Client(timeout=10.0) as client:
                resp = client.post(token_url, headers=headers, data=data)
        except httpx.TimeoutException:
            raise PayPalAuthenticationError("Connection timeout while contacting PayPal OAuth endpoint.")
        except httpx.RequestError as exc:
            raise PayPalAuthenticationError(f"Network error while connecting to PayPal OAuth: {sanitize_sensitive_data(str(exc))}")

        if resp.status_code != 200:
            err_msg = f"PayPal OAuth authentication failed with HTTP {resp.status_code}."
            try:
                err_json = resp.json()
                desc = err_json.get("error_description") or err_json.get("message") or str(err_json)
                err_msg += f" Detail: {desc}"
            except Exception:
                err_msg += f" Raw: {resp.text[:200]}"
            raise PayPalAuthenticationError(sanitize_sensitive_data(err_msg), status_code=resp.status_code)

        token_data = resp.json()
        access_token = token_data.get("access_token")
        expires_in = token_data.get("expires_in", 32400)

        if not access_token:
            raise PayPalAuthenticationError("OAuth response did not contain an access_token.")

        cls._cached_token = access_token
        cls._token_expires_at = now + expires_in
        logger.info(f"Obtained PayPal Sandbox OAuth token (valid for {expires_in} seconds).")
        return access_token

    @classmethod
    def verify_connectivity(cls) -> Dict[str, Any]:
        """
        Performs a safe connection test to the PayPal Sandbox.
        Returns connection status without leaking credentials.
        Distinguishes:
        - Missing configuration
        - Authentication failed
        - Sandbox authenticated successfully
        """
        if not cls.is_configured():
            return {
                "status": "misconfigured",
                "connection_state": "missing_configuration",
                "mode": "sandbox",
                "authenticated": False,
                "token_cached": False,
                "token_expires_in_seconds": None,
                "message": "Missing configuration: PayPal Sandbox credentials (PAYPAL_CLIENT_ID or PAYPAL_CLIENT_SECRET) are missing or set to placeholder defaults."
            }

        try:
            token = cls.get_access_token()
            remaining = max(0, int(cls._token_expires_at - time.time()))
            return {
                "status": "connected",
                "connection_state": "sandbox_authenticated_successfully",
                "mode": "sandbox",
                "authenticated": True,
                "token_cached": True,
                "token_expires_in_seconds": remaining,
                "message": f"Sandbox authenticated successfully with PayPal Developer Sandbox ({cls.get_base_url()})."
            }
        except PayPalAuthenticationError as e:
            return {
                "status": "authentication_failed",
                "connection_state": "authentication_failed",
                "mode": "sandbox",
                "authenticated": False,
                "token_cached": False,
                "token_expires_in_seconds": None,
                "message": f"Authentication failed: {sanitize_sensitive_data(e.message)}"
            }
        except PayPalError as e:
            return {
                "status": "error",
                "connection_state": "authentication_failed",
                "mode": "sandbox",
                "authenticated": False,
                "token_cached": False,
                "token_expires_in_seconds": None,
                "message": sanitize_sensitive_data(e.message)
            }
        except Exception as e:
            return {
                "status": "error",
                "connection_state": "authentication_failed",
                "mode": "sandbox",
                "authenticated": False,
                "token_cached": False,
                "token_expires_in_seconds": None,
                "message": f"Unexpected error during connection test: {sanitize_sensitive_data(str(e))}"
            }

    @classmethod
    def map_invoice_to_paypal_v2(cls, invoice: Invoice, merchant_name: str = "InvoicePilot Merchant", merchant_email: str = "merchant@invoicepilot.ai") -> Dict[str, Any]:
        """
        Maps a local Invoice record to the official PayPal Invoicing v2 schema.
        Validates required fields and applies formatting according to PayPal specifications.
        """
        # Validate Customer Email
        customer_email = invoice.customer_email or (invoice.customer.email if invoice.customer else None)
        if not customer_email or not customer_email.strip():
            raise PayPalValidationError("Customer email is required for PayPal invoice delivery.")

        customer_name = invoice.customer_name or (invoice.customer.name if invoice.customer else "Valued Client")
        currency = (invoice.currency or "USD").upper().strip()

        # Validate line items
        items = invoice.items or []
        if not items:
            raise PayPalValidationError("At least one line item is required to create a PayPal invoice.")

        # Format dates (YYYY-MM-DD)
        inv_date_str = (invoice.issue_date or datetime.now(timezone.utc)).strftime("%Y-%m-%d")
        
        detail_obj: Dict[str, Any] = {
            "invoice_number": invoice.invoice_number,
            "currency_code": currency,
            "invoice_date": inv_date_str,
            "note": invoice.notes or "Thank you for your business.",
            "memo": f"InvoicePilot local ID #{invoice.id}"
        }

        if invoice.due_date:
            detail_obj["payment_term"] = {
                "term_type": "DUE_ON_DATE_SPECIFIED",
                "due_date": invoice.due_date.strftime("%Y-%m-%d")
            }

        # Build Invoicer
        invoicer_obj = {
            "name": {
                "given_name": merchant_name.split()[0] if merchant_name else "InvoicePilot",
                "surname": " ".join(merchant_name.split()[1:]) if len(merchant_name.split()) > 1 else "Merchant"
            },
            "email_address": merchant_email
        }

        # Build Primary Recipient
        recipients_obj = [
            {
                "billing_info": {
                    "name": {
                        "given_name": customer_name
                    },
                    "email_address": customer_email.strip()
                }
            }
        ]

        # Build Line Items
        paypal_items = []
        for it in items:
            unit_val = f"{float(it.unit_price or 0.0):.2f}"
            qty_val = f"{float(it.quantity or 1.0):.2f}".rstrip("0").rstrip(".")
            if not qty_val or qty_val == "0":
                qty_val = "1"

            item_dict: Dict[str, Any] = {
                "name": it.description[:300] if it.description else "Service Item",
                "quantity": qty_val,
                "unit_amount": {
                    "currency_code": currency,
                    "value": unit_val
                },
                "unit_of_measure": "QUANTITY"
            }

            if it.tax_rate and float(it.tax_rate) > 0.0:
                item_dict["tax"] = {
                    "name": "Tax",
                    "percent": f"{float(it.tax_rate):.2f}".rstrip("0").rstrip(".")
                }

            paypal_items.append(item_dict)

        payload = {
            "detail": detail_obj,
            "invoicer": invoicer_obj,
            "primary_recipients": recipients_obj,
            "items": paypal_items,
            "configuration": {
                "allow_tip": False,
                "tax_calculated_after_discount": True,
                "tax_inclusive": False
            }
        }
        return payload

    @classmethod
    def _handle_api_error_response(
        cls,
        resp: httpx.Response,
        action_name: str,
        error_cls=PayPalError
    ) -> None:
        """
        Parses PayPal REST API error payloads according to APIMatic standards:
        - Automatically clears cached OAuth token on 401 Unauthorized to trigger fresh re-authentication.
        - Extracts paypal-debug-id from headers and body for transaction traceability.
        - Parses field-level validation errors from details array.
        """
        if resp.status_code == 401:
            cls.clear_token_cache()
            logger.warning("PayPal OAuth token rejected with 401 Unauthorized; token cache cleared.")

        debug_id = resp.headers.get("paypal-debug-id")
        details: Dict[str, Any] = {}
        err_msg = f"Failed to {action_name} (HTTP {resp.status_code})."

        try:
            parsed = resp.json()
            if isinstance(parsed, dict):
                details = parsed
                if not debug_id and "debug_id" in parsed:
                    debug_id = parsed.get("debug_id")
                # Format detailed validation issues if present
                issues_list = parsed.get("details", [])
                issue_summary = ""
                if isinstance(issues_list, list) and issues_list:
                    formatted_issues = []
                    for iss in issues_list:
                        if isinstance(iss, dict):
                            field_name = iss.get("field", "payload")
                            issue_desc = iss.get("description") or iss.get("issue", "")
                            formatted_issues.append(f"{field_name}: {issue_desc}")
                    if formatted_issues:
                        issue_summary = " Details: " + "; ".join(formatted_issues)

                main_msg = parsed.get("message") or parsed.get("error_description") or parsed.get("name") or ""
                if main_msg:
                    err_msg += f" Details: {main_msg}.{issue_summary}"
                elif issue_summary:
                    err_msg += issue_summary
        except Exception:
            err_msg += f" Response: {resp.text[:200]}"

        if debug_id:
            details["debug_id"] = debug_id
            err_msg += f" (PayPal Debug ID: {debug_id})"

        raise error_cls(sanitize_sensitive_data(err_msg), status_code=resp.status_code, details=details)

    @classmethod
    def create_draft_invoice(cls, invoice: Invoice, idempotency_key: Optional[str] = None) -> Dict[str, Any]:
        """
        Creates a draft invoice in PayPal Sandbox via POST /v2/invoicing/invoices.
        Checks for existing PayPal ID to prevent duplicates.
        """
        if invoice.paypal_invoice_id:
            raise PayPalDuplicateError(
                f"Invoice {invoice.invoice_number} already has an associated PayPal draft (ID: {invoice.paypal_invoice_id})."
            )

        token = cls.get_access_token()
        payload = cls.map_invoice_to_paypal_v2(invoice)
        url = f"{cls.get_base_url()}/v2/invoicing/invoices"
        req_id = idempotency_key or str(uuid.uuid4())

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "PayPal-Request-Id": req_id,
            "Prefer": "return=representation"
        }

        try:
            with httpx.Client(timeout=15.0) as client:
                resp = client.post(url, headers=headers, json=payload)
        except httpx.TimeoutException:
            raise PayPalError("Timeout while creating PayPal draft invoice.")
        except httpx.RequestError as exc:
            raise PayPalError(f"Network error while calling PayPal Invoicing API: {sanitize_sensitive_data(str(exc))}")

        if resp.status_code not in (200, 201):
            cls._handle_api_error_response(resp, "create PayPal draft invoice", error_cls=PayPalValidationError)

        created_data = resp.json()
        logger.info(f"Created PayPal draft invoice ID: {created_data.get('id')} for local invoice #{invoice.id}")
        return created_data

    @classmethod
    def send_invoice(cls, paypal_invoice_id: str, options: Optional[Dict[str, Any]] = None, idempotency_key: Optional[str] = None) -> Dict[str, Any]:
        """
        Dispatches a PayPal draft invoice via POST /v2/invoicing/invoices/{id}/send.
        Verifies resulting status by retrieving the updated invoice.
        """
        if not paypal_invoice_id:
            raise PayPalValidationError("paypal_invoice_id is required to dispatch an invoice.")

        token = cls.get_access_token()
        url = f"{cls.get_base_url()}/v2/invoicing/invoices/{paypal_invoice_id}/send"
        req_id = idempotency_key or str(uuid.uuid4())

        opts = options or {}
        payload = {
            "send_to_recipient": opts.get("send_to_recipient", True),
            "send_to_invoicer": opts.get("send_to_invoicer", False),
            "subject": opts.get("subject") or "Invoice from InvoicePilot AI",
            "note": opts.get("note") or "Thank you for your business. Please find your invoice attached."
        }

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "PayPal-Request-Id": req_id
        }

        try:
            with httpx.Client(timeout=15.0) as client:
                resp = client.post(url, headers=headers, json=payload)
        except httpx.TimeoutException:
            raise PayPalError("Timeout while sending PayPal invoice.")
        except httpx.RequestError as exc:
            raise PayPalError(f"Network error while dispatching invoice via PayPal: {sanitize_sensitive_data(str(exc))}")

        if resp.status_code not in (200, 202):
            cls._handle_api_error_response(resp, "send PayPal invoice", error_cls=PayPalError)

        # Retrieve verified status immediately after send
        verified_details = cls.get_invoice_details(paypal_invoice_id)
        return verified_details

    @classmethod
    def get_invoice_details(cls, paypal_invoice_id: str) -> Dict[str, Any]:
        """
        Retrieves remote invoice status and metadata from PayPal via GET /v2/invoicing/invoices/{id}.
        Source of truth for remote payment and delivery state.
        """
        if not paypal_invoice_id:
            raise PayPalValidationError("paypal_invoice_id is required to fetch invoice details.")

        token = cls.get_access_token()
        url = f"{cls.get_base_url()}/v2/invoicing/invoices/{paypal_invoice_id}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json"
        }

        try:
            with httpx.Client(timeout=15.0) as client:
                resp = client.get(url, headers=headers)
        except httpx.TimeoutException:
            raise PayPalError(f"Timeout while retrieving status for PayPal invoice {paypal_invoice_id}.")
        except httpx.RequestError as exc:
            raise PayPalError(f"Network error while retrieving PayPal invoice details: {sanitize_sensitive_data(str(exc))}")

        if resp.status_code != 200:
            cls._handle_api_error_response(resp, "retrieve PayPal invoice details", error_cls=PayPalError)

        data = resp.json()

        # Extract Payer View URL from links
        payer_url = None
        for link in data.get("links", []):
            if link.get("rel") in ("payer-view", "view"):
                payer_url = link.get("href")
                break

        data["_extracted_payer_url"] = payer_url
        return data

    @classmethod
    def verify_webhook_signature(
        cls,
        headers: Dict[str, str],
        raw_body_bytes: bytes,
        webhook_id: Optional[str] = None
    ) -> bool:
        """
        Validates an incoming PayPal webhook event signature using the official
        PayPal endpoint POST /v1/notifications/verify-webhook-signature.
        Requires PAYPAL_WEBHOOK_ID and the 5 cryptographic transmission headers.
        """
        active_webhook_id = (webhook_id or settings.PAYPAL_WEBHOOK_ID or "").strip()
        if not active_webhook_id:
            raise PayPalConfigurationError("PAYPAL_WEBHOOK_ID is not configured in environment settings.")

        # Header normalization (HTTP headers may be lower or mixed case)
        normalized_headers = {k.lower(): v for k, v in headers.items()}
        auth_algo = normalized_headers.get("paypal-auth-algo")
        cert_url = normalized_headers.get("paypal-cert-url")
        transmission_id = normalized_headers.get("paypal-transmission-id")
        transmission_sig = normalized_headers.get("paypal-transmission-sig")
        transmission_time = normalized_headers.get("paypal-transmission-time")

        missing_headers = []
        if not auth_algo: missing_headers.append("PAYPAL-AUTH-ALGO")
        if not cert_url: missing_headers.append("PAYPAL-CERT-URL")
        if not transmission_id: missing_headers.append("PAYPAL-TRANSMISSION-ID")
        if not transmission_sig: missing_headers.append("PAYPAL-TRANSMISSION-SIG")
        if not transmission_time: missing_headers.append("PAYPAL-TRANSMISSION-TIME")

        if missing_headers:
            raise PayPalValidationError(f"Missing required PayPal webhook signature headers: {', '.join(missing_headers)}")

        try:
            webhook_event_json = json.loads(raw_body_bytes.decode("utf-8"))
        except Exception as e:
            raise PayPalValidationError(f"Invalid JSON payload in webhook body: {str(e)}")

        token = cls.get_access_token()
        url = f"{cls.get_base_url()}/v1/notifications/verify-webhook-signature"
        req_headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json"
        }

        payload = {
            "auth_algo": auth_algo,
            "cert_url": cert_url,
            "transmission_id": transmission_id,
            "transmission_sig": transmission_sig,
            "transmission_time": transmission_time,
            "webhook_id": active_webhook_id,
            "webhook_event": webhook_event_json
        }

        try:
            with httpx.Client(timeout=15.0) as client:
                resp = client.post(url, headers=req_headers, json=payload)
        except httpx.TimeoutException:
            raise PayPalError("Timeout while contacting PayPal webhook verification service.")
        except httpx.RequestError as exc:
            raise PayPalError(f"Network error during webhook verification: {sanitize_sensitive_data(str(exc))}")

        if resp.status_code != 200:
            cls._handle_api_error_response(resp, "verify PayPal webhook signature", error_cls=PayPalError)

        result_data = resp.json()
        verification_status = result_data.get("verification_status", "FAILURE")
        logger.info("PayPal webhook signature verification result: %s", verification_status)
        return verification_status == "SUCCESS"

    @classmethod
    def send_invoice_reminder(
        cls,
        paypal_invoice_id: str,
        subject: Optional[str] = None,
        note: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Dispatches a reminder for an unpaid PayPal invoice via POST /v2/invoicing/invoices/{id}/remind.
        """
        if not paypal_invoice_id:
            raise PayPalValidationError("paypal_invoice_id is required to send a reminder.")

        token = cls.get_access_token()
        url = f"{cls.get_base_url()}/v2/invoicing/invoices/{paypal_invoice_id}/remind"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json"
        }

        payload = {
            "subject": subject or "Payment Reminder for Invoice",
            "note": note or "This is a friendly reminder that payment is due for your invoice.",
            "send_to_recipient": True,
            "send_to_invoicer": False
        }

        try:
            with httpx.Client(timeout=15.0) as client:
                resp = client.post(url, headers=headers, json=payload)
        except httpx.TimeoutException:
            raise PayPalError(f"Timeout while dispatching PayPal reminder for invoice {paypal_invoice_id}.")
        except httpx.RequestError as exc:
            raise PayPalError(f"Network error while dispatching PayPal reminder: {sanitize_sensitive_data(str(exc))}")

        if resp.status_code not in (200, 204):
            cls._handle_api_error_response(resp, "dispatch PayPal reminder", error_cls=PayPalError)

        return {"success": True, "paypal_invoice_id": paypal_invoice_id}

    @classmethod
    def get_payment_capture_details(cls, capture_id: str) -> Dict[str, Any]:
        """
        Retrieves payment capture information from PayPal via GET /v2/payments/captures/{capture_id}.
        """
        if not capture_id:
            raise PayPalValidationError("capture_id is required to fetch capture details.")

        token = cls.get_access_token()
        url = f"{cls.get_base_url()}/v2/payments/captures/{capture_id}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json"
        }

        try:
            with httpx.Client(timeout=15.0) as client:
                resp = client.get(url, headers=headers)
        except httpx.TimeoutException:
            raise PayPalError(f"Timeout while retrieving payment capture {capture_id}.")
        except httpx.RequestError as exc:
            raise PayPalError(f"Network error while retrieving capture details: {sanitize_sensitive_data(str(exc))}")

        if resp.status_code != 200:
            cls._handle_api_error_response(resp, "retrieve payment capture details", error_cls=PayPalError)

        return resp.json()

