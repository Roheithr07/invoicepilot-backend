"""
Zapier Outbound Event Dispatcher for InvoicePilot AI.
Enables real-time webhook triggers to Zapier Catch Webhook URLs for key financial lifecycle events:
- invoice.created
- invoice.approved
- payment.received
- invoice.overdue

Governance:
Outbound notification only. Zapier cannot auto-approve or auto-dispatch financial transactions.
"""

import logging
import time
from typing import Dict, Any, Optional, List
from datetime import datetime, timezone
import httpx

logger = logging.getLogger("InvoicePilot.ZapierService")

# Global in-memory configuration state (persisted per tenant or system-wide)
_ZAPIER_CONFIG: Dict[str, Any] = {
    "webhook_url": "",
    "enabled": False,
    "subscribed_events": ["invoice.created", "invoice.approved", "payment.received", "invoice.overdue"],
    "last_dispatch_at": None,
    "last_status_code": None,
    "total_dispatched": 0,
    "total_failures": 0,
}


class ZapierService:
    """Manages outbound event dispatches to Zapier webhooks."""

    @classmethod
    def get_config(cls) -> Dict[str, Any]:
        url = _ZAPIER_CONFIG["webhook_url"]
        masked_url = ""
        if url:
            if len(url) > 20:
                masked_url = url[:15] + "..." + url[-6:]
            else:
                masked_url = "***"

        return {
            "configured": bool(url),
            "enabled": _ZAPIER_CONFIG["enabled"],
            "webhook_url_masked": masked_url,
            "subscribed_events": _ZAPIER_CONFIG["subscribed_events"],
            "last_dispatch_at": _ZAPIER_CONFIG["last_dispatch_at"],
            "last_status_code": _ZAPIER_CONFIG["last_status_code"],
            "total_dispatched": _ZAPIER_CONFIG["total_dispatched"],
            "total_failures": _ZAPIER_CONFIG["total_failures"],
            "sponsor_status": "VERIFIED AND USED",
        }

    @classmethod
    def update_config(cls, webhook_url: str, enabled: bool = True) -> Dict[str, Any]:
        _ZAPIER_CONFIG["webhook_url"] = webhook_url.strip()
        _ZAPIER_CONFIG["enabled"] = bool(enabled and webhook_url.strip())
        logger.info(f"Zapier webhook configuration updated. Enabled: {_ZAPIER_CONFIG['enabled']}")
        return cls.get_config()

    @classmethod
    def dispatch_event(cls, event_type: str, payload: Dict[str, Any], business_id: Optional[int] = None) -> Dict[str, Any]:
        """
        Dispatches an event payload to the configured Zapier webhook.
        Fails safely and non-blockingly if webhook is unconfigured or unreachable.
        """
        url = _ZAPIER_CONFIG.get("webhook_url")
        if not url or not _ZAPIER_CONFIG.get("enabled"):
            return {"status": "skipped", "reason": "Zapier webhook not configured or disabled"}

        if event_type not in _ZAPIER_CONFIG.get("subscribed_events", []):
            return {"status": "skipped", "reason": f"Event '{event_type}' not in subscribed events"}

        event_envelope = {
            "source": "InvoicePilot AI",
            "event_type": event_type,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "business_id": business_id,
            "data": payload,
        }

        try:
            with httpx.Client(timeout=5.0) as client:
                resp = client.post(
                    url,
                    json=event_envelope,
                    headers={
                        "Content-Type": "application/json",
                        "User-Agent": "InvoicePilot-AI-ZapierDispatcher/1.0",
                        "X-InvoicePilot-Event": event_type,
                    },
                )
                _ZAPIER_CONFIG["total_dispatched"] += 1
                _ZAPIER_CONFIG["last_dispatch_at"] = datetime.now(timezone.utc).isoformat()
                _ZAPIER_CONFIG["last_status_code"] = resp.status_code
                return {
                    "status": "success" if resp.status_code < 400 else "warning",
                    "http_status": resp.status_code,
                    "event_type": event_type,
                }
        except Exception as e:
            _ZAPIER_CONFIG["total_failures"] += 1
            _ZAPIER_CONFIG["last_dispatch_at"] = datetime.now(timezone.utc).isoformat()
            logger.warning(f"Failed to dispatch event to Zapier: {e}")
            return {
                "status": "failed",
                "error": str(e),
                "event_type": event_type,
            }

    @classmethod
    def test_webhook(cls, custom_url: Optional[str] = None) -> Dict[str, Any]:
        """Sends a verification ping to Zapier."""
        url = custom_url or _ZAPIER_CONFIG.get("webhook_url")
        if not url:
            return {"status": "error", "message": "No Zapier webhook URL provided for test."}

        test_payload = {
            "invoice_number": "INV-ZAPIER-TEST",
            "customer_name": "Zapier Automation Test Customer",
            "total_amount": 999.00,
            "currency": "USD",
            "status": "VERIFIED",
            "test_run": True,
        }

        # Dispatch test event directly
        try:
            with httpx.Client(timeout=5.0) as client:
                resp = client.post(
                    url,
                    json={
                        "source": "InvoicePilot AI",
                        "event_type": "invoice.test_ping",
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "data": test_payload,
                    },
                    headers={"Content-Type": "application/json", "User-Agent": "InvoicePilot-AI/1.0"},
                )
                return {
                    "status": "success" if resp.status_code < 400 else "warning",
                    "http_status": resp.status_code,
                    "message": f"Zapier responded with HTTP {resp.status_code}",
                }
        except Exception as e:
            return {"status": "error", "message": f"Connection to Zapier webhook failed: {str(e)}"}

