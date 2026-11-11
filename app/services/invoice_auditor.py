"""
Financial Validation Engine for InvoicePilot AI.
Layer 1 of the Hybrid Auditing Architecture: Deterministic validation with Python Decimal arithmetic.
"""

from datetime import datetime, timezone, timedelta
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from typing import List, Tuple, Dict, Any, Optional
import re
import hashlib
from sqlalchemy.orm import Session
from sqlalchemy import or_

from app.models.models import (
    Invoice, InvoiceItem, Customer, DocumentUpload,
    AuditSeverity, AuditRecommendation
)
from app.schemas.audit import (
    AuditFinding, FinancialDiscrepancy, AuditCalculationBreakdown
)


# ISO 4217 Currency Decimal Rules
CURRENCY_DECIMALS: Dict[str, int] = {
    # 0 Decimals
    "JPY": 0, "KRW": 0, "VND": 0, "CLP": 0, "HUF": 0, "ISK": 0, "TWD": 0, "UGX": 0, "PYG": 0, "RWF": 0,
    # 3 Decimals
    "BHD": 3, "KWD": 3, "OMR": 3, "JOD": 3, "TND": 3, "IQD": 3, "LYD": 3,
    # Standard 2 Decimals (Defaults applied to all others including USD, EUR, GBP, CAD, AUD, CHF, SGD, INR)
}

EMAIL_REGEX = re.compile(r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$")


def get_currency_decimals(currency_code: str) -> int:
    return CURRENCY_DECIMALS.get(currency_code.upper().strip(), 2)


def quantize_currency(val: Decimal, currency_code: str = "USD") -> Decimal:
    decimals = get_currency_decimals(currency_code)
    exp = Decimal("1") if decimals == 0 else Decimal(f"0.{'0' * (decimals - 1)}1")
    return val.quantize(exp, rounding=ROUND_HALF_UP)


class InvoiceAuditor:
    """
    Deterministic Financial Auditor.
    Executes rule-based checks and banking-grade Decimal calculations.
    """

    @staticmethod
    def audit(db: Session, invoice: Invoice) -> Tuple[bool, List[AuditFinding], List[str], AuditCalculationBreakdown, Dict[str, Any]]:
        findings: List[AuditFinding] = []
        passed_checks: List[str] = []
        evidence_dict: Dict[str, Any] = {}

        currency = (invoice.currency or "USD").upper().strip()
        decimals = get_currency_decimals(currency)
        evidence_dict["currency"] = currency
        evidence_dict["currency_decimals"] = decimals

        # A. CURRENCY VALIDATION
        InvoiceAuditor._validate_currency(invoice, currency, decimals, findings, passed_checks, evidence_dict)

        # B. LINE ITEM VALIDATION & ARITHMETIC
        calc_subtotal, calc_tax, calc_total = InvoiceAuditor._validate_line_items(
            invoice, currency, findings, passed_checks, evidence_dict
        )

        # C. SUBTOTAL VALIDATION
        InvoiceAuditor._validate_subtotal(
            invoice, calc_subtotal, currency, findings, passed_checks, evidence_dict
        )

        # D. TAX VALIDATION
        InvoiceAuditor._validate_tax(
            invoice, calc_tax, currency, findings, passed_checks, evidence_dict
        )

        # E. GRAND TOTAL & DISCREPANCY VALIDATION
        discrepancy = InvoiceAuditor._validate_grand_total(
            invoice, calc_subtotal, calc_tax, calc_total, currency, findings, passed_checks, evidence_dict
        )

        # F. DATE VALIDATION
        InvoiceAuditor._validate_dates(invoice, findings, passed_checks, evidence_dict)

        # G. CUSTOMER VALIDATION
        InvoiceAuditor._validate_customer(invoice, findings, passed_checks, evidence_dict)

        # H. DUPLICATE INVOICE DETECTION
        InvoiceAuditor._detect_duplicates(db, invoice, findings, passed_checks, evidence_dict)

        # I. ANOMALY DETECTION
        InvoiceAuditor._detect_anomalies(db, invoice, calc_total, findings, passed_checks, evidence_dict)

        # Prepare Calculation Breakdown
        breakdown = AuditCalculationBreakdown(
            calculated_subtotal=float(calc_subtotal),
            calculated_tax=float(calc_tax),
            calculated_total=float(calc_total),
            stated_subtotal=float(invoice.subtotal or 0.0),
            stated_tax=float(invoice.tax_total or 0.0),
            stated_total=float(invoice.total_amount or 0.0),
            currency=currency,
            currency_decimals=decimals,
            discrepancy=discrepancy
        )

        is_valid = not any(f.severity in [AuditSeverity.CRITICAL, AuditSeverity.HIGH] for f in findings)
        return is_valid, findings, passed_checks, breakdown, evidence_dict

    @staticmethod
    def _validate_currency(
        invoice: Invoice, currency: str, decimals: int,
        findings: List[AuditFinding], passed_checks: List[str], evidence: Dict[str, Any]
    ):
        if not invoice.currency or len(invoice.currency.strip()) != 3:
            findings.append(AuditFinding(
                finding_id="CURR-001",
                category="CURRENCY",
                severity=AuditSeverity.HIGH,
                title="Invalid Currency Code",
                description=f"Supplied currency '{invoice.currency}' is not a valid 3-letter ISO 4217 code.",
                evidence=f"Value: '{invoice.currency}'",
                recommendation="Specify standard 3-letter ISO code (e.g. USD, EUR, GBP, JPY).",
                field="currency"
            ))
        else:
            passed_checks.append("ISO 4217 Currency Code Validated")

        # Zero decimal currency fraction check
        if decimals == 0:
            stated_tot = Decimal(str(invoice.total_amount or 0.0))
            if stated_tot % 1 != 0:
                findings.append(AuditFinding(
                    finding_id="CURR-002",
                    category="CURRENCY",
                    severity=AuditSeverity.HIGH,
                    title="Fractional Amount in Zero-Decimal Currency",
                    description=f"Currency {currency} does not use fractional cents, but invoice total has decimals.",
                    evidence=f"Total: {invoice.total_amount} {currency}",
                    recommendation=f"Round {currency} totals to whole units without fractional decimal cents.",
                    field="total_amount"
                ))
            else:
                passed_checks.append(f"{currency} Zero-Decimal Precision Respected")

    @staticmethod
    def _validate_line_items(
        invoice: Invoice, currency: str,
        findings: List[AuditFinding], passed_checks: List[str], evidence: Dict[str, Any]
    ) -> Tuple[Decimal, Decimal, Decimal]:
        items = invoice.items or []
        evidence["line_items_count"] = len(items)

        if not items:
            findings.append(AuditFinding(
                finding_id="LINE-001",
                category="ARITHMETIC",
                severity=AuditSeverity.HIGH,
                title="No Itemized Line Items",
                description="The invoice contains no line items. Accounting and auditing standards require an itemized breakdown.",
                evidence="Items count: 0",
                recommendation="Add at least one line item specifying description, quantity, and unit price.",
                field="items"
            ))
            # Fall back to stated values
            sub = quantize_currency(Decimal(str(invoice.subtotal or 0.0)), currency)
            tax = quantize_currency(Decimal(str(invoice.tax_total or 0.0)), currency)
            tot = quantize_currency(Decimal(str(invoice.total_amount or 0.0)), currency)
            return sub, tax, tot

        calc_subtotal = Decimal("0.0")
        calc_tax = Decimal("0.0")
        line_item_math_correct = True

        for idx, item in enumerate(items, start=1):
            qty = Decimal(str(item.quantity or 0.0))
            price = Decimal(str(item.unit_price or 0.0))
            tax_rate = Decimal(str(item.tax_rate or 0.0))

            # Missing description
            if not item.description or not item.description.strip():
                findings.append(AuditFinding(
                    finding_id=f"LINE-DESC-{idx}",
                    category="ARITHMETIC",
                    severity=AuditSeverity.MEDIUM,
                    title=f"Missing Description on Line #{idx}",
                    description=f"Line item #{idx} lacks a product or service description.",
                    evidence=f"Item #{idx} description is empty",
                    recommendation="Provide a clear description for all billed goods or services.",
                    field=f"items[{idx-1}].description"
                ))

            # Quantity check
            if qty <= 0:
                findings.append(AuditFinding(
                    finding_id=f"LINE-QTY-{idx}",
                    category="ARITHMETIC",
                    severity=AuditSeverity.CRITICAL,
                    title=f"Non-Positive Quantity on Line #{idx}",
                    description=f"Line item #{idx} has quantity {qty}. Quantity must be strictly positive.",
                    evidence=f"Quantity: {qty}",
                    recommendation="Set quantity to 1 or greater.",
                    field=f"items[{idx-1}].quantity"
                ))
                line_item_math_correct = False

            # Unit price check
            if price < 0:
                findings.append(AuditFinding(
                    finding_id=f"LINE-PRICE-{idx}",
                    category="ARITHMETIC",
                    severity=AuditSeverity.CRITICAL,
                    title=f"Negative Unit Price on Line #{idx}",
                    description=f"Line item #{idx} has negative unit price {price}. Negative prices are disallowed.",
                    evidence=f"Unit Price: {price}",
                    recommendation="Use positive unit prices. For credits or discounts, record them as formal deductions.",
                    field=f"items[{idx-1}].unit_price"
                ))
                line_item_math_correct = False

            # Decimal arithmetic for line item
            item_sub = quantize_currency(qty * price, currency)
            item_tax = quantize_currency(item_sub * (tax_rate / Decimal("100.0")), currency)
            item_total = item_sub + item_tax

            calc_subtotal += item_sub
            calc_tax += item_tax

            # Check individual item total if recorded
            if item.total is not None and item.total > 0:
                stated_item_tot = quantize_currency(Decimal(str(item.total)), currency)
                diff = abs(stated_item_tot - item_total)
                if diff > Decimal("0.01"):
                    findings.append(AuditFinding(
                        finding_id=f"LINE-MATH-{idx}",
                        category="ARITHMETIC",
                        severity=AuditSeverity.HIGH,
                        title=f"Line #{idx} Math Inconsistency",
                        description=(
                            f"Line item #{idx} ('{item.description}') stated total {stated_item_tot} {currency} "
                            f"does not match calculated ({qty} × {price} + {item_tax} tax = {item_total} {currency})."
                        ),
                        evidence=f"Calculated: {item_total} | Stated: {stated_item_tot} | Difference: {diff}",
                        recommendation="Recalculate line total using unit price × quantity + tax.",
                        field=f"items[{idx-1}].total"
                    ))
                    line_item_math_correct = False

        calc_subtotal = quantize_currency(calc_subtotal, currency)
        calc_tax = quantize_currency(calc_tax, currency)
        calc_total = quantize_currency(calc_subtotal + calc_tax, currency)

        if line_item_math_correct:
            passed_checks.append("All Line Item Quantities, Prices & Math Validated")

        return calc_subtotal, calc_tax, calc_total

    @staticmethod
    def _validate_subtotal(
        invoice: Invoice, calc_subtotal: Decimal, currency: str,
        findings: List[AuditFinding], passed_checks: List[str], evidence: Dict[str, Any]
    ):
        stated_subtotal = quantize_currency(Decimal(str(invoice.subtotal or 0.0)), currency)
        diff = abs(stated_subtotal - calc_subtotal)
        evidence["calculated_subtotal"] = float(calc_subtotal)
        evidence["stated_subtotal"] = float(stated_subtotal)
        evidence["subtotal_diff"] = float(stated_subtotal - calc_subtotal)

        if diff > Decimal("0.01"):
            findings.append(AuditFinding(
                finding_id="SUBTOTAL-001",
                category="ARITHMETIC",
                severity=AuditSeverity.HIGH,
                title="Subtotal Calculation Discrepancy",
                description=(
                    f"The stated invoice subtotal ({stated_subtotal} {currency}) does not equal the sum of itemized "
                    f"line subtotals ({calc_subtotal} {currency}). Discrepancy of {stated_subtotal - calc_subtotal:+.2f} {currency} detected."
                ),
                evidence=f"Line Items Sum: {calc_subtotal} | Stated Subtotal: {stated_subtotal} | Delta: {stated_subtotal - calc_subtotal:+.2f}",
                recommendation="Update invoice subtotal to match the sum of line items.",
                field="subtotal"
            ))
        else:
            passed_checks.append("Invoice Subtotal Matches Itemized Sum")

    @staticmethod
    def _validate_tax(
        invoice: Invoice, calc_tax: Decimal, currency: str,
        findings: List[AuditFinding], passed_checks: List[str], evidence: Dict[str, Any]
    ):
        stated_tax = quantize_currency(Decimal(str(invoice.tax_total or 0.0)), currency)
        diff = abs(stated_tax - calc_tax)
        evidence["calculated_tax"] = float(calc_tax)
        evidence["stated_tax"] = float(stated_tax)
        evidence["tax_diff"] = float(stated_tax - calc_tax)

        # Missing tax rate when tax amount is supplied
        items = invoice.items or []
        for idx, item in enumerate(items, start=1):
            if (item.tax_amount or 0.0) > 0 and (item.tax_rate or 0.0) == 0.0:
                findings.append(AuditFinding(
                    finding_id=f"TAX-RATE-MISSING-{idx}",
                    category="TAX",
                    severity=AuditSeverity.MEDIUM,
                    title=f"Missing Tax Rate for Tax Amount on Line #{idx}",
                    description=f"Line item #{idx} specifies tax amount {item.tax_amount} but tax rate is 0% or omitted.",
                    evidence=f"Tax Amount: {item.tax_amount} | Tax Rate: {item.tax_rate}",
                    recommendation="Specify the explicit tax percentage rate (e.g. 10% VAT, 8.25% Sales Tax).",
                    field=f"items[{idx-1}].tax_rate"
                ))

        if diff > Decimal("0.01"):
            findings.append(AuditFinding(
                finding_id="TAX-001",
                category="TAX",
                severity=AuditSeverity.HIGH,
                title="Tax Calculation Discrepancy",
                description=(
                    f"Stated tax total ({stated_tax} {currency}) differs from calculated itemized tax ({calc_tax} {currency}). "
                    f"Discrepancy: {stated_tax - calc_tax:+.2f} {currency}."
                ),
                evidence=f"Calculated Tax: {calc_tax} | Stated Tax: {stated_tax} | Delta: {stated_tax - calc_tax:+.2f}",
                recommendation="Ensure stated tax equals the mathematical sum of individual line item tax rates.",
                field="tax_total"
            ))
        else:
            passed_checks.append("Tax Rate Math Validated")

    @staticmethod
    def _validate_grand_total(
        invoice: Invoice, calc_subtotal: Decimal, calc_tax: Decimal, calc_total: Decimal, currency: str,
        findings: List[AuditFinding], passed_checks: List[str], evidence: Dict[str, Any]
    ) -> FinancialDiscrepancy:
        stated_total = quantize_currency(Decimal(str(invoice.total_amount or 0.0)), currency)
        expected_total = calc_subtotal + calc_tax
        diff = abs(stated_total - expected_total)

        subtotal_diff = float(quantize_currency(Decimal(str(invoice.subtotal or 0.0)), currency) - calc_subtotal)
        tax_diff = float(quantize_currency(Decimal(str(invoice.tax_total or 0.0)), currency) - calc_tax)
        total_diff = float(stated_total - expected_total)
        has_math_error = abs(total_diff) > 0.01 or abs(subtotal_diff) > 0.01 or abs(tax_diff) > 0.01

        discrepancy = FinancialDiscrepancy(
            subtotal_diff=subtotal_diff,
            tax_diff=tax_diff,
            total_diff=total_diff,
            has_math_error=has_math_error
        )
        evidence["expected_total"] = float(expected_total)
        evidence["stated_total"] = float(stated_total)
        evidence["total_diff"] = total_diff

        if diff > Decimal("0.01"):
            findings.append(AuditFinding(
                finding_id="TOTAL-001",
                category="ARITHMETIC",
                severity=AuditSeverity.CRITICAL,
                title="Grand Total Mismatch",
                description=(
                    f"Invoice grand total ({stated_total} {currency}) does not match Subtotal ({calc_subtotal}) + "
                    f"Tax ({calc_tax}) = {expected_total} {currency}. Discrepancy: {stated_total - expected_total:+.2f} {currency}."
                ),
                evidence=f"Expected Total: {expected_total} | Stated Total: {stated_total} | Discrepancy: {stated_total - expected_total:+.2f}",
                recommendation="Adjust invoice total to equal verified subtotal + tax.",
                field="total_amount"
            ))
        else:
            passed_checks.append("Grand Total Balance Verified (Subtotal + Tax = Total)")

        return discrepancy

    @staticmethod
    def _validate_dates(
        invoice: Invoice,
        findings: List[AuditFinding], passed_checks: List[str], evidence: Dict[str, Any]
    ):
        if not invoice.issue_date:
            findings.append(AuditFinding(
                finding_id="DATE-001",
                category="DATE",
                severity=AuditSeverity.HIGH,
                title="Missing Invoice Issue Date",
                description="The invoice lacks an issue date, which is legally required for valid billing records.",
                evidence="issue_date is null",
                recommendation="Provide the date the invoice was officially generated.",
                field="issue_date"
            ))
        else:
            passed_checks.append("Issue Date Present")

        if invoice.issue_date and invoice.due_date:
            if invoice.due_date < invoice.issue_date:
                findings.append(AuditFinding(
                    finding_id="DATE-002",
                    category="DATE",
                    severity=AuditSeverity.HIGH,
                    title="Due Date Precedes Issue Date",
                    description=(
                        f"Invoice payment due date ({invoice.due_date.strftime('%Y-%m-%d')}) precedes "
                        f"the issue date ({invoice.issue_date.strftime('%Y-%m-%d')}). Payment cannot be due before issuance."
                    ),
                    evidence=f"Issue: {invoice.issue_date.strftime('%Y-%m-%d')} | Due: {invoice.due_date.strftime('%Y-%m-%d')}",
                    recommendation="Set the due date to a date on or after the invoice date.",
                    field="due_date"
                ))
            else:
                passed_checks.append("Chronological Date Sequence Validated")

    @staticmethod
    def _validate_customer(
        invoice: Invoice,
        findings: List[AuditFinding], passed_checks: List[str], evidence: Dict[str, Any]
    ):
        customer = invoice.customer
        c_name = invoice.customer_name or (customer.name if customer else None)
        c_email = invoice.customer_email or (customer.email if customer else None)

        if not c_name or not c_name.strip():
            findings.append(AuditFinding(
                finding_id="CUST-001",
                category="CUSTOMER",
                severity=AuditSeverity.HIGH,
                title="Missing Customer Name",
                description="Invoice has no customer or client company name.",
                evidence="Customer name is empty",
                recommendation="Specify recipient full name or business entity.",
                field="customer_name"
            ))
        else:
            passed_checks.append("Customer Identity Verified")

        if not c_email or not c_email.strip():
            findings.append(AuditFinding(
                finding_id="CUST-002",
                category="CUSTOMER",
                severity=AuditSeverity.HIGH,
                title="Missing Customer Email",
                description="Customer email address is missing. Email is mandatory for PayPal invoice delivery.",
                evidence="Customer email is empty",
                recommendation="Provide a valid client billing email address.",
                field="customer_email"
            ))
        elif not EMAIL_REGEX.match(c_email.strip()):
            findings.append(AuditFinding(
                finding_id="CUST-003",
                category="CUSTOMER",
                severity=AuditSeverity.HIGH,
                title="Invalid Customer Email Format",
                description=f"Customer email '{c_email}' is not a syntactically valid email address.",
                evidence=f"Email: '{c_email}'",
                recommendation="Correct the recipient email address.",
                field="customer_email"
            ))
        else:
            passed_checks.append("Customer Email Syntax Validated")

    @staticmethod
    def _detect_duplicates(
        db: Session, invoice: Invoice,
        findings: List[AuditFinding], passed_checks: List[str], evidence: Dict[str, Any]
    ):
        """
        Duplicate Invoice Detection:
        - Exact duplicate: same invoice number AND same customer AND same total.
        - Same invoice number with different content/amount.
        - Possible duplicate: different invoice number, but same customer AND same total AND issue date within 3 days.
        """
        # Query active invoices excluding self
        others = db.query(Invoice).filter(
            Invoice.id != invoice.id,
            Invoice.is_deleted == False
        ).all()

        c_name = (invoice.customer_name or (invoice.customer.name if invoice.customer else "")).strip().lower()
        c_email = (invoice.customer_email or (invoice.customer.email if invoice.customer else "")).strip().lower()
        inv_num = (invoice.invoice_number or "").strip().lower()
        inv_tot = float(invoice.total_amount or 0.0)

        found_exact = False
        found_same_number = False
        found_possible = False

        for other in others:
            o_num = (other.invoice_number or "").strip().lower()
            o_name = (other.customer_name or (other.customer.name if other.customer else "")).strip().lower()
            o_email = (other.customer_email or (other.customer.email if other.customer else "")).strip().lower()
            o_tot = float(other.total_amount or 0.0)

            # 1. Exact Duplicate
            if o_num == inv_num and (o_email == c_email or o_name == c_name) and abs(o_tot - inv_tot) < 0.01:
                findings.append(AuditFinding(
                    finding_id=f"DUP-EXACT-{other.id}",
                    category="DUPLICATE",
                    severity=AuditSeverity.CRITICAL,
                    title="Exact Duplicate Invoice Detected",
                    description=(
                        f"An identical active invoice ({other.invoice_number}, ID #{other.id}) already exists for "
                        f"client '{other.customer_name}' with amount ${o_tot:.2f}."
                    ),
                    evidence=f"Matched active invoice #{other.id} ({other.invoice_number})",
                    recommendation="Cancel or archive this draft to prevent duplicate billing and double payment collection.",
                    field="invoice_number"
                ))
                found_exact = True
                break

            # 2. Same Number Different Content
            if o_num == inv_num:
                findings.append(AuditFinding(
                    finding_id=f"DUP-NUM-{other.id}",
                    category="DUPLICATE",
                    severity=AuditSeverity.CRITICAL,
                    title="Duplicate Invoice Number Conflict",
                    description=(
                        f"Invoice number '{invoice.invoice_number}' is already assigned to invoice ID #{other.id} "
                        f"(Client: {other.customer_name}, Amount: ${o_tot:.2f}). Invoice numbers must be globally unique."
                    ),
                    evidence=f"Collision with invoice ID #{other.id} on number '{invoice.invoice_number}'",
                    recommendation="Assign a new unique invoice numbering sequence.",
                    field="invoice_number"
                ))
                found_same_number = True
                break

            # 3. Possible Duplicate (Same client, same amount, within 3 days)
            if not found_possible and (o_email == c_email or (c_name and o_name == c_name)) and abs(o_tot - inv_tot) < 0.01:
                if invoice.issue_date and other.issue_date:
                    delta_days = abs((invoice.issue_date - other.issue_date).days)
                    if delta_days <= 3:
                        findings.append(AuditFinding(
                            finding_id=f"DUP-POSSIBLE-{other.id}",
                            category="DUPLICATE",
                            severity=AuditSeverity.MEDIUM,
                            title="Possible Duplicate Invoice Warning",
                            description=(
                                f"Existing invoice {other.invoice_number} (ID #{other.id}) was billed to this customer "
                                f"for the identical amount (${inv_tot:.2f}) within {delta_days} day(s)."
                            ),
                            evidence=f"Invoice #{other.id} issued {other.issue_date.strftime('%Y-%m-%d')} for ${o_tot:.2f}",
                            recommendation="Confirm that this is an independent billing cycle and not an accidental duplicate document.",
                            field="invoice_number"
                        ))
                        found_possible = True

        if not found_exact and not found_same_number and not found_possible:
            passed_checks.append("Duplicate Invoice Number & Amount Protection Cleared")

    @staticmethod
    def _detect_anomalies(
        db: Session, invoice: Invoice, calc_total: Decimal,
        findings: List[AuditFinding], passed_checks: List[str], evidence: Dict[str, Any]
    ):
        """
        Anomaly Detection:
        - Unusually high line item values.
        - Large discrepancies between line items and total.
        - Unusual quantity values (> 10,000).
        - Significant deviation from customer's previous billing history.
        """
        items = invoice.items or []
        for idx, item in enumerate(items, start=1):
            qty = item.quantity or 0.0
            price = item.unit_price or 0.0
            line_val = qty * price

            # Unusual quantity
            if qty > 10000:
                findings.append(AuditFinding(
                    finding_id=f"ANOM-QTY-{idx}",
                    category="ANOMALY",
                    severity=AuditSeverity.LOW,
                    title=f"Unusually High Quantity on Line #{idx}",
                    description=f"Line item #{idx} specifies {qty:,.0f} units, which exceeds typical freelance/SMB billing volumes.",
                    evidence=f"Quantity: {qty}",
                    recommendation="Verify unit quantity with contract or warehouse delivery order.",
                    field=f"items[{idx-1}].quantity"
                ))

            # Unusually high line value (> $50,000)
            if line_val > 50000.0:
                findings.append(AuditFinding(
                    finding_id=f"ANOM-VAL-{idx}",
                    category="ANOMALY",
                    severity=AuditSeverity.INFO,
                    title=f"High-Value Line Item on Line #{idx}",
                    description=f"Line item #{idx} is valued at ${line_val:,.2f}, which triggers enhanced high-value transaction monitoring.",
                    evidence=f"Line Total: ${line_val:,.2f}",
                    recommendation="Ensure high-value purchase order (PO) approval is on file before sending.",
                    field=f"items[{idx-1}].unit_price"
                ))

        # Customer historical invoice comparison
        if invoice.customer_id:
            historical_invoices = db.query(Invoice).filter(
                Invoice.customer_id == invoice.customer_id,
                Invoice.id != invoice.id,
                Invoice.is_deleted == False
            ).all()

            if len(historical_invoices) >= 2:
                hist_totals = [float(h.total_amount or 0.0) for h in historical_invoices]
                avg_tot = sum(hist_totals) / len(hist_totals)
                current_tot = float(calc_total)
                evidence["customer_historical_avg"] = avg_tot

                if current_tot > avg_tot * 3.5 and current_tot > 2000.0:
                    findings.append(AuditFinding(
                        finding_id="ANOM-HIST-001",
                        category="ANOMALY",
                        severity=AuditSeverity.LOW,
                        title="Significant Deviation from Client History",
                        description=(
                            f"Current invoice amount (${current_tot:,.2f}) is over 3.5× higher than this customer's "
                            f"historical average (${avg_tot:,.2f} across {len(historical_invoices)} prior invoices)."
                        ),
                        evidence=f"Current: ${current_tot:,.2f} vs Historical Avg: ${avg_tot:,.2f}",
                        recommendation="Confirm that project scope expansion or milestone delivery matches contract terms."
                    ))
                else:
                    passed_checks.append("Customer Billing Trend Consistent with History")
            else:
                passed_checks.append("Anomaly Profile Within Expected Operating Thresholds")
        else:
            passed_checks.append("Anomaly Profile Within Expected Operating Thresholds")

