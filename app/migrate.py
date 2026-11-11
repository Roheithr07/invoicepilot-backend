"""
Database Migration and Schema Synchronization Script for InvoicePilot AI.
"""

import sqlite3
import os
from pathlib import Path
from app.database import engine, Base
from app.config import settings
import app.models  # Import all models to ensure registered in Base.metadata

def sync_schema():
    print(f"Synchronizing Database Schema for engine: {engine.name}...")
    
    # 1. Run SQLAlchemy create_all to create any newly declared tables
    Base.metadata.create_all(bind=engine)
    
    # 2. If SQLite, add newly added columns to existing tables using ALTER TABLE
    if settings.DATABASE_URL.startswith("sqlite"):
        db_path = settings.DATABASE_URL.replace("sqlite:///", "")
        if os.path.exists(db_path):
            conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        
        # Check existing columns in invoices table
        cur.execute("PRAGMA table_info(invoices);")
        columns = [row[1] for row in cur.fetchall()]
        
        if "is_deleted" not in columns:
            print("Adding column 'is_deleted' to invoices...")
            cur.execute("ALTER TABLE invoices ADD COLUMN is_deleted BOOLEAN DEFAULT 0;")
            
        if "document_path" not in columns:
            print("Adding column 'document_path' to invoices...")
            cur.execute("ALTER TABLE invoices ADD COLUMN document_path VARCHAR(500);")
            
        if "raw_extracted_text" not in columns:
            print("Adding column 'raw_extracted_text' to invoices...")
            cur.execute("ALTER TABLE invoices ADD COLUMN raw_extracted_text TEXT;")

        # Phase 5 PayPal Integration Columns
        paypal_invoice_alterations = [
            ("paypal_status", "VARCHAR(50)"),
            ("paypal_last_checked_at", "TIMESTAMP"),
            ("paypal_draft_created_at", "TIMESTAMP"),
            ("paypal_sent_at", "TIMESTAMP"),
            ("paypal_error_detail", "TEXT"),
        ]
        for col_name, col_type in paypal_invoice_alterations:
            if col_name not in columns:
                print(f"Adding column '{col_name}' to invoices...")
                cur.execute(f"ALTER TABLE invoices ADD COLUMN {col_name} {col_type};")
            
        # Check existing columns in audit_results table
        cur.execute("PRAGMA table_info(audit_results);")
        audit_cols = [row[1] for row in cur.fetchall()]
        
        audit_alterations = [
            ("stated_subtotal", "FLOAT"),
            ("stated_tax", "FLOAT"),
            ("stated_total", "FLOAT"),
            ("overall_recommendation", "VARCHAR(20) DEFAULT 'REVIEW'"),
            ("max_severity", "VARCHAR(20) DEFAULT 'INFO'"),
            ("review_status", "VARCHAR(20) DEFAULT 'PENDING'"),
            ("reviewer_decision", "VARCHAR(50)"),
            ("resolution_notes", "TEXT"),
            ("reviewed_at", "TIMESTAMP"),
            ("ai_summary", "TEXT"),
            ("ai_findings", "JSON DEFAULT '[]'"),
        ]
        
        for col_name, col_type in audit_alterations:
            if col_name not in audit_cols:
                print(f"Adding column '{col_name}' to audit_results...")
                cur.execute(f"ALTER TABLE audit_results ADD COLUMN {col_name} {col_type};")

        # Phase 6 Payment Reconciliation Columns
        cur.execute("PRAGMA table_info(payments);")
        payment_cols = [row[1] for row in cur.fetchall()]
        payment_alterations = [
            ("paypal_capture_id", "VARCHAR(255)"),
            ("fee_amount", "FLOAT DEFAULT 0.0"),
            ("net_amount", "FLOAT DEFAULT 0.0"),
            ("payer_email", "VARCHAR(255)"),
            ("payer_name", "VARCHAR(255)"),
            ("reconciliation_status", "VARCHAR(50) DEFAULT 'UNMATCHED'"),
            ("reconciliation_notes", "TEXT"),
        ]
        for col_name, col_type in payment_alterations:
            if col_name not in payment_cols:
                print(f"Adding column '{col_name}' to payments...")
                cur.execute(f"ALTER TABLE payments ADD COLUMN {col_name} {col_type};")

        # Phase 6 Reminder Record Columns
        cur.execute("PRAGMA table_info(reminder_records);")
        reminder_cols = [row[1] for row in cur.fetchall()]
        reminder_alterations = [
            ("priority_level", "VARCHAR(50) DEFAULT 'MEDIUM'"),
            ("priority_reason", "TEXT"),
            ("recommended_send_date", "TIMESTAMP"),
            ("tone", "VARCHAR(50) DEFAULT 'FRIENDLY'"),
            ("status", "VARCHAR(50) DEFAULT 'PENDING_APPROVAL'"),
            ("dispatch_method", "VARCHAR(50) DEFAULT 'MANUAL_COPY'"),
            ("dispatch_notes", "TEXT"),
            ("approved_at", "TIMESTAMP"),
            ("approved_by", "VARCHAR(255)"),
        ]
        for col_name, col_type in reminder_alterations:
            if col_name not in reminder_cols:
                print(f"Adding column '{col_name}' to reminder_records...")
                cur.execute(f"ALTER TABLE reminder_records ADD COLUMN {col_name} {col_type};")

        # Phase 6 Webhook Event Columns
        cur.execute("PRAGMA table_info(webhook_events);")
        webhook_cols = [row[1] for row in cur.fetchall()]
        webhook_alterations = [
            ("processing_status", "VARCHAR(50) DEFAULT 'PENDING'"),
            ("error_detail", "TEXT"),
            ("retry_count", "INTEGER DEFAULT 0"),
        ]
        for col_name, col_type in webhook_alterations:
            if col_name not in webhook_cols:
                print(f"Adding column '{col_name}' to webhook_events...")
                cur.execute(f"ALTER TABLE webhook_events ADD COLUMN {col_name} {col_type};")

        # Phase 12 RBAC & Multi-Tenancy Columns
        # 1. users table
        cur.execute("PRAGMA table_info(users);")
        user_cols = [row[1] for row in cur.fetchall()]
        user_alterations = [
            ("hashed_password", "VARCHAR(255)"),
            ("role", "VARCHAR(50) DEFAULT 'STAFF'"),
            ("business_id", "INTEGER"),
            ("is_active", "BOOLEAN DEFAULT 1"),
        ]
        for col_name, col_type in user_alterations:
            if col_name not in user_cols:
                print(f"Adding column '{col_name}' to users...")
                cur.execute(f"ALTER TABLE users ADD COLUMN {col_name} {col_type};")

        # 2. customers table
        cur.execute("PRAGMA table_info(customers);")
        cust_cols = [row[1] for row in cur.fetchall()]
        if "business_id" not in cust_cols:
            print("Adding column 'business_id' to customers...")
            cur.execute("ALTER TABLE customers ADD COLUMN business_id INTEGER;")

        # 3. invoices table
        if "business_id" not in columns:
            print("Adding column 'business_id' to invoices...")
            cur.execute("ALTER TABLE invoices ADD COLUMN business_id INTEGER;")
        if "approved_by_user_id" not in columns:
            print("Adding column 'approved_by_user_id' to invoices...")
            cur.execute("ALTER TABLE invoices ADD COLUMN approved_by_user_id INTEGER;")
        if "approved_by_name" not in columns:
            print("Adding column 'approved_by_name' to invoices...")
            cur.execute("ALTER TABLE invoices ADD COLUMN approved_by_name VARCHAR(255);")
        if "sent_by_user_id" not in columns:
            print("Adding column 'sent_by_user_id' to invoices...")
            cur.execute("ALTER TABLE invoices ADD COLUMN sent_by_user_id INTEGER;")
        if "sent_by_name" not in columns:
            print("Adding column 'sent_by_name' to invoices...")
            cur.execute("ALTER TABLE invoices ADD COLUMN sent_by_name VARCHAR(255);")

        # Performance Indexes
        performance_indexes = [
            "CREATE INDEX IF NOT EXISTS idx_invoices_customer_id ON invoices(customer_id);",
            "CREATE INDEX IF NOT EXISTS idx_invoices_business_id ON invoices(business_id);",
            "CREATE INDEX IF NOT EXISTS idx_customers_business_id ON customers(business_id);",
            "CREATE INDEX IF NOT EXISTS idx_users_business_id ON users(business_id);",
            "CREATE INDEX IF NOT EXISTS idx_invoice_items_invoice_id ON invoice_items(invoice_id);",
            "CREATE INDEX IF NOT EXISTS idx_payments_invoice_id ON payments(invoice_id);",
            "CREATE INDEX IF NOT EXISTS idx_audit_results_invoice_id ON audit_results(invoice_id);",
            "CREATE INDEX IF NOT EXISTS idx_reminder_records_invoice_id ON reminder_records(invoice_id);",
            "CREATE INDEX IF NOT EXISTS idx_reconciliation_records_invoice_id ON reconciliation_records(invoice_id);",
            "CREATE INDEX IF NOT EXISTS idx_reconciliation_records_payment_id ON reconciliation_records(payment_id);"
        ]
        for idx_sql in performance_indexes:
            cur.execute(idx_sql)

        # Seed Default Businesses and Link Legacy Records
        cur.execute("SELECT count(*) FROM businesses;")
        business_count = cur.fetchone()[0]
        if business_count == 0:
            print("Seeding default multi-tenant businesses...")
            cur.execute("INSERT INTO businesses (id, name, registration_number, currency, created_at, updated_at) VALUES (1, 'Apex Global Technologies', 'US-DEL-98214', 'USD', datetime('now'), datetime('now'));")
            cur.execute("INSERT INTO businesses (id, name, registration_number, currency, created_at, updated_at) VALUES (2, 'Acme Retail Corp', 'US-CA-55102', 'USD', datetime('now'), datetime('now'));")

        # Associate existing legacy records with business_id 1
        cur.execute("UPDATE invoices SET business_id = 1 WHERE business_id IS NULL;")
        cur.execute("UPDATE customers SET business_id = 1 WHERE business_id IS NULL;")

        # Seed Default Demo Users for all 4 RBAC Roles
        from app.auth.security import hash_password
        default_pwd_hash = hash_password("Password123!")

        demo_users = [
            ("admin@invoicepilot.ai", "Alex Administrator", "SUPER_ADMIN", 1, default_pwd_hash),
            ("owner@invoicepilot.ai", "Olivia Owner", "BUSINESS_OWNER", 1, default_pwd_hash),
            ("accountant@invoicepilot.ai", "Arthur Accountant", "ACCOUNTANT", 1, default_pwd_hash),
            ("staff@invoicepilot.ai", "Sam Staff", "STAFF", 1, default_pwd_hash),
            ("owner@acmeretail.com", "Rachel Retailer", "BUSINESS_OWNER", 2, default_pwd_hash)
        ]

        for email, name, role, b_id, pwd_hash in demo_users:
            cur.execute("SELECT id FROM users WHERE email = ?;", (email,))
            existing = cur.fetchone()
            if not existing:
                print(f"Seeding demo user: {email} ({role}) for business {b_id}...")
                cur.execute(
                    "INSERT INTO users (email, name, role, business_id, hashed_password, is_active, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 1, datetime('now'), datetime('now'));",
                    (email, name, role, b_id, pwd_hash)
                )
            else:
                # Update role, business_id, and password hash if empty
                cur.execute(
                    "UPDATE users SET role = ?, business_id = ?, hashed_password = COALESCE(hashed_password, ?) WHERE id = ?;",
                    (role, b_id, pwd_hash, existing[0])
                )

        conn.commit()
        conn.close()
        print("Schema synchronization completed successfully.")

if __name__ == "__main__":
    sync_schema()

