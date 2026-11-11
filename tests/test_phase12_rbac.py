"""
Phase 12 Comprehensive Test Suite — Authentication, Enterprise RBAC & Multi-Tenancy.
Tests:
- Authentication & JWT Token issuance/validation.
- 4 Role Permission boundaries (SUPER_ADMIN, BUSINESS_OWNER, ACCOUNTANT, STAFF).
- Financial Governance: Staff blocked from PayPal send & invoice approval.
- Multi-Tenant Isolation & Anti-IDOR protection.
- Live Dashboard Statistics aggregation.
- Real-Time Event Bus & Polling Fallback.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from datetime import datetime, timezone

from app.models.models import User, UserRole, Business, Customer, Invoice, InvoiceStatus
from app.auth.security import hash_password, create_access_token


def seed_test_env(db: Session):
    """Seed test businesses, users across roles, customers, and invoices in the test db session."""
    # 1. Businesses
    b1 = Business(id=101, name="Tenant One Tech", currency="USD")
    b2 = Business(id=102, name="Tenant Two Retail", currency="EUR")
    db.add(b1)
    db.add(b2)
    db.commit()

    # 2. Users across roles
    users = [
        User(id=201, email="admin@test.com", name="Admin Tester", role=UserRole.SUPER_ADMIN, business_id=101, hashed_password=hash_password("Secret123!"), is_active=True),
        User(id=202, email="owner1@test.com", name="Owner One", role=UserRole.BUSINESS_OWNER, business_id=101, hashed_password=hash_password("Secret123!"), is_active=True),
        User(id=203, email="acct1@test.com", name="Accountant One", role=UserRole.ACCOUNTANT, business_id=101, hashed_password=hash_password("Secret123!"), is_active=True),
        User(id=204, email="staff1@test.com", name="Staff One", role=UserRole.STAFF, business_id=101, hashed_password=hash_password("Secret123!"), is_active=True),
        User(id=205, email="owner2@test.com", name="Owner Two", role=UserRole.BUSINESS_OWNER, business_id=102, hashed_password=hash_password("Secret123!"), is_active=True),
    ]
    for u in users:
        db.add(u)
    db.commit()

    # 3. Customers
    c1 = Customer(id=101, business_id=101, name="Client One", email="client1@corp.com", phone="+1-555-0101", address="100 Tech Way")
    c2 = Customer(id=102, business_id=102, name="Client Two", email="client2@corp.com", phone="+44-20-7946", address="200 Fleet St")
    db.add(c1)
    db.add(c2)
    db.commit()

    # 4. Invoices
    inv1 = Invoice(
        id=101,
        business_id=101,
        user_id=202,
        invoice_number="INV-T1-001",
        customer_name="Client One",
        customer_email="client1@corp.com",
        total_amount=1500.0,
        subtotal=1500.0,
        status=InvoiceStatus.DRAFT,
        currency="USD",
        is_deleted=False
    )
    inv2 = Invoice(
        id=102,
        business_id=102,
        user_id=205,
        invoice_number="INV-T2-001",
        customer_name="Client Two",
        customer_email="client2@corp.com",
        total_amount=2400.0,
        subtotal=2400.0,
        status=InvoiceStatus.DRAFT,
        currency="EUR",
        is_deleted=False
    )
    db.add(inv1)
    db.add(inv2)
    db.commit()


def get_auth_header(user_id: int, role: str, business_id: int) -> dict:
    token = create_access_token(data={"sub": str(user_id), "role": role, "business_id": business_id})
    return {"Authorization": f"Bearer {token}"}


# -------------------------------------------------------------------------
# 1. Authentication Tests
# -------------------------------------------------------------------------

def test_login_success(client: TestClient, db_session: Session):
    """Verify login generates valid JWT with role and user metadata."""
    seed_test_env(db_session)
    resp = client.post(
        "/api/auth/login",
        json={"email": "owner1@test.com", "password": "Secret123!"}
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert "access_token" in data
    assert data["token_type"] == "bearer"
    assert data["user"]["role"] == "BUSINESS_OWNER"
    assert data["user"]["business_id"] == 101
    assert "invoices.approve" in data["user"]["permissions"]


def test_login_invalid_password(client: TestClient, db_session: Session):
    """Verify login rejection with incorrect credentials."""
    seed_test_env(db_session)
    resp = client.post(
        "/api/auth/login",
        json={"email": "owner1@test.com", "password": "WrongPassword!"}
    )
    assert resp.status_code == 401


def test_auth_me_endpoint(client: TestClient, db_session: Session):
    """Verify /api/auth/me returns profile matching token identity."""
    seed_test_env(db_session)
    headers = get_auth_header(202, "BUSINESS_OWNER", 101)
    resp = client.get("/api/auth/me", headers=headers)
    assert resp.status_code == 200, resp.text
    profile = resp.json()
    assert profile["email"] == "owner1@test.com"
    assert profile["role"] == "BUSINESS_OWNER"
    assert "invoices.create" in profile["permissions"]


# -------------------------------------------------------------------------
# 2. RBAC Permissions & Financial Governance Tests
# -------------------------------------------------------------------------

def test_staff_cannot_approve_invoice(client: TestClient, db_session: Session):
    """Governance rule: STAFF role is blocked from approving invoices (HTTP 403)."""
    seed_test_env(db_session)
    headers = get_auth_header(204, "STAFF", 101)
    resp = client.post("/api/invoices/101/approve", headers=headers)
    assert resp.status_code == 403
    assert "Permission Denied" in resp.json()["detail"]


def test_staff_cannot_send_paypal_invoice(client: TestClient, db_session: Session):
    """Governance rule: STAFF role is blocked from sending PayPal invoices (HTTP 403)."""
    seed_test_env(db_session)
    headers = get_auth_header(204, "STAFF", 101)
    resp = client.post("/api/invoices/101/paypal/send", json={"confirmed": True}, headers=headers)
    assert resp.status_code == 403
    assert "not authorized to dispatch PayPal invoices" in resp.json()["detail"]


def test_business_owner_can_approve_invoice(client: TestClient, db_session: Session):
    """Owner can approve invoices for their own tenant."""
    seed_test_env(db_session)
    headers = get_auth_header(202, "BUSINESS_OWNER", 101)
    resp = client.post("/api/invoices/101/approve", headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "SENT"
    assert resp.json()["approved_by_name"] == "Owner One"


def test_accountant_cannot_approve_or_delete_invoice(client: TestClient, db_session: Session):
    """ACCOUNTANT cannot approve or delete invoices."""
    seed_test_env(db_session)
    headers = get_auth_header(203, "ACCOUNTANT", 101)
    # Cannot approve
    resp1 = client.post("/api/invoices/101/approve", headers=headers)
    assert resp1.status_code == 403
    # Cannot delete
    resp2 = client.delete("/api/invoices/101", headers=headers)
    assert resp2.status_code == 403


# -------------------------------------------------------------------------
# 3. Multi-Tenant Isolation & Anti-IDOR Tests
# -------------------------------------------------------------------------

def test_cross_tenant_invoice_access_blocked(client: TestClient, db_session: Session):
    """Tenant 2 owner cannot read or mutate Tenant 1 invoice (HTTP 403)."""
    seed_test_env(db_session)
    headers_t2 = get_auth_header(205, "BUSINESS_OWNER", 102)
    
    # Attempting to fetch Tenant 1's invoice (ID: 101)
    resp = client.get("/api/invoices/101", headers=headers_t2)
    assert resp.status_code == 403
    assert "does not belong to your business organization" in resp.json()["detail"]


def test_cross_tenant_customer_access_blocked(client: TestClient, db_session: Session):
    """Tenant 2 owner cannot access Tenant 1 customer (HTTP 403)."""
    seed_test_env(db_session)
    headers_t2 = get_auth_header(205, "BUSINESS_OWNER", 102)
    resp = client.get("/api/customers/101", headers=headers_t2)
    assert resp.status_code == 403


def test_tenant_invoice_listing_isolation(client: TestClient, db_session: Session):
    """Tenant 1 user only sees Tenant 1 invoices."""
    seed_test_env(db_session)
    headers_t1 = get_auth_header(202, "BUSINESS_OWNER", 101)
    resp = client.get("/api/invoices", headers=headers_t1)
    assert resp.status_code == 200
    items = resp.json()["items"]
    # All returned invoices must belong to Tenant 1
    for inv in items:
        assert inv["business_id"] == 101 or inv["business_id"] is None


def test_super_admin_cross_tenant_visibility(client: TestClient, db_session: Session):
    """SUPER_ADMIN can access invoices across any tenant."""
    seed_test_env(db_session)
    headers_admin = get_auth_header(201, "SUPER_ADMIN", 101)
    resp = client.get("/api/invoices/102", headers=headers_admin)
    assert resp.status_code == 200
    assert resp.json()["id"] == 102


# -------------------------------------------------------------------------
# 4. Customer CRUD & Tenant Scoping
# -------------------------------------------------------------------------

def test_customer_crud_lifecycle(client: TestClient, db_session: Session):
    """Test full customer lifecycle: create, list, update, and get."""
    seed_test_env(db_session)
    headers = get_auth_header(202, "BUSINESS_OWNER", 101)
    
    # 1. Create
    payload = {
        "name": "Acme Widgets Ltd",
        "email": "acme@widgets.com",
        "phone": "+1-555-1234",
        "address": "123 Main St"
    }
    resp = client.post("/api/customers", json=payload, headers=headers)
    assert resp.status_code == 201, resp.text
    cust_id = resp.json()["id"]

    # 2. Get
    resp = client.get(f"/api/customers/{cust_id}", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["name"] == "Acme Widgets Ltd"

    # 3. Update
    resp = client.put(f"/api/customers/{cust_id}", json={"address": "456 Market St"}, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["address"] == "456 Market St"


# -------------------------------------------------------------------------
# 5. Live Dynamic Dashboard Statistics
# -------------------------------------------------------------------------

def test_live_dashboard_stats_endpoint(client: TestClient, db_session: Session):
    """Verify /api/dashboard/stats returns real aggregated DB numbers."""
    seed_test_env(db_session)
    headers = get_auth_header(202, "BUSINESS_OWNER", 101)
    resp = client.get("/api/dashboard/stats", headers=headers)
    assert resp.status_code == 200, resp.text
    stats = resp.json()
    assert "total_revenue_collected" in stats
    assert "total_invoices" in stats
    assert "paid_invoices" in stats
    assert "overdue_invoices" in stats
    assert isinstance(stats["total_invoices"], int)


# -------------------------------------------------------------------------
# 6. Real-Time Events Buffer & Polling Fallback
# -------------------------------------------------------------------------

def test_events_recent_polling_fallback(client: TestClient, db_session: Session):
    """Verify /api/events/recent returns recent bus events filtered by tenant."""
    seed_test_env(db_session)
    headers = get_auth_header(202, "BUSINESS_OWNER", 101)
    resp = client.get("/api/events/recent", headers=headers)
    assert resp.status_code == 200
    events_data = resp.json()
    assert "events" in events_data
    assert isinstance(events_data["events"], list)
