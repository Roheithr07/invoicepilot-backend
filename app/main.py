"""
InvoicePilot AI — Main FastAPI Application Entrypoint.
PayPal AI Hackathon 2026.
"""

from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from app.config import settings
from app.database import Base, engine
from app.routers import (
    health_router, invoices_router, audits_router,
    paypal_router, invoices_paypal_router,
    webhooks_router, payments_router,
    reconciliation_router, followups_router,
    assistant_router, auth_router, users_router,
    customers_router, dashboard_router, events_router,
    integrations_router
)
from app.migrate import sync_schema
from app.services.paypal_service import sanitize_sensitive_data
import app.models  # Register all SQLAlchemy models


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: Ensure database tables and schema migrations are synchronized in SQLite
    sync_schema()
    yield
    # Shutdown logic (if any)


app = FastAPI(
    title=settings.APP_NAME,
    description="Intelligent AI-powered invoice management and PayPal payment automation platform.",
    version="0.1.0",
    lifespan=lifespan
)

# Security Headers Middleware
@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    return response

# Configure CORS Middleware for Frontend Access
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

# Register Routers
app.include_router(health_router, prefix="/api")
app.include_router(health_router, prefix="/api/v1")
app.include_router(auth_router, prefix="/api")
app.include_router(users_router, prefix="/api")
app.include_router(customers_router, prefix="/api")
app.include_router(dashboard_router, prefix="/api")
app.include_router(events_router, prefix="/api")
app.include_router(events_router, prefix="")
app.include_router(invoices_router, prefix="/api")
app.include_router(audits_router, prefix="/api")
app.include_router(paypal_router, prefix="/api")
app.include_router(invoices_paypal_router, prefix="/api")
app.include_router(webhooks_router, prefix="/api")
app.include_router(payments_router, prefix="/api")
app.include_router(reconciliation_router, prefix="/api")
app.include_router(followups_router, prefix="/api")
app.include_router(assistant_router, prefix="/api")
app.include_router(integrations_router, prefix="/api")
app.include_router(health_router, prefix="")


if __name__ == "__main__":
    import uvicorn
    import os
    port = int(os.environ.get("PORT", settings.APP_PORT))
    uvicorn.run(
        "app.main:app",
        host=settings.APP_HOST,
        port=port,
        reload=(settings.APP_ENV == "development")
    )

