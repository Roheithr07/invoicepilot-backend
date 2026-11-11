from app.routers.health import router as health_router
from app.routers.invoices import router as invoices_router
from app.routers.audits import router as audits_router
from app.routers.paypal import router as paypal_router, invoices_paypal_router
from app.routers.webhooks import router as webhooks_router
from app.routers.payments import router as payments_router
from app.routers.reconciliation import router as reconciliation_router
from app.routers.followups import router as followups_router
from app.routers.assistant import router as assistant_router
from app.routers.auth import router as auth_router
from app.routers.users import router as users_router
from app.routers.customers import router as customers_router
from app.routers.dashboard import router as dashboard_router
from app.routers.events import router as events_router
from app.routers.integrations import router as integrations_router

__all__ = [
    "health_router",
    "invoices_router",
    "audits_router",
    "paypal_router",
    "invoices_paypal_router",
    "webhooks_router",
    "payments_router",
    "reconciliation_router",
    "followups_router",
    "assistant_router",
    "auth_router",
    "users_router",
    "customers_router",
    "dashboard_router",
    "events_router",
    "integrations_router"
]
