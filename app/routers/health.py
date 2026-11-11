"""
Health Check and System Diagnostics Router for InvoicePilot AI.
"""

from datetime import datetime, timezone
from fastapi import APIRouter, Depends, status, Response
from sqlalchemy.orm import Session
from sqlalchemy import text
from app.database import get_db
from app.config import settings
from app.schemas.health import HealthCheckResponse
from app.services.storage import storage_service
from app.services.paypal_service import PayPalService
import app

router = APIRouter(tags=["System & Diagnostics"])


@router.get("/health", response_model=HealthCheckResponse, status_code=status.HTTP_200_OK)
def get_health(db: Session = Depends(get_db)):
    """
    Comprehensive system health-check endpoint.
    Verifies database connectivity, configuration parameters, and runtime environment.
    """
    db_status = "connected"
    db_details = {}
    
    try:
        # Perform quick verification query
        db.execute(text("SELECT 1"))
        db_details["engine"] = settings.DATABASE_URL.split(":///")[0]
        db_details["connection"] = "active"
    except Exception as e:
        db_status = f"unhealthy: {str(e)}"
        db_details["connection"] = "failed"

    # Storage subsystem check
    storage_ok = storage_service.is_healthy()
    storage_details = {
        "status": "connected" if storage_ok else "unhealthy",
        "backend": storage_service.get_backend_type()
    }

    # PayPal subsystem check
    paypal_configured = PayPalService.is_configured()
    paypal_details = {
        "configured": paypal_configured,
        "mode": settings.PAYPAL_MODE,
        "masked_client_id": PayPalService.get_masked_client_id(),
        "webhook_configured": PayPalService.is_webhook_configured()
    }

    overall_status = "healthy" if (db_status == "connected" and storage_ok) else "degraded"

    return HealthCheckResponse(
        status=overall_status,
        app_name=settings.APP_NAME,
        environment=settings.APP_ENV,
        version=getattr(app, "__version__", "0.1.0"),
        database=db_status,
        timestamp=datetime.now(timezone.utc),
        details={
            "database": db_details,
            "storage": storage_details,
            "paypal": paypal_details,
            "paypal_mode": settings.PAYPAL_MODE,
            "ai_provider": settings.AI_PROVIDER,
        }
    )


@router.get("/ai/health", status_code=status.HTTP_200_OK)
def get_ai_health():
    """
    Returns AI provider status, active model, and deterministic fallback readiness.
    """
    from app.services.ai_provider import AIProviderManager
    return AIProviderManager.get_health()


@router.get("/ready", status_code=status.HTTP_200_OK)
def get_readiness(db: Session = Depends(get_db)):
    """
    Kubernetes / Container Readiness probe.
    Returns HTTP 200 if database and storage are ready to accept traffic.
    """
    try:
        db.execute(text("SELECT 1"))
        if not storage_service.is_healthy():
            return Response(content='{"status": "storage_not_ready"}', status_code=status.HTTP_503_SERVICE_UNAVAILABLE, media_type="application/json")
        return {"status": "ready", "timestamp": datetime.now(timezone.utc).isoformat()}
    except Exception as e:
        return Response(content=f'{{"status": "db_not_ready", "error": "{str(e)}"}}', status_code=status.HTTP_503_SERVICE_UNAVAILABLE, media_type="application/json")


@router.get("/", status_code=status.HTTP_200_OK)
def root():
    """Root endpoint welcoming clients to InvoicePilot AI."""
    return {
        "message": f"Welcome to {settings.APP_NAME} API",
        "status": "online",
        "docs_url": "/docs",
        "health_url": "/api/health"
    }

