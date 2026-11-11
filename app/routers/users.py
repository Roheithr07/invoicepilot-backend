"""
User Management Router for InvoicePilot AI.
Provides enterprise multi-tenant user administration, role assignment, and access audits.
"""

from typing import Optional, List
from fastapi import APIRouter, Depends, HTTPException, status, Query
from pydantic import BaseModel, EmailStr
from sqlalchemy.orm import Session
from sqlalchemy import desc

from app.database import get_db
from app.models.models import User, Business, UserRole, ActivityLog
from app.auth.security import hash_password
from app.auth.permissions import get_permissions_for_role
from app.auth.dependencies import (
    get_current_active_user, require_permission,
    filter_by_business_scope, verify_business_ownership
)

router = APIRouter(prefix="/users", tags=["User Management"])


class UserItemResponse(BaseModel):
    id: int
    name: str
    email: str
    role: str
    business_id: Optional[int] = None
    business_name: Optional[str] = None
    is_active: bool
    created_at: Optional[str] = None

    class Config:
        from_attributes = True


class CreateUserRequest(BaseModel):
    name: str
    email: EmailStr
    password: str
    role: str  # SUPER_ADMIN, BUSINESS_OWNER, ACCOUNTANT, STAFF
    business_id: Optional[int] = None


class UpdateUserRequest(BaseModel):
    name: Optional[str] = None
    role: Optional[str] = None
    is_active: Optional[bool] = None
    business_id: Optional[int] = None


@router.get("", response_model=List[UserItemResponse])
def list_users(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("users.read"))
):
    """
    Lists users.
    - SUPER_ADMIN: returns all users across all businesses.
    - BUSINESS_OWNER / permitted: returns only users belonging to current_user.business_id.
    """
    query = db.query(User)
    query = filter_by_business_scope(query, User, current_user)
    users = query.order_by(desc(User.created_at)).all()

    results = []
    for u in users:
        results.append(UserItemResponse(
            id=u.id,
            name=u.name,
            email=u.email,
            role=u.role.value if hasattr(u.role, 'value') else str(u.role),
            business_id=u.business_id,
            business_name=u.business.name if u.business else "Master System",
            is_active=u.is_active,
            created_at=u.created_at.isoformat() if u.created_at else None
        ))
    return results


@router.post("", response_model=UserItemResponse, status_code=status.HTTP_201_CREATED)
def create_user(
    payload: CreateUserRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("users.create"))
):
    """
    Creates a new user account with role-based guardrails:
    - Only SUPER_ADMIN can create SUPER_ADMIN or assign to other businesses.
    - BUSINESS_OWNER can only create ACCOUNTANT or STAFF within their own business.
    """
    try:
        assigned_role = UserRole(payload.role.upper())
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid role '{payload.role}'. Allowed roles: {[r.value for r in UserRole]}."
        )

    # Enforce role escalation defenses
    if current_user.role != UserRole.SUPER_ADMIN:
        if assigned_role in (UserRole.SUPER_ADMIN, UserRole.BUSINESS_OWNER):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Permission Denied: Only SUPER_ADMIN can create administrative or owner accounts."
            )
        target_business_id = current_user.business_id
    else:
        target_business_id = payload.business_id or current_user.business_id or 1

    clean_email = payload.email.strip().lower()
    existing = db.query(User).filter(User.email == clean_email).first()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"User with email '{clean_email}' already exists."
        )

    # Verify business exists
    business = db.query(Business).filter(Business.id == target_business_id).first()
    if not business:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Business ID {target_business_id} does not exist."
        )

    hashed_pwd = hash_password(payload.password)
    new_user = User(
        name=payload.name.strip(),
        email=clean_email,
        hashed_password=hashed_pwd,
        role=assigned_role,
        business_id=target_business_id,
        is_active=True
    )
    db.add(new_user)
    db.commit()
    db.refresh(new_user)

    log = ActivityLog(
        action="USER_CREATED",
        entity_type="user",
        entity_id=str(new_user.id),
        description=f"Created user {new_user.email} with role {new_user.role.value} for business {business.name} by {current_user.email}."
    )
    db.add(log)
    db.commit()

    return UserItemResponse(
        id=new_user.id,
        name=new_user.name,
        email=new_user.email,
        role=new_user.role.value,
        business_id=new_user.business_id,
        business_name=business.name,
        is_active=new_user.is_active,
        created_at=new_user.created_at.isoformat() if new_user.created_at else None
    )


@router.put("/{user_id}", response_model=UserItemResponse)
def update_user(
    user_id: int,
    payload: UpdateUserRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("users.update"))
):
    """Updates user name, role, or active status with business isolation."""
    target_user = db.query(User).filter(User.id == user_id).first()
    if not target_user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"User ID {user_id} not found.")

    verify_business_ownership(target_user, current_user, "User")

    if payload.role:
        try:
            new_role = UserRole(payload.role.upper())
            if current_user.role != UserRole.SUPER_ADMIN and new_role in (UserRole.SUPER_ADMIN, UserRole.BUSINESS_OWNER):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Permission Denied: Only SUPER_ADMIN can assign administrative roles."
                )
            target_user.role = new_role
        except ValueError:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Invalid role '{payload.role}'.")

    if payload.name:
        target_user.name = payload.name.strip()
    if payload.is_active is not None:
        target_user.is_active = payload.is_active

    db.commit()
    db.refresh(target_user)

    return UserItemResponse(
        id=target_user.id,
        name=target_user.name,
        email=target_user.email,
        role=target_user.role.value if hasattr(target_user.role, 'value') else str(target_user.role),
        business_id=target_user.business_id,
        business_name=target_user.business.name if target_user.business else "Master System",
        is_active=target_user.is_active,
        created_at=target_user.created_at.isoformat() if target_user.created_at else None
    )


@router.delete("/{user_id}")
def delete_user(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("users.delete"))
):
    """Deletes or deactivates a user account."""
    if user_id == current_user.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Self-Deletion Prevention: You cannot delete your own authenticated account."
        )

    target_user = db.query(User).filter(User.id == user_id).first()
    if not target_user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"User ID {user_id} not found.")

    verify_business_ownership(target_user, current_user, "User")

    target_user.is_active = False
    db.commit()

    return {"status": "success", "message": f"User {target_user.email} has been deactivated."}

