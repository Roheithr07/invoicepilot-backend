"""
Customer Management Router for InvoicePilot AI.
Provides complete CRUD operations with strict multi-tenant isolation and role-based permissions.
"""

from typing import Optional, List
from fastapi import APIRouter, Depends, HTTPException, status, Query
from pydantic import BaseModel, EmailStr
from sqlalchemy.orm import Session
from sqlalchemy import desc, or_

from app.database import get_db
from app.models.models import Customer, Invoice, User, ActivityLog
from app.auth.dependencies import (
    get_current_active_user, require_permission,
    filter_by_business_scope, verify_business_ownership
)

router = APIRouter(prefix="/customers", tags=["Customer Management"])


class CustomerBase(BaseModel):
    name: str
    email: EmailStr
    phone: Optional[str] = None
    address: Optional[str] = None


class CustomerCreateInput(CustomerBase):
    pass


class CustomerUpdateInput(BaseModel):
    name: Optional[str] = None
    email: Optional[EmailStr] = None
    phone: Optional[str] = None
    address: Optional[str] = None


class CustomerResponse(CustomerBase):
    id: int
    business_id: Optional[int] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    invoices_count: Optional[int] = 0

    class Config:
        from_attributes = True


class CustomerListResponse(BaseModel):
    items: List[CustomerResponse]
    total: int
    page: int
    page_size: int
    total_pages: int


@router.get("", response_model=CustomerListResponse)
def list_customers(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    search: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("customers.read"))
):
    """Lists customers scoped to the authenticated user's business organization."""
    query = db.query(Customer)
    query = filter_by_business_scope(query, Customer, current_user)

    if search:
        s = f"%{search.strip()}%"
        query = query.filter(or_(Customer.name.ilike(s), Customer.email.ilike(s), Customer.phone.ilike(s)))

    total = query.count()
    customers = query.order_by(desc(Customer.created_at)).offset((page - 1) * page_size).limit(page_size).all()

    items = []
    for c in customers:
        inv_count = db.query(Invoice).filter(Invoice.customer_id == c.id, Invoice.is_deleted == False).count()
        items.append(CustomerResponse(
            id=c.id,
            name=c.name,
            email=c.email,
            phone=c.phone,
            address=c.address,
            business_id=c.business_id,
            created_at=c.created_at.isoformat() if c.created_at else None,
            updated_at=c.updated_at.isoformat() if c.updated_at else None,
            invoices_count=inv_count
        ))

    total_pages = (total + page_size - 1) // page_size if total > 0 else 1
    return CustomerListResponse(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
        total_pages=total_pages
    )


@router.post("", response_model=CustomerResponse, status_code=status.HTTP_201_CREATED)
def create_customer(
    payload: CustomerCreateInput,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("customers.create"))
):
    """Creates a new customer record linked to the authenticated user's business."""
    target_business_id = current_user.business_id or 1

    # Check for duplicate email within the same business
    existing = db.query(Customer).filter(
        Customer.email == payload.email.strip().lower(),
        Customer.business_id == target_business_id
    ).first()

    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Customer with email '{payload.email}' already exists in your organization."
        )

    customer = Customer(
        name=payload.name.strip(),
        email=payload.email.strip().lower(),
        phone=payload.phone.strip() if payload.phone else None,
        address=payload.address.strip() if payload.address else None,
        business_id=target_business_id
    )
    db.add(customer)
    db.commit()
    db.refresh(customer)

    log = ActivityLog(
        action="CUSTOMER_CREATED",
        entity_type="customer",
        entity_id=str(customer.id),
        description=f"Created customer '{customer.name}' ({customer.email}) by {current_user.email}."
    )
    db.add(log)
    db.commit()

    return CustomerResponse(
        id=customer.id,
        name=customer.name,
        email=customer.email,
        phone=customer.phone,
        address=customer.address,
        business_id=customer.business_id,
        created_at=customer.created_at.isoformat() if customer.created_at else None,
        updated_at=customer.updated_at.isoformat() if customer.updated_at else None,
        invoices_count=0
    )


@router.get("/{customer_id}", response_model=CustomerResponse)
def get_customer(
    customer_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("customers.read"))
):
    """Retrieves customer details with business boundary validation."""
    customer = db.query(Customer).filter(Customer.id == customer_id).first()
    if not customer:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Customer ID {customer_id} not found.")

    verify_business_ownership(customer, current_user, "Customer")
    inv_count = db.query(Invoice).filter(Invoice.customer_id == customer.id, Invoice.is_deleted == False).count()

    return CustomerResponse(
        id=customer.id,
        name=customer.name,
        email=customer.email,
        phone=customer.phone,
        address=customer.address,
        business_id=customer.business_id,
        created_at=customer.created_at.isoformat() if customer.created_at else None,
        updated_at=customer.updated_at.isoformat() if customer.updated_at else None,
        invoices_count=inv_count
    )


@router.put("/{customer_id}", response_model=CustomerResponse)
def update_customer(
    customer_id: int,
    payload: CustomerUpdateInput,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("customers.update"))
):
    """Updates customer details with business boundary validation."""
    customer = db.query(Customer).filter(Customer.id == customer_id).first()
    if not customer:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Customer ID {customer_id} not found.")

    verify_business_ownership(customer, current_user, "Customer")

    if payload.name:
        customer.name = payload.name.strip()
    if payload.email:
        clean_email = payload.email.strip().lower()
        # Verify no duplicate email in the same business
        dup = db.query(Customer).filter(
            Customer.email == clean_email,
            Customer.business_id == customer.business_id,
            Customer.id != customer.id
        ).first()
        if dup:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Customer email '{clean_email}' already in use by another record."
            )
        customer.email = clean_email
    if payload.phone is not None:
        customer.phone = payload.phone.strip() if payload.phone else None
    if payload.address is not None:
        customer.address = payload.address.strip() if payload.address else None

    db.commit()
    db.refresh(customer)

    return CustomerResponse(
        id=customer.id,
        name=customer.name,
        email=customer.email,
        phone=customer.phone,
        address=customer.address,
        business_id=customer.business_id,
        created_at=customer.created_at.isoformat() if customer.created_at else None,
        updated_at=customer.updated_at.isoformat() if customer.updated_at else None,
        invoices_count=0
    )


@router.delete("/{customer_id}")
def delete_customer(
    customer_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("customers.delete"))
):
    """Deletes customer record if authorized and has no active invoices."""
    customer = db.query(Customer).filter(Customer.id == customer_id).first()
    if not customer:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Customer ID {customer_id} not found.")

    verify_business_ownership(customer, current_user, "Customer")

    active_invoices = db.query(Invoice).filter(
        Invoice.customer_id == customer.id,
        Invoice.is_deleted == False
    ).count()

    if active_invoices > 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot delete customer: {active_invoices} active invoice(s) are associated with this client."
        )

    db.delete(customer)
    db.commit()

    return {"status": "success", "message": f"Customer '{customer.name}' deleted successfully."}

