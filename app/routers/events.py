"""
Real-Time Events & WebSocket Router for InvoicePilot AI.
Provides WebSocket stream /api/ws and HTTP polling fallback /api/events/recent.
"""

from typing import Optional, List, Dict, Any
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.models import User, UserRole
from app.auth.security import decode_access_token
from app.services.websocket_manager import ws_manager
from app.auth.dependencies import get_current_active_user

router = APIRouter(tags=["Real-Time WebSockets & Events"])


@router.websocket("/ws")
async def websocket_endpoint(
    websocket: WebSocket,
    token: Optional[str] = Query(None),
    db: Session = Depends(get_db)
):
    """
    Authenticated WebSocket connection endpoint.
    Clients supply JWT token in query parameter (?token=<token>).
    Connections are registered and isolated by tenant business_id.
    """
    user_id = 0
    business_id = 1
    role = "STAFF"

    if token:
        payload = decode_access_token(token)
        if payload and "sub" in payload:
            user = db.query(User).filter(User.id == int(payload["sub"])).first()
            if user and user.is_active:
                user_id = user.id
                business_id = user.business_id
                role = user.role.value if hasattr(user.role, 'value') else str(user.role)

    # Accept and register connection
    await ws_manager.connect(websocket, user_id=user_id, business_id=business_id, role=role)

    try:
        while True:
            # Keep-alive loop and listen for client pings
            data_text = await websocket.receive_text()
            if data_text == "ping":
                await websocket.send_text('{"event": "pong"}')
    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)
    except Exception:
        ws_manager.disconnect(websocket)


@router.get("/events/recent")
def get_recent_events(
    since: Optional[str] = Query(None),
    current_user: User = Depends(get_current_active_user)
):
    """
    HTTP Polling fallback endpoint for clients where WebSockets are unavailable or reconnecting.
    Returns events scoped to current_user.business_id.
    """
    role_str = current_user.role.value if hasattr(current_user.role, 'value') else str(current_user.role)
    events = ws_manager.get_recent_events(
        business_id=current_user.business_id,
        role=role_str,
        since=since
    )
    return {
        "status": "success",
        "count": len(events),
        "events": events
    }

