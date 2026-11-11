"""
Role-Based Access Control (RBAC) Permissions Matrix for InvoicePilot AI.
PayPal AI Hackathon 2026.
Defines granular permissions and role-to-permission mappings.
"""

from typing import Set, Dict, List
from app.models.models import UserRole

# All system granular permissions
ALL_PERMISSIONS = {
    # User Management
    "users.read",
    "users.create",
    "users.update",
    "users.delete",
    "roles.manage",

    # Customer Management
    "customers.read",
    "customers.create",
    "customers.update",
    "customers.delete",

    # Invoices & Billing
    "invoices.read",
    "invoices.create",
    "invoices.update",
    "invoices.approve",
    "invoices.send",
    "invoices.delete",

    # Financial Reconciliation & Payments
    "payments.read",
    "payments.reconcile",

    # Reports & Audit
    "reports.read",
    "reminders.create",
    "audit_logs.read",
    "settings.manage"
}

# Role to Permissions Mapping
ROLE_PERMISSIONS: Dict[UserRole, Set[str]] = {
    UserRole.SUPER_ADMIN: ALL_PERMISSIONS.copy(),

    UserRole.BUSINESS_OWNER: {
        # Can manage their business users & roles
        "users.read",
        "users.create",
        "users.update",
        # Full customer management
        "customers.read",
        "customers.create",
        "customers.update",
        "customers.delete",
        # Full invoice & billing lifecycle
        "invoices.read",
        "invoices.create",
        "invoices.update",
        "invoices.approve",
        "invoices.send",
        "invoices.delete",
        # Payments & reconciliation
        "payments.read",
        "payments.reconcile",
        # Analytics & administration
        "reports.read",
        "reminders.create",
        "audit_logs.read",
        "settings.manage"
    },

    UserRole.ACCOUNTANT: {
        # Customer inspection
        "customers.read",
        # Invoice auditing & draft management
        "invoices.read",
        "invoices.update",
        # Reconciliation and financial reports
        "payments.read",
        "payments.reconcile",
        "reports.read",
        "audit_logs.read"
        # Explicitly CANNOT: invoices.approve, invoices.send, users.create, roles.manage, settings.manage
    },

    UserRole.STAFF: {
        # Customer creation & viewing
        "customers.read",
        "customers.create",
        "customers.update",
        # Invoice creation & drafting
        "invoices.read",
        "invoices.create",
        "invoices.update",
        # Follow-up reminder drafting
        "reminders.create"
        # Explicitly CANNOT: invoices.approve, invoices.send, payments.reconcile, users.*, settings.manage
    }
}


def get_permissions_for_role(role: UserRole | str) -> List[str]:
    """Returns sorted list of permission keys for a given role."""
    if isinstance(role, str):
        try:
            role = UserRole(role.upper())
        except ValueError:
            return []
    perms = ROLE_PERMISSIONS.get(role, set())
    return sorted(list(perms))


def has_permission(role: UserRole | str, permission: str) -> bool:
    """Checks whether a role possesses a specific granular permission."""
    if isinstance(role, str):
        try:
            role = UserRole(role.upper())
        except ValueError:
            return False
    perms = ROLE_PERMISSIONS.get(role, set())
    return permission in perms

