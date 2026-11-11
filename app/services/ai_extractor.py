"""
AI Invoice Extraction & Arithmetic Validation Service for InvoicePilot AI.
Converts raw extracted document text into structured invoice schemas with strict
Pydantic validation, Decimal verification, and zero hallucination guardrails.
"""

import re
import json
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from typing import Optional, Dict, Any, List
import httpx
from app.config import settings
from app.schemas.extraction import ExtractedInvoiceData, ExtractedItem


def parse_decimal_safe(val: Any) -> Optional[Decimal]:
    """Safely parses float/str to Decimal without precision loss."""
    if val is None or val == "":
        return None
    try:
        cleaned = str(val).replace("$", "").replace(",", "").strip()
        return Decimal(cleaned)
    except (InvalidOperation, ValueError):
        return None


def round_decimal(val: Decimal) -> float:
    return float(val.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


class AIExtractor:
    @staticmethod
    def extract_structured_data(raw_text: str, use_ai: bool = True) -> ExtractedInvoiceData:
        """
        Orchestrates structured extraction.
        Prioritizes configured free AI engine (Ollama) or active provider,
        falling back gracefully and deterministically to zero-dependency heuristic parsing.
        """
        if not raw_text or not raw_text.strip():
            return ExtractedInvoiceData(
                invoice_number=None,
                currency="USD",
                missing_fields=["invoice_number", "customer_name", "customer_email", "total_amount"],
                warnings=["Document contains no readable text. Manual entry or OCR is required."],
                raw_text="",
                extraction_method="empty_document_fallback"
            )

        extracted_data: Optional[ExtractedInvoiceData] = None

        # 1. Attempt LLM extraction via provider abstraction (Ollama/HF/Deterministic/Gemini)
        if use_ai:
            try:
                from app.services.ai_provider import AIProviderManager
                provider = AIProviderManager.get_provider()
                extracted_data = provider.extract_invoice(raw_text)
            except Exception:
                extracted_data = None

        # 2. Heuristic extraction fallback (guarantees zero-failure extraction)
        if extracted_data is None:
            extracted_data = AIExtractor._heuristic_regex_parser(raw_text)

        # 3. Deterministic Arithmetic Validation & Field Auditing
        return AIExtractor._validate_and_reconcile_arithmetic(extracted_data, raw_text)

    @staticmethod
    def _call_llm_provider(raw_text: str) -> ExtractedInvoiceData:
        """
        Delegates to configured AIProviderManager or legacy provider.
        """
        from app.services.ai_provider import AIProviderManager
        provider = AIProviderManager.get_provider()
        res = provider.extract_invoice(raw_text)
        if res is not None:
            return res
        return AIExtractor._heuristic_regex_parser(raw_text)

    @staticmethod
    def _heuristic_regex_parser(text: str) -> ExtractedInvoiceData:
        """
        Deterministic regex and pattern extraction engine.
        Acts as reliable fallback when no AI key is active or when offline.
        """
        lines = [l.strip() for l in text.split("\n") if l.strip()]
        
        # 1. Invoice Number search - prioritize explicit label with delimiter
        inv_match = re.search(r"(?:invoice\s*(?:#|no\.?|number|id)\s*[:#]\s*)([a-zA-Z0-9\-_/]{3,30})", text, re.IGNORECASE)
        if not inv_match:
            inv_match = re.search(r"(?:invoice\s*(?:#|no\.?|number)\s+)([a-zA-Z0-9\-_/]{3,30})", text, re.IGNORECASE)
        if not inv_match:
            inv_match = re.search(r"(?:inv\s*[:#\s]+)([a-zA-Z0-9\-_/]{3,30})", text, re.IGNORECASE)
        invoice_number = inv_match.group(1).strip() if inv_match else None

        # 2. Email search
        email_match = re.search(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+", text)
        customer_email = email_match.group(0).strip() if email_match else None

        # 3. Dates search (YYYY-MM-DD or MM/DD/YYYY or DD/MM/YYYY)
        date_matches = re.findall(r"(\d{4}[-/]\d{1,2}[-/]\d{1,2})|(\d{1,2}[-/]\d{1,2}[-/]\d{4})", text)
        dates_found = []
        for m in date_matches:
            d_str = m[0] or m[1]
            dates_found.append(d_str.replace("/", "-"))

        invoice_date = dates_found[0] if len(dates_found) > 0 else None
        due_date = dates_found[1] if len(dates_found) > 1 else None

        # 4. Currency Search
        currency = "USD"
        if "EUR" in text or "€" in text:
            currency = "EUR"
        elif "GBP" in text or "£" in text:
            currency = "GBP"
        elif "CAD" in text:
            currency = "CAD"

        # 5. Financial Totals (Subtotal, Tax, Total) - ensure word boundaries so "subtotal" doesn't trigger "total"
        subtotal_match = re.search(r"\b(?:subtotal|net amount|sub-total)[:\s]*\$?\s*([0-9,]+\.[0-9]{2})", text, re.IGNORECASE)
        subtotal = float(subtotal_match.group(1).replace(",", "")) if subtotal_match else None

        total_match = re.search(r"\b(?:grand\s*total|total\s*amount|balance\s*due|amount\s*due|(?<!sub)total)[:\s]*\$?\s*([0-9,]+\.[0-9]{2})", text, re.IGNORECASE)
        total_amount = float(total_match.group(1).replace(",", "")) if total_match else None

        tax_match = re.search(r"\b(?:tax|vat|gst|sales\s*tax)[:\s]*\$?\s*([0-9,]+\.[0-9]{2})", text, re.IGNORECASE)
        tax_amount = float(tax_match.group(1).replace(",", "")) if tax_match else None

        # 6. Customer Name detection (look for "Bill To" or "Client" context)
        customer_name = None
        for i, line in enumerate(lines):
            if re.search(r"(?:bill\s*to|billed\s*to|client|customer)[:\s]*$", line, re.IGNORECASE):
                if i + 1 < len(lines):
                    customer_name = lines[i + 1]
                    break
            elif re.search(r"(?:bill\s*to|billed\s*to|client)[:\s]+(.+)", line, re.IGNORECASE):
                customer_name = re.sub(r"(?:bill\s*to|billed\s*to|client)[:\s]+", "", line, flags=re.IGNORECASE).strip()
                break

        # 7. Line Items extraction (simple table pattern: Description, Qty, Price, Total)
        items: List[ExtractedItem] = []
        for line in lines:
            line_item_match = re.search(r"^(.+?)\s+(\d+)\s+[\$]?([0-9,]+\.[0-9]{2})\s+[\$]?([0-9,]+\.[0-9]{2})$", line)
            if line_item_match:
                desc = line_item_match.group(1).strip()
                qty = float(line_item_match.group(2))
                price = float(line_item_match.group(3).replace(",", ""))
                ltotal = float(line_item_match.group(4).replace(",", ""))
                items.append(ExtractedItem(
                    description=desc,
                    quantity=qty,
                    unit_price=price,
                    line_total=ltotal
                ))

        if not items and total_amount:
            # Fallback single line item if totals found
            items.append(ExtractedItem(
                description="General Services (Extracted from document total)",
                quantity=1.0,
                unit_price=subtotal if subtotal else total_amount,
                line_total=subtotal if subtotal else total_amount
            ))

        return ExtractedInvoiceData(
            invoice_number=invoice_number,
            customer_name=customer_name,
            customer_email=customer_email,
            invoice_date=invoice_date,
            due_date=due_date,
            currency=currency,
            items=items,
            subtotal=subtotal,
            tax_amount=tax_amount,
            total_amount=total_amount,
            extraction_method="deterministic_pattern_parser"
        )

    @staticmethod
    def _validate_and_reconcile_arithmetic(data: ExtractedInvoiceData, raw_text: str) -> ExtractedInvoiceData:
        """
        Runs independent Decimal calculations and verifies mathematical consistency.
        Flags missing required fields and computes per-field confidence scores.
        """
        confidence: Dict[str, float] = {}
        missing_fields: List[str] = []
        warnings: List[str] = []

        # Field presence checks
        if data.invoice_number:
            confidence["invoice_number"] = 0.95
        else:
            missing_fields.append("invoice_number")
            confidence["invoice_number"] = 0.0

        if data.customer_name:
            confidence["customer_name"] = 0.85
        else:
            missing_fields.append("customer_name")
            confidence["customer_name"] = 0.0

        if data.customer_email:
            confidence["customer_email"] = 0.95
        else:
            missing_fields.append("customer_email")
            confidence["customer_email"] = 0.0

        if data.due_date:
            confidence["due_date"] = 0.85
        else:
            missing_fields.append("due_date")
            confidence["due_date"] = 0.0

        # Perform Backend Decimal Arithmetic
        items_subtotal_dec = Decimal("0.00")
        for item in data.items:
            qty_dec = parse_decimal_safe(item.quantity) or Decimal("1.0")
            price_dec = parse_decimal_safe(item.unit_price) or Decimal("0.0")
            line_sub = (qty_dec * price_dec).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            
            if item.line_total is not None:
                stated_line_total = parse_decimal_safe(item.line_total)
                if stated_line_total and abs(stated_line_total - line_sub) > Decimal("0.05"):
                    warnings.append(
                        f"Line item '{item.description}' calculation discrepancy: "
                        f"{qty_dec} * ${price_dec} = ${line_sub}, but document stated ${stated_line_total}."
                    )
            items_subtotal_dec += line_sub

        calculated_subtotal = round_decimal(items_subtotal_dec)
        
        # Check subtotal
        stated_subtotal_dec = parse_decimal_safe(data.subtotal)
        if stated_subtotal_dec is not None:
            if abs(stated_subtotal_dec - items_subtotal_dec) > Decimal("0.05"):
                warnings.append(
                    f"Subtotal discrepancy: Sum of items (${calculated_subtotal}) differs from stated subtotal (${stated_subtotal_dec})."
                )
        elif items_subtotal_dec > 0:
            data.subtotal = calculated_subtotal

        # Check total
        tax_dec = parse_decimal_safe(data.tax_amount) or Decimal("0.00")
        expected_total_dec = items_subtotal_dec + tax_dec
        stated_total_dec = parse_decimal_safe(data.total_amount)

        if stated_total_dec is not None:
            if abs(stated_total_dec - expected_total_dec) > Decimal("0.05"):
                warnings.append(
                    f"Total amount mismatch: Subtotal (${items_subtotal_dec}) + Tax (${tax_dec}) = "
                    f"${expected_total_dec}, but document stated ${stated_total_dec}."
                )
                confidence["total_amount"] = 0.50
            else:
                confidence["total_amount"] = 0.98
        else:
            missing_fields.append("total_amount")
            confidence["total_amount"] = 0.0
            if expected_total_dec > 0:
                data.total_amount = round_decimal(expected_total_dec)
                warnings.append(f"Derived invoice total from line items and tax: ${data.total_amount}.")

        data.confidence = confidence
        data.missing_fields = missing_fields
        data.warnings = warnings
        data.raw_text = raw_text[:500] if raw_text else ""
        return data
