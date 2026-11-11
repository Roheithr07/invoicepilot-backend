"""
AI Provider Abstraction Layer for InvoicePilot AI.
Supports:
1. Ollama (Default local free AI engine, zero cloud costs, 100% private)
2. Deterministic Fallback (Offline rule-based & regex financial parser, always available)
3. Hugging Face (Optional free-tier hosted inference)
4. Gemini (Optional legacy adapter, disabled by default, requires user API key)
"""

import abc
import json
import logging
import re
import time
from typing import Dict, Any, Optional, List
import httpx

from app.config import settings
from app.schemas.extraction import ExtractedInvoiceData, ExtractedItem

logger = logging.getLogger("InvoicePilot.AIProvider")

SYSTEM_INSTRUCTION = (
    "You are an expert financial invoice extraction system. Extract structured invoice data from the provided text. "
    "CRITICAL INSTRUCTIONS: NEVER invent missing numbers, emails, or names. If a field is not explicitly present in the text, return null. "
    "Output MUST be valid JSON conforming exactly to this structure:\n"
    "{\n"
    '  "invoice_number": string or null,\n'
    '  "customer_name": string or null,\n'
    '  "customer_email": string or null,\n'
    '  "invoice_date": "YYYY-MM-DD" or null,\n'
    '  "due_date": "YYYY-MM-DD" or null,\n'
    '  "currency": "USD",\n'
    '  "items": [{"description": string, "quantity": number, "unit_price": number, "tax_rate": number or null, "line_total": number or null}],\n'
    '  "subtotal": number or null,\n'
    '  "tax_amount": number or null,\n'
    '  "total_amount": number or null\n'
    "}"
)


class BaseAIProvider(abc.ABC):
    """Abstract Base Class for AI Extraction Providers."""

    @abc.abstractmethod
    def extract_invoice(self, raw_text: str) -> Optional[ExtractedInvoiceData]:
        """Extracts structured invoice data from document text."""
        pass

    @abc.abstractmethod
    def check_health(self) -> Dict[str, Any]:
        """Returns provider status, active model, and availability metrics."""
        pass


class OllamaProvider(BaseAIProvider):
    """
    Local Ollama Inference Engine.
    Uses locally hosted models (e.g. phi3:mini, llama3:latest, mistral:latest).
    Completely free, offline-capable, and private.
    """

    def __init__(self, base_url: Optional[str] = None, model: Optional[str] = None):
        self.base_url = (base_url or getattr(settings, "OLLAMA_BASE_URL", "http://localhost:11434")).rstrip("/")
        self.model = model or getattr(settings, "AI_MODEL", "phi3:mini")
        self.timeout = 45.0

    def check_health(self) -> Dict[str, Any]:
        try:
            with httpx.Client(timeout=3.0) as client:
                resp = client.get(f"{self.base_url}/api/tags")
                if resp.status_code == 200:
                    data = resp.json()
                    available_models = [m.get("name") for m in data.get("models", [])]
                    is_model_present = any(self.model in m for m in available_models)
                    return {
                        "status": "connected",
                        "provider": "ollama",
                        "engine_type": "local",
                        "base_url": self.base_url,
                        "configured_model": self.model,
                        "model_ready": is_model_present,
                        "available_models": available_models,
                        "deterministic_fallback_ready": True,
                        "cost": "FREE ($0.00 / Local CPU/GPU)",
                        "message": f"Ollama daemon is active at {self.base_url} with model '{self.model}'."
                    }
        except Exception as e:
            return {
                "status": "offline",
                "provider": "ollama",
                "engine_type": "local",
                "base_url": self.base_url,
                "configured_model": self.model,
                "model_ready": False,
                "available_models": [],
                "deterministic_fallback_ready": True,
                "cost": "FREE ($0.00)",
                "message": f"Ollama daemon is unreachable ({str(e)}). Deterministic arithmetic parser will handle extractions."
            }

    def extract_invoice(self, raw_text: str) -> Optional[ExtractedInvoiceData]:
        url = f"{self.base_url}/api/generate"
        prompt = f"{SYSTEM_INSTRUCTION}\n\nDOCUMENT TEXT:\n{raw_text[:7000]}"
        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "format": "json",
            "options": {
                "temperature": 0.0
            }
        }

        try:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.post(url, json=payload)
                if resp.status_code != 200:
                    logger.warning(f"Ollama returned HTTP {resp.status_code}: {resp.text[:200]}")
                    return None

                result_json = resp.json()
                raw_response = result_json.get("response", "")
                parsed = json.loads(raw_response)
                return self._parse_to_schema(parsed)
        except Exception as e:
            logger.warning(f"Ollama extraction failed ({str(e)}). Engaging deterministic fallback.")
            return None

    def _parse_to_schema(self, parsed: Dict[str, Any]) -> ExtractedInvoiceData:
        items: List[ExtractedItem] = []
        for it in parsed.get("items", []):
            try:
                items.append(ExtractedItem(
                    description=str(it.get("description") or "Service Item"),
                    quantity=float(it.get("quantity") or 1.0),
                    unit_price=float(it.get("unit_price") or 0.0),
                    tax_rate=float(it["tax_rate"]) if it.get("tax_rate") is not None else None,
                    line_total=float(it["line_total"]) if it.get("line_total") is not None else None
                ))
            except Exception:
                continue

        return ExtractedInvoiceData(
            invoice_number=parsed.get("invoice_number"),
            customer_name=parsed.get("customer_name"),
            customer_email=parsed.get("customer_email"),
            invoice_date=parsed.get("invoice_date"),
            due_date=parsed.get("due_date"),
            currency=parsed.get("currency") or "USD",
            items=items,
            subtotal=float(parsed["subtotal"]) if parsed.get("subtotal") is not None else None,
            tax_amount=float(parsed["tax_amount"]) if parsed.get("tax_amount") is not None else None,
            total_amount=float(parsed["total_amount"]) if parsed.get("total_amount") is not None else None,
            extraction_method=f"ollama_{self.model}"
        )


class DeterministicProvider(BaseAIProvider):
    """
    Zero-Cost, Zero-Network Deterministic Heuristic Extractor.
    Always active and guarantees extraction even in air-gapped or offline environments.
    """

    def check_health(self) -> Dict[str, Any]:
        return {
            "status": "connected",
            "provider": "deterministic",
            "engine_type": "rule_based_heuristics",
            "configured_model": "heuristic_regex_v2",
            "model_ready": True,
            "deterministic_fallback_ready": True,
            "cost": "FREE ($0.00 / Local)",
            "message": "Deterministic regex and Decimal arithmetic extraction engine is active and operational."
        }

    def extract_invoice(self, raw_text: str) -> Optional[ExtractedInvoiceData]:
        # Handled in AIExtractor._heuristic_regex_parser
        return None


class HuggingFaceProvider(BaseAIProvider):
    """
    Hugging Face Free Inference API Provider.
    Optionally configured with a free Hugging Face API token.
    """

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None):
        self.api_key = api_key or getattr(settings, "AI_API_KEY", "")
        self.model = model or getattr(settings, "AI_MODEL", "meta-llama/Meta-Llama-3-8B-Instruct")

    def check_health(self) -> Dict[str, Any]:
        has_key = bool(self.api_key and self.api_key.strip())
        return {
            "status": "connected" if has_key else "unconfigured",
            "provider": "huggingface",
            "engine_type": "cloud_free_tier",
            "configured_model": self.model,
            "model_ready": has_key,
            "deterministic_fallback_ready": True,
            "cost": "FREE (Hugging Face Serverless Free Tier)",
            "message": "Hugging Face inference configured." if has_key else "Hugging Face API token not provided. Using deterministic fallback."
        }

    def extract_invoice(self, raw_text: str) -> Optional[ExtractedInvoiceData]:
        if not self.api_key:
            return None
        # Call HuggingFace Inference API router
        url = f"https://api-inference.huggingface.co/models/{self.model}"
        headers = {"Authorization": f"Bearer {self.api_key}"}
        prompt = f"<s>[INST] {SYSTEM_INSTRUCTION}\n\nDOCUMENT TEXT:\n{raw_text[:5000]} [/INST]"
        try:
            with httpx.Client(timeout=20.0) as client:
                resp = client.post(url, headers=headers, json={"inputs": prompt, "parameters": {"max_new_tokens": 1024, "temperature": 0.01}})
                if resp.status_code == 200:
                    data = resp.json()
                    out = data[0].get("generated_text", "") if isinstance(data, list) else data.get("generated_text", "")
                    # Extract JSON substring
                    json_match = re.search(r"\{.*\}", out, re.DOTALL)
                    if json_match:
                        parsed = json.loads(json_match.group(0))
                        return OllamaProvider()._parse_to_schema(parsed)
        except Exception as e:
            logger.warning(f"HuggingFace extraction error: {e}")
        return None


class GeminiProvider(BaseAIProvider):
    """
    Optional Legacy Gemini Adapter.
    Disabled by default; only utilized if user explicitly configures AI_PROVIDER=gemini and AI_API_KEY.
    """

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None):
        self.api_key = api_key or getattr(settings, "AI_API_KEY", "")
        self.model = model or getattr(settings, "AI_MODEL", "gemini-1.5-pro")

    def check_health(self) -> Dict[str, Any]:
        has_key = bool(self.api_key and self.api_key.strip())
        return {
            "status": "connected" if has_key else "unconfigured",
            "provider": "gemini",
            "engine_type": "optional_cloud",
            "configured_model": self.model,
            "model_ready": has_key,
            "deterministic_fallback_ready": True,
            "cost": "Optional (Requires User Gemini API Key)",
            "message": "Legacy Gemini adapter configured." if has_key else "Gemini is optional and unconfigured. Free local Ollama or deterministic fallback is active."
        }

    def extract_invoice(self, raw_text: str) -> Optional[ExtractedInvoiceData]:
        if not self.api_key:
            return None
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent?key={self.api_key}"
        prompt_payload = {
            "contents": [{"parts": [{"text": f"{SYSTEM_INSTRUCTION}\n\nDOCUMENT TEXT:\n{raw_text[:7000]}"}]}],
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0.0}
        }
        try:
            with httpx.Client(timeout=20.0) as client:
                resp = client.post(url, json=prompt_payload)
                if resp.status_code == 200:
                    parsed = json.loads(resp.json()["candidates"][0]["content"]["parts"][0]["text"])
                    return OllamaProvider()._parse_to_schema(parsed)
        except Exception as e:
            logger.warning(f"Gemini API error: {e}")
        return None


class AIProviderManager:
    """Manages active AI provider instance with automatic deterministic fallback."""

    @staticmethod
    def get_provider() -> BaseAIProvider:
        provider_name = (getattr(settings, "AI_PROVIDER", "ollama") or "ollama").lower().strip()
        if provider_name == "ollama":
            return OllamaProvider()
        elif provider_name == "deterministic":
            return DeterministicProvider()
        elif provider_name == "huggingface":
            return HuggingFaceProvider()
        elif provider_name == "gemini":
            return GeminiProvider()
        # Default safe fallback
        return OllamaProvider()

    @staticmethod
    def get_health() -> Dict[str, Any]:
        provider = AIProviderManager.get_provider()
        health = provider.check_health()
        # Add summary info
        health["active_provider_setting"] = getattr(settings, "AI_PROVIDER", "ollama")
        health["free_tier_compliant"] = True
        return health
