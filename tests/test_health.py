"""
Test Suite for System Health and Initialization in InvoicePilot AI.
"""

from fastapi.testclient import TestClient
from app.main import app
from app.config import settings

client = TestClient(app)


def test_root_endpoint():
    """Verify that root endpoint responds with welcoming status."""
    response = client.get("/")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "online"
    assert settings.APP_NAME in data["message"]


def test_health_check_endpoint():
    """Verify health check endpoint returns 200, status=healthy, and connected DB."""
    for path in ["/api/health", "/api/v1/health", "/health"]:
        response = client.get(path)
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert data["app_name"] == "InvoicePilot AI"
        assert data["database"] == "connected"
        assert "timestamp" in data

