"""
Invoice Management and Database Service for InvoicePilot AI.
Provides transactional CRUD operations with backend Decimal calculations and soft deletion.
"""

from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import List, Optional, Tuple
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import or_, desc, asc
from fastapi import HTTPException, status

from app.models.models import (
    Invoice, InvoiceItem, Customer, DocumentUpload, ActivityLog, InvoiceStatus
)
from app.schemas.invoice import InvoiceCreate, InvoiceUpdate


def quantize_dec(val: Decimal) -> Decimal:
    return val.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


class InvoiceService:
    @staticmethod
    def create_invoice(db: Session, data: InvoiceCreate, business_id: Optional[int] = None, user_id: Optional[int] = None) -> Invoice:
        """
        Creates an invoice with independent Decimal financial calculations,
        tenant business isolation, and automatic customer linking inside a database transaction.
        """
        # 1. Uniqueness check on non-deleted invoice numbers
        existing = db.query(Invoice).filter(
            Invoice.invoice_number == data.invoice_number,
            Invoice.is_deleted == False
        ).first()
        if existing:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"An active invoice with number '{data.invoice_number}' already exists."
            )

        # Determine effective business_id
        eff_business_id = business_id or 1

        # 2. Find or create Customer record within tenant business
        customer = db.query(Customer).filter(
            Customer.email == data.customer_email,
            Customer.business_id == eff_business_id
        ).first()
        if not customer:
            customer = Customer(
                name=data.customer_name,
                email=data.customer_email,
                phone=data.customer_phone,
                address=data.customer_address,
                business_id=eff_business_id
            )
            db.add(customer)
            db.flush()
        else:
            # Update customer name / phone if provided
            if data.customer_name and customer.name != data.customer_name:
                customer.name = data.customer_name
            if data.customer_phone:
                customer.phone = data.customer_phone
            if data.customer_address:
                customer.address = data.customer_address
            db.flush()

        # 3. Calculate financial totals using Decimal arithmetic
        subtotal_dec = Decimal("0.00")
        tax_total_dec = Decimal("0.00")
        db_items: List[InvoiceItem] = []

        for it in data.items:
            qty = quantize_dec(Decimal(str(it.quantity)))
            price = quantize_dec(Decimal(str(it.unit_price)))
            rate = quantize_dec(Decimal(str(it.tax_rate or 0.0)))

            item_sub = quantize_dec(qty * price)
            item_tax = quantize_dec(item_sub * (rate / Decimal("100.00")))
            item_tot = item_sub + item_tax

            subtotal_dec += item_sub
            tax_total_dec += item_tax

            db_items.append(InvoiceItem(
                description=it.description,
                quantity=float(qty),
                unit_price=float(price),
                tax_rate=float(rate),
                tax_amount=float(item_tax),
                total=float(item_tot)
            ))

        total_amount_dec = subtotal_dec + tax_total_dec

        # 4. Check document upload link
        doc_path = None
        raw_text = None
        if data.document_upload_id:
            upload_rec = db.query(DocumentUpload).filter(
                DocumentUpload.upload_id == data.document_upload_id
            ).first()
            if upload_rec:
                doc_path = upload_rec.file_path
                raw_text = upload_rec.extracted_text

        # 5. Create Invoice
        invoice = Invoice(
            invoice_number=data.invoice_number,
            business_id=eff_business_id,
            user_id=user_id,
            customer_id=customer.id,
            currency=data.currency,
            subtotal=float(subtotal_dec),
            tax_total=float(tax_total_dec),
            total_amount=float(total_amount_dec),
            issue_date=data.issue_date or datetime.now(timezone.utc),
            due_date=data.due_date,
            status=InvoiceStatus.DRAFT,
            notes=data.notes,
            document_path=doc_path,
            raw_extracted_text=raw_text,
            is_deleted=False,
            is_demo=False,
            items=db_items
        )
        db.add(invoice)
        db.flush()

        # Link upload record back to invoice
        if data.document_upload_id:
            upload_rec = db.query(DocumentUpload).filter(
                DocumentUpload.upload_id == data.document_upload_id
            ).first()
            if upload_rec:
                upload_rec.invoice_id = invoice.id

        # 6. Record Audit/Activity Log
        log = ActivityLog(
            action="INVOICE_CREATED",
            entity_type="invoice",
            entity_id=str(invoice.id),
            description=f"Invoice {invoice.invoice_number} created for {customer.name} (${invoice.total_amount:.2f} {invoice.currency}).",
            metadata_json={
                "invoice_number": invoice.invoice_number,
                "customer_email": customer.email,
                "total_amount": invoice.total_amount,
                "item_count": len(db_items)
            }
        )
        db.add(log)
        db.commit()
        db.refresh(invoice)
        return invoice

    @staticmethod
    def get_invoices(
        db: Session,
        page: int = 1,
        page_size: int = 20,
        status_filter: Optional[str] = None,
        search: Optional[str] = None,
        customer_id: Optional[int] = None,
        sort_by: str = "due_date",
        sort_asc: bool = False,
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None,
        business_id: Optional[int] = None
    ) -> Tuple[List[Invoice], int]:
        """
        Retrieves paginated, filtered, and sorted invoices scoped to business tenant.
        """
        query = db.query(Invoice).options(
            joinedload(Invoice.customer),
            joinedload(Invoice.items)
        ).filter(Invoice.is_deleted == False)

        if business_id is not None:
            query = query.filter(Invoice.business_id == business_id)

        # Filters
        if status_filter and status_filter.upper() != "ALL":
            try:
                st = InvoiceStatus(status_filter.upper())
                query = query.filter(Invoice.status == st)
            except ValueError:
                pass

        if customer_id:
            query = query.filter(Invoice.customer_id == customer_id)

        if date_from:
            query = query.filter(Invoice.issue_date >= date_from)

        if date_to:
            query = query.filter(Invoice.issue_date <= date_to)

        if search and search.strip():
            term = f"%{search.strip()}%"
            query = query.join(Invoice.customer).filter(
                or_(
                    Invoice.invoice_number.ilike(term),
                    Customer.name.ilike(term),
                    Customer.email.ilike(term)
                )
            )

        # Count total matches before pagination
        total_count = query.count()

        # Sorting
        sort_col = getattr(Invoice, sort_by, Invoice.due_date)
        order_clause = asc(sort_col) if sort_asc else desc(sort_col)
        query = query.order_by(order_clause)

        # Pagination
        skip = (page - 1) * page_size
        invoices = query.offset(skip).limit(page_size).all()
        return invoices, total_count

    @staticmethod
    def get_invoice_by_id(db: Session, invoice_id: int) -> Invoice:
        """
        Retrieves a single invoice by ID with items and customer.
        """
        invoice = db.query(Invoice).options(
            joinedload(Invoice.customer),
            joinedload(Invoice.items)
        ).filter(
            Invoice.id == invoice_id,
            Invoice.is_deleted == False
        ).first()

        if not invoice:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Invoice with ID {invoice_id} not found."
            )
        return invoice

    @staticmethod
    def update_invoice(db: Session, invoice_id: int, data: InvoiceUpdate) -> Invoice:
        """
        Updates an existing invoice and recalculates financial figures if items change.
        """
        invoice = InvoiceService.get_invoice_by_id(db, invoice_id)

        # Update customer if details provided
        if data.customer_name or data.customer_email:
            if invoice.customer:
                if data.customer_name:
                    invoice.customer.name = data.customer_name
                if data.customer_email:
                    invoice.customer.email = data.customer_email
                if data.customer_phone is not None:
                    invoice.customer.phone = data.customer_phone
                if data.customer_address is not None:
                    invoice.customer.address = data.customer_address

        if data.currency:
            invoice.currency = data.currency.upper()
        if data.due_date:
            invoice.due_date = data.due_date
        if data.status:
            invoice.status = data.status
        if data.notes is not None:
            invoice.notes = data.notes

        # If items are updated, recalculate line items and totals
        if data.items is not None:
            # Clear old items
            db.query(InvoiceItem).filter(InvoiceItem.invoice_id == invoice.id).delete()
            subtotal_dec = Decimal("0.00")
            tax_total_dec = Decimal("0.00")
            new_items = []

            for it in data.items:
                qty = quantize_dec(Decimal(str(it.quantity or 1.0)))
                price = quantize_dec(Decimal(str(it.unit_price or 0.0)))
                rate = quantize_dec(Decimal(str(it.tax_rate or 0.0)))

                item_sub = quantize_dec(qty * price)
                item_tax = quantize_dec(item_sub * (rate / Decimal("100.00")))
                item_tot = item_sub + item_tax

                subtotal_dec += item_sub
                tax_total_dec += item_tax

                new_items.append(InvoiceItem(
                    invoice_id=invoice.id,
                    description=it.description or "Line Item",
                    quantity=float(qty),
                    unit_price=float(price),
                    tax_rate=float(rate),
                    tax_amount=float(item_tax),
                    total=float(item_tot)
                ))

            invoice.subtotal = float(subtotal_dec)
            invoice.tax_total = float(tax_total_dec)
            invoice.total_amount = float(subtotal_dec + tax_total_dec)
            invoice.items = new_items

        # Log Activity
        log = ActivityLog(
            action="INVOICE_UPDATED",
            entity_type="invoice",
            entity_id=str(invoice.id),
            description=f"Invoice {invoice.invoice_number} updated."
        )
        db.add(log)
        db.commit()
        db.refresh(invoice)
        return invoice

    @staticmethod
    def delete_invoice(db: Session, invoice_id: int) -> bool:
        """
        Soft-deletes invoice. Ensures payment records and audit records are preserved.
        Only invoices in DRAFT or CANCELLED status can be deleted.
        """
        invoice = InvoiceService.get_invoice_by_id(db, invoice_id)

        if invoice.status not in [InvoiceStatus.DRAFT, InvoiceStatus.CANCELLED]:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Cannot delete invoice in '{invoice.status.value}' status. Only DRAFT or CANCELLED invoices can be deleted."
            )

        invoice.is_deleted = True
        log = ActivityLog(
            action="INVOICE_DELETED",
            entity_type="invoice",
            entity_id=str(invoice.id),
            description=f"Draft invoice {invoice.invoice_number} was soft-deleted."
        )
        db.add(log)
        db.commit()
        return True

