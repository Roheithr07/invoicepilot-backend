"""
Authentication Router for InvoicePilot AI.
Provides login, session validation, user profile inspection, and role matrix endpoints.
"""

from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Any
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.models import User, Business, UserRole, ActivityLog
from app.auth.security import hash_password, verify_password, create_access_token
from app.auth.permissions import get_permissions_for_role, ROLE_PERMISSIONS
from app.auth.dependencies import get_current_active_user

router = APIRouter(prefix="/auth", tags=["Authentication & RBAC"])


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class UserProfileResponse(BaseModel):
    id: int
    name: str
    email: str
    role: str
    business_id: Optional[int] = None
    business_name: Optional[str] = None
    currency: Optional[str] = "USD"
    permissions: List[str]
    is_active: bool

    class Config:
        from_attributes = True


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserProfileResponse


@router.post("/login", response_model=LoginResponse)
def login(payload: LoginRequest, db: Session = Depends(get_db)):
    """
    Authenticates user by email and password using PBKDF2-HMAC-SHA256 verification.
    Issues signed JWT access token containing user identity and role claims.
    """
    email_clean = payload.email.strip().lower()
    user = db.query(User).filter(User.email == email_clean).first()
    
    if not user or not user.hashed_password:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password.",
            headers={"WWW-Authenticate": "Bearer"}
        )

    if not verify_password(payload.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password.",
            headers={"WWW-Authenticate": "Bearer"}
        )

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User account is deactivated. Contact an administrator."
        )

    # Fetch business metadata
    b_name = user.business.name if user.business else "Master System"
    b_currency = user.business.currency if user.business else "USD"

    # Generate JWT
    token_data = {
        "sub": str(user.id),
        "email": user.email,
        "name": user.name,
        "role": user.role.value,
        "business_id": user.business_id
    }
    access_token = create_access_token(token_data)

    # Log login activity
    log = ActivityLog(
        action="USER_LOGIN",
        entity_type="user",
        entity_id=str(user.id),
        description=f"User {user.email} logged in with role {user.role.value} (Business: {b_name})."
    )
    db.add(log)
    db.commit()

    user_resp = UserProfileResponse(
        id=user.id,
        name=user.name,
        email=user.email,
        role=user.role.value,
        business_id=user.business_id,
        business_name=b_name,
        currency=b_currency,
        permissions=get_permissions_for_role(user.role),
        is_active=user.is_active
    )

    return LoginResponse(access_token=access_token, user=user_resp)


@router.get("/me", response_model=UserProfileResponse)
def get_current_user_profile(current_user: User = Depends(get_current_active_user)):
    """Returns profile and permissions of the currently authenticated user."""
    b_name = current_user.business.name if current_user.business else "Master System"
    b_currency = current_user.business.currency if current_user.business else "USD"

    return UserProfileResponse(
        id=current_user.id,
        name=current_user.name,
        email=current_user.email,
        role=current_user.role.value,
        business_id=current_user.business_id,
        business_name=b_name,
        currency=b_currency,
        permissions=get_permissions_for_role(current_user.role),
        is_active=current_user.is_active
    )


@router.post("/logout")
def logout(current_user: User = Depends(get_current_active_user)):
    """Logs out the user and acknowledges session termination."""
    return {"status": "success", "message": f"Successfully logged out {current_user.email}."}


@router.get("/roles")
def get_roles_and_permissions():
    """Returns public specification of the 4 RBAC roles and their granular permissions."""
    return {
        role.value: sorted(list(perms))
        for role, perms in ROLE_PERMISSIONS.items()
    }

