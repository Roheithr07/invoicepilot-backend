"""
Real-Time WebSocket & Event Bus Infrastructure for InvoicePilot AI.
Provides authenticated WebSocket connections, multi-tenant scoped broadcasts,
and an in-memory event buffer with polling fallback.
"""

import json
import logging
import asyncio
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any, Set
from fastapi import WebSocket, WebSocketDisconnect

logger = logging.getLogger("InvoicePilot.WebSocketManager")


class ConnectionManager:
    """Manages active WebSocket connections grouped by user and business."""

    def __init__(self):
        # All active WebSocket connections
        self.active_connections: Set[WebSocket] = set()
        # Map websocket to (user_id, business_id, role)
        self.connection_meta: Dict[WebSocket, Dict[str, Any]] = {}
        # Recent events buffer for polling fallback (max 100 events)
        self._recent_events: List[Dict[str, Any]] = []
        self._max_events = 100

    async def connect(self, websocket: WebSocket, user_id: int, business_id: Optional[int], role: str):
        await websocket.accept()
        self.active_connections.add(websocket)
        self.connection_meta[websocket] = {
            "user_id": user_id,
            "business_id": business_id,
            "role": role,
            "connected_at": datetime.now(timezone.utc).isoformat()
        }
        logger.info(f"WebSocket client connected: user_id={user_id}, business_id={business_id}, role={role}")

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)
        meta = self.connection_meta.pop(websocket, None)
        if meta:
            logger.info(f"WebSocket client disconnected: user_id={meta.get('user_id')}")

    def broadcast_event(
        self,
        event_type: str,
        data: Dict[str, Any],
        business_id: Optional[int] = None,
        recipient_user_id: Optional[int] = None
    ):
        """
        Broadcasts event to authorized subscribers.
        Works seamlessly in both sync route handlers and async loops.
        - If business_id is set: only users with matching business_id (or SUPER_ADMIN) receive it.
        - If recipient_user_id is set: only that user receives it.
        """
        event_payload = {
            "event": event_type,
            "data": data,
            "business_id": business_id,
            "timestamp": datetime.now(timezone.utc).isoformat()
        }

        # Store in recent events buffer (immediately accessible to polling fallback)
        self._recent_events.append(event_payload)
        if len(self._recent_events) > self._max_events:
            self._recent_events.pop(0)

        # If an asyncio event loop is running, asynchronously deliver to active WebSockets
        try:
            loop = asyncio.get_running_loop()
            if loop.is_running():
                loop.create_task(self._deliver_to_clients(event_payload, business_id, recipient_user_id))
        except RuntimeError:
            # No running loop (e.g. in synchronous test suite)
            pass

    async def _deliver_to_clients(
        self,
        event_payload: Dict[str, Any],
        business_id: Optional[int] = None,
        recipient_user_id: Optional[int] = None
    ):
        message_text = json.dumps(event_payload)
        stale_connections = []

        for ws in list(self.active_connections):
            meta = self.connection_meta.get(ws, {})
            u_role = meta.get("role")
            u_bid = meta.get("business_id")
            u_id = meta.get("user_id")

            # Check recipient targeting
            if recipient_user_id is not None and u_id != recipient_user_id:
                continue

            # Check business isolation: SUPER_ADMIN sees all; others only see their business
            if u_role != "SUPER_ADMIN" and business_id is not None and u_bid != business_id:
                continue

            try:
                await ws.send_text(message_text)
            except Exception as e:
                logger.warning(f"Error sending to WebSocket {u_id}: {e}")
                stale_connections.append(ws)

        for ws in stale_connections:
            self.disconnect(ws)

    def get_recent_events(self, business_id: Optional[int] = None, role: str = "STAFF", since: Optional[str] = None) -> List[Dict[str, Any]]:
        """Returns recent events matching business_id or all for SUPER_ADMIN, filtered by since timestamp."""
        results = []
        since_dt = None
        if since:
            try:
                since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
            except Exception:
                pass

        for ev in self._recent_events:
            if since_dt:
                ev_time = datetime.fromisoformat(ev["timestamp"].replace("Z", "+00:00"))
                if ev_time <= since_dt:
                    continue

            ev_bid = ev.get("business_id")
            if role == "SUPER_ADMIN" or ev_bid is None or business_id is None or ev_bid == business_id:
                results.append(ev)

        return results


# Global singleton manager
ws_manager = ConnectionManager()
