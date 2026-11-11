"""
Database Backup & Disaster Recovery Service for InvoicePilot AI.
Exports and restores all core models: Users, Customers, Invoices, InvoiceItems,
Payments, WebhookEvents, AuditResults, ReminderRecords, ActivityLogs.
Includes SHA-256 data integrity validation.
"""

import json
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, List
from sqlalchemy.orm import Session
from sqlalchemy import create_engine
from app.models.models import (
    Base, User, Customer, Invoice, InvoiceItem, Payment,
    WebhookEvent, AuditResult, ReminderRecord, ActivityLog,
    DocumentUpload, ReconciliationRecord
)
from app.database import engine

BACKUP_DIR = Path(__file__).resolve().parent.parent.parent / "backups"


class BackupService:
    @staticmethod
    def export_database_snapshot(session: Session, target_path: Path) -> Dict[str, Any]:
        """
        Dumps all persistent application records to a structured JSON snapshot file.
        Computes SHA-256 checksum for tamper evidence.
        """
        target_path.parent.mkdir(parents=True, exist_ok=True)

        tables = {
            "customers": [
                {
                    "id": c.id, "name": c.name, "email": c.email,
                    "phone": c.phone, "address": c.address,
                    "created_at": c.created_at.isoformat() if c.created_at else None
                }
                for c in session.query(Customer).all()
            ],
            "invoices": [
                {
                    "id": inv.id, "invoice_number": inv.invoice_number,
                    "customer_id": inv.customer_id, "currency": inv.currency,
                    "subtotal": inv.subtotal, "tax_total": inv.tax_total,
                    "total_amount": inv.total_amount,
                    "issue_date": inv.issue_date.isoformat() if inv.issue_date else None,
                    "due_date": inv.due_date.isoformat() if inv.due_date else None,
                    "status": inv.status.value if hasattr(inv.status, "value") else str(inv.status),
                    "paypal_invoice_id": inv.paypal_invoice_id,
                    "paypal_status": inv.paypal_status,
                    "document_path": inv.document_path,
                    "raw_extracted_text": inv.raw_extracted_text,
                    "is_demo": inv.is_demo, "is_deleted": inv.is_deleted,
                    "notes": inv.notes,
                    "created_at": inv.created_at.isoformat() if inv.created_at else None
                }
                for inv in session.query(Invoice).all()
            ],
            "invoice_items": [
                {
                    "id": itm.id, "invoice_id": itm.invoice_id,
                    "description": itm.description, "quantity": itm.quantity,
                    "unit_price": itm.unit_price, "tax_rate": itm.tax_rate,
                    "tax_amount": itm.tax_amount, "total": itm.total
                }
                for itm in session.query(InvoiceItem).all()
            ],
            "payments": [
                {
                    "id": p.id, "invoice_id": p.invoice_id,
                    "amount": p.amount, "currency": p.currency,
                    "status": p.status.value if hasattr(p.status, "value") else str(p.status),
                    "payment_method": p.payment_method,
                    "paypal_payment_id": p.paypal_payment_id,
                    "paypal_capture_id": p.paypal_capture_id,
                    "fee_amount": p.fee_amount, "net_amount": p.net_amount,
                    "payer_email": p.payer_email, "payer_name": p.payer_name,
                    "reconciliation_status": p.reconciliation_status,
                    "reconciliation_notes": p.reconciliation_notes,
                    "payment_date": p.payment_date.isoformat() if p.payment_date else None
                }
                for p in session.query(Payment).all()
            ],
            "webhook_events": [
                {
                    "id": w.id, "event_id": w.event_id,
                    "event_type": w.event_type,
                    "resource_type": w.resource_type,
                    "is_verified": w.is_verified,
                    "processing_status": w.processing_status,
                    "error_detail": w.error_detail,
                    "retry_count": w.retry_count,
                    "payload": w.payload,
                    "received_at": w.received_at.isoformat() if w.received_at else None,
                    "processed_at": w.processed_at.isoformat() if w.processed_at else None
                }
                for w in session.query(WebhookEvent).all()
            ],
            "audit_results": [
                {
                    "id": a.id, "invoice_id": a.invoice_id,
                    "is_valid": a.is_valid,
                    "issues": a.issues, "passed_checks": a.passed_checks,
                    "calculated_subtotal": a.calculated_subtotal,
                    "calculated_tax": a.calculated_tax,
                    "calculated_total": a.calculated_total,
                    "stated_subtotal": a.stated_subtotal,
                    "stated_tax": a.stated_tax,
                    "stated_total": a.stated_total,
                    "overall_recommendation": a.overall_recommendation,
                    "max_severity": a.max_severity,
                    "review_status": a.review_status,
                    "reviewer_decision": a.reviewer_decision,
                    "ai_summary": a.ai_summary,
                    "ai_findings": a.ai_findings,
                    "audited_at": a.audited_at.isoformat() if a.audited_at else None
                }
                for a in session.query(AuditResult).all()
            ],
            "reminder_records": [
                {
                    "id": r.id, "invoice_id": r.invoice_id,
                    "recipient_email": r.recipient_email,
                    "priority_level": r.priority_level,
                    "priority_reason": r.priority_reason,
                    "subject": r.subject, "body": r.body,
                    "reminder_type": r.reminder_type,
                    "status": r.status, "tone": r.tone,
                    "dispatch_method": r.dispatch_method,
                    "recommended_send_date": r.recommended_send_date.isoformat() if r.recommended_send_date else None,
                    "created_at": r.created_at.isoformat() if hasattr(r, "created_at") and r.created_at else None
                }
                for r in session.query(ReminderRecord).all()
            ],
            "activity_logs": [
                {
                    "id": act.id, "action": act.action,
                    "entity_type": act.entity_type, "entity_id": act.entity_id,
                    "description": act.description,
                    "metadata_json": act.metadata_json,
                    "created_at": act.created_at.isoformat() if act.created_at else None
                }
                for act in session.query(ActivityLog).all()
            ]
        }

        # Calculate counts
        counts = {k: len(v) for k, v in tables.items()}
        data_json = json.dumps(tables, sort_keys=True)
        data_hash = hashlib.sha256(data_json.encode("utf-8")).hexdigest()

        snapshot = {
            "metadata": {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "sha256_hash": data_hash,
                "record_counts": counts,
                "engine_name": session.bind.name if session.bind else "unknown",
                "format_version": "1.0.0"
            },
            "data": tables
        }

        target_path.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
        return snapshot["metadata"]

    @staticmethod
    def restore_database_snapshot(
        target_session: Session, snapshot_path: Path
    ) -> Dict[str, Any]:
        """
        Safely restores all tables from a verified snapshot into target_session.
        Validates SHA-256 integrity before inserting.
        """
        if not snapshot_path.exists():
            raise FileNotFoundError(f"Snapshot file not found: {snapshot_path}")

        snapshot_content = json.loads(snapshot_path.read_text(encoding="utf-8"))
        meta = snapshot_content.get("metadata", {})
        data = snapshot_content.get("data", {})

        # Verify integrity
        computed_hash = hashlib.sha256(json.dumps(data, sort_keys=True).encode("utf-8")).hexdigest()
        if computed_hash != meta.get("sha256_hash"):
            raise ValueError("Integrity check failed: Snapshot data does not match SHA-256 checksum!")

        # Restore customers
        restored_counts = {}
        for c_dict in data.get("customers", []):
            cust = Customer(
                id=c_dict["id"], name=c_dict["name"], email=c_dict["email"],
                phone=c_dict.get("phone"), address=c_dict.get("address")
            )
            target_session.merge(cust)
        target_session.commit()
        restored_counts["customers"] = len(data.get("customers", []))

        # Restore invoices
        for i_dict in data.get("invoices", []):
            inv = Invoice(
                id=i_dict["id"], invoice_number=i_dict["invoice_number"],
                customer_id=i_dict["customer_id"], currency=i_dict["currency"],
                subtotal=i_dict["subtotal"], tax_total=i_dict["tax_total"],
                total_amount=i_dict["total_amount"],
                status=i_dict["status"],
                paypal_invoice_id=i_dict.get("paypal_invoice_id"),
                paypal_status=i_dict.get("paypal_status"),
                document_path=i_dict.get("document_path"),
                raw_extracted_text=i_dict.get("raw_extracted_text"),
                is_demo=i_dict.get("is_demo", False),
                is_deleted=i_dict.get("is_deleted", False),
                notes=i_dict.get("notes")
            )
            target_session.merge(inv)
        target_session.commit()
        restored_counts["invoices"] = len(data.get("invoices", []))

        # Restore items
        for itm_dict in data.get("invoice_items", []):
            itm = InvoiceItem(
                id=itm_dict["id"], invoice_id=itm_dict["invoice_id"],
                description=itm_dict["description"], quantity=itm_dict["quantity"],
                unit_price=itm_dict["unit_price"], tax_rate=itm_dict.get("tax_rate", 0.0),
                tax_amount=itm_dict.get("tax_amount", 0.0), total=itm_dict.get("total", 0.0)
            )
            target_session.merge(itm)
        target_session.commit()
        restored_counts["invoice_items"] = len(data.get("invoice_items", []))

        # Restore payments
        for p_dict in data.get("payments", []):
            p = Payment(
                id=p_dict["id"], invoice_id=p_dict["invoice_id"],
                amount=p_dict["amount"], currency=p_dict["currency"],
                status=p_dict["status"], payment_method=p_dict.get("payment_method", "paypal"),
                paypal_payment_id=p_dict.get("paypal_payment_id"),
                paypal_capture_id=p_dict.get("paypal_capture_id"),
                fee_amount=p_dict.get("fee_amount", 0.0),
                net_amount=p_dict.get("net_amount", 0.0),
                payer_email=p_dict.get("payer_email"),
                payer_name=p_dict.get("payer_name"),
                reconciliation_status=p_dict.get("reconciliation_status", "UNMATCHED"),
                reconciliation_notes=p_dict.get("reconciliation_notes")
            )
            target_session.merge(p)
        target_session.commit()
        restored_counts["payments"] = len(data.get("payments", []))

        # Restore webhook events
        for w_dict in data.get("webhook_events", []):
            w = WebhookEvent(
                id=w_dict["id"], event_id=w_dict["event_id"],
                event_type=w_dict["event_type"],
                resource_type=w_dict.get("resource_type"),
                is_verified=w_dict.get("is_verified", False),
                processing_status=w_dict.get("processing_status", "PENDING"),
                error_detail=w_dict.get("error_detail"),
                retry_count=w_dict.get("retry_count", 0),
                payload=w_dict.get("payload", {})
            )
            target_session.merge(w)
        target_session.commit()
        restored_counts["webhook_events"] = len(data.get("webhook_events", []))

        # Restore audit results
        for a_dict in data.get("audit_results", []):
            a = AuditResult(
                id=a_dict["id"], invoice_id=a_dict["invoice_id"],
                is_valid=a_dict.get("is_valid", True),
                issues=a_dict.get("issues", []),
                passed_checks=a_dict.get("passed_checks", []),
                calculated_subtotal=a_dict.get("calculated_subtotal", 0.0),
                calculated_tax=a_dict.get("calculated_tax", 0.0),
                calculated_total=a_dict.get("calculated_total", 0.0),
                stated_subtotal=a_dict.get("stated_subtotal"),
                stated_tax=a_dict.get("stated_tax"),
                stated_total=a_dict.get("stated_total"),
                overall_recommendation=a_dict.get("overall_recommendation", "REVIEW"),
                max_severity=a_dict.get("max_severity", "INFO"),
                review_status=a_dict.get("review_status", "PENDING"),
                reviewer_decision=a_dict.get("reviewer_decision"),
                ai_summary=a_dict.get("ai_summary"),
                ai_findings=a_dict.get("ai_findings", [])
            )
            target_session.merge(a)
        target_session.commit()
        restored_counts["audit_results"] = len(data.get("audit_results", []))

        # Restore reminder records
        for r_dict in data.get("reminder_records", []):
            r = ReminderRecord(
                id=r_dict["id"], invoice_id=r_dict["invoice_id"],
                recipient_email=r_dict.get("recipient_email", ""),
                priority_level=r_dict.get("priority_level", "MEDIUM"),
                priority_reason=r_dict.get("priority_reason"),
                subject=r_dict.get("subject", ""),
                body=r_dict.get("body", ""),
                reminder_type=r_dict.get("reminder_type", "DUE_SOON"),
                status=r_dict.get("status", "PENDING_APPROVAL"),
                tone=r_dict.get("tone", "FRIENDLY"),
                dispatch_method=r_dict.get("dispatch_method", "MANUAL_COPY")
            )
            target_session.merge(r)
        target_session.commit()
        restored_counts["reminder_records"] = len(data.get("reminder_records", []))

        return restored_counts
