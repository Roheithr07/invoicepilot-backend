"""
Authentication & Authorization Package for InvoicePilot AI.
"""

from app.auth.security import hash_password, verify_password, create_access_token, decode_access_token
from app.auth.permissions import ALL_PERMISSIONS, ROLE_PERMISSIONS, get_permissions_for_role, has_permission
from app.auth.dependencies import (
    get_current_user, get_current_active_user, require_permission,
    require_role, filter_by_business_scope, verify_business_ownership
)

