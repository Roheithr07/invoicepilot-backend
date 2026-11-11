# InvoicePilot AI — Backend API Service

[![PayPal AI Hackathon 2026](https://img.shields.io/badge/PayPal%20AI%20Hackathon-2026-blue.svg)](https://devpost.com)
[![FastAPI](https://img.shields.io/badge/Framework-FastAPI%200.110+-009688.svg)](https://fastapi.tiangolo.com)
[![Python](https://img.shields.io/badge/Python-3.11+-3776AB.svg)](https://www.python.org/)
[![Render](https://img.shields.io/badge/Deploy-Render-46E3B7.svg)](https://render.com)
[![APIMatic](https://img.shields.io/badge/Sponsor-APIMatic%20PayPal%20Plugin-00D4AA.svg)](https://apimatic.io)

Autonomous AI-Powered Financial Operations, Dispute Defense, and Cash Flow Copilot built for the **PayPal AI Hackathon 2026** (Business Solutions Category).

---

## Architecture Overview

InvoicePilot AI Backend is an enterprise-ready, multi-tenant financial orchestration API built with FastAPI, SQLAlchemy, and PayPal's official APIs:

- **PayPal Invoicing API v2**: Draft generation, customer invoicing, webhook ingestion, payment status tracking, and reconciliation.
- **APIMatic PayPal Context Plugin**: Protocol-level OAuth 2.0 token caching, automatic 401 retry recovery, structured ApiError decoding, and debug-id tracing.
- **Multi-Tenant Enterprise RBAC**: Role-based access control supporting `Business Owner`, `Finance Admin`, `Auditor`, and `Super Admin` roles.
- **AI Invoice Extraction & Auditing**: Fallback-safe OCR and deterministic financial auditing with guardrails preventing dispatch of disputed or invalid invoices.
- **Render Cloud Ready**: Native Python 3.11 web service with containerized Docker support, zero-downtime health probes, and managed PostgreSQL/SQLite support.

---

## Directory Structure

```text
invoicepilot-backend/
├── alembic/                # Database migrations
├── alembic.ini             # Alembic configuration
├── app/                    # Core application package
│   ├── auth/               # JWT authentication, passwords, RBAC permissions
│   ├── routers/            # FastAPI API route controllers
│   │   ├── auth.py         # Login, register, token refresh
│   │   ├── customers.py    # Customer CRUD
│   │   ├── dashboard.py    # Multi-tenant zero-state dashboard metrics
│   │   ├── events.py       # Real-time SSE / event stream
│   │   ├── health.py       # System, DB, and PayPal health probes
│   │   ├── integrations.py # Multi-sponsor status (APIMatic, Render, Zapier)
│   │   ├── invoices.py     # Invoices, upload, OCR, dispatch
│   │   ├── payments.py     # Payment records and reconciliation
│   │   └── users.py        # Team members and RBAC management
│   ├── schemas/            # Pydantic v2 schemas and validation models
│   ├── services/           # Business logic and external service clients
│   │   ├── ai_extractor.py # AI invoice extraction and parser
│   │   ├── ai_provider.py  # Local / deterministic free AI engine
│   │   ├── auditor.py      # Automated invoice anomaly auditor
│   │   ├── paypal_service.py # Official PayPal Invoicing v2 + OAuth engine
│   │   └── zapier_service.py # Webhook and automation dispatcher
│   ├── config.py           # Pydantic Settings configuration loader
│   ├── database.py         # SQLAlchemy engine, session maker, schema sync
│   ├── main.py             # FastAPI entry point, lifespan, CORS, middleware
│   └── models.py           # SQLAlchemy database entities
├── tests/                  # Pytest automated test suite (79 tests)
├── Dockerfile              # Production container specification
├── render.yaml             # Render Infrastructure-as-Code blueprint
├── requirements.txt        # Pinned Python production dependencies
├── .env.example            # Environment variables template
└── README.md               # Backend documentation
```

---

## API Endpoints Summary

| Endpoint | Method | Role Required | Description |
|---|---|---|---|
| `/api/health` | GET | Public | Render / load-balancer health check |
| `/api/v1/health` | GET | Public | Detailed system, DB, and PayPal status |
| `/api/v1/auth/register` | POST | Public | User & tenant registration |
| `/api/v1/auth/login` | POST | Public | OAuth2 password bearer token generation |
| `/api/v1/dashboard/metrics` | GET | Authenticated | Live business metrics and cash flow KPIs |
| `/api/v1/invoices` | GET / POST | Authenticated | List, filter, and draft invoices |
| `/api/v1/invoices/{id}/dispatch` | POST | Owner / Admin | Send invoice via PayPal Invoicing API v2 |
| `/api/v1/invoices/{id}/audit` | POST | Authenticated | Run AI financial anomaly audit |
| `/api/v1/customers` | GET / POST | Authenticated | Manage customer CRM profiles |
| `/api/v1/events` | GET | Authenticated | Server-Sent Events (SSE) notification stream |
| `/api/v1/webhooks/paypal` | POST | Public (Signed)| PayPal webhook ingestion & verification |
| `/api/v1/integrations/status`| GET | Authenticated | Multi-sponsor tool verification status |

---

## Local Development Setup

1. **Clone the repository:**
   ```bash
   git clone https://github.com/Roheithr07/invoicepilot-backend.git
   cd invoicepilot-backend
   ```

2. **Create and activate virtual environment:**
   ```bash
   python -m venv .venv
   # Windows:
   .venv\Scripts\activate
   # Linux/macOS:
   source .venv/bin/activate
   ```

3. **Install dependencies:**
   ```bash
   pip install --upgrade pip
   pip install -r requirements.txt
   ```

4. **Configure environment:**
   ```bash
   cp .env.example .env
   # Edit .env with your PayPal Sandbox credentials and SECRET_KEY
   ```

5. **Run tests:**
   ```bash
   pytest
   ```

6. **Start local development server:**
   ```bash
   uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
   ```

Interactive API documentation will be available at:
- Swagger UI: `http://localhost:8000/docs`
- ReDoc: `http://localhost:8000/redoc`

---

## Render Deployment Guide

### Option 1: Render Blueprint (Recommended)
1. Fork or push to your GitHub repository.
2. In the [Render Dashboard](https://dashboard.render.com), click **New +** > **Blueprint**.
3. Connect `https://github.com/Roheithr07/invoicepilot-backend`.
4. Render will detect `render.yaml` and configure the Web Service automatically.
5. In the Render Environment tab, add the secret environment variables:
   - `PAYPAL_CLIENT_ID`: Your PayPal Developer Sandbox Client ID
   - `PAYPAL_CLIENT_SECRET`: Your PayPal Developer Sandbox Client Secret
   - `PAYPAL_WEBHOOK_ID`: Your PayPal Webhook ID

### Option 2: Manual Web Service
1. In Render Dashboard, click **New +** > **Web Service**.
2. Connect `https://github.com/Roheithr07/invoicepilot-backend`.
3. Set the following fields:
   - **Environment**: Python
   - **Branch**: `main`
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `uvicorn app.main:app --host 0.0.0.0 --port $PORT --proxy-headers --forwarded-allow-ips='*'`
   - **Health Check Path**: `/api/health` (or `/api/v1/health`)
4. Add environment variables according to `.env.example`.
5. Click **Create Web Service**.

---

## License

Created for the PayPal AI Hackathon 2026. All rights reserved.
