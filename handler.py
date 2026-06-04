from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from html import unescape
from typing import Any
from urllib.parse import urlparse

import httpx

from intent_handler import BaseIntentHandler, IntentHandlerResult
from utils import get_logger

logger = get_logger(__name__)


class UrlFetcherIntentHandler(BaseIntentHandler):
    """Fetch trusted URL content and publish structured facts for downstream steps."""

    _ALLOWED_DOMAINS = {
        "hebcal.com",
        "www.hebcal.com",
    }

    async def get_prompt_runtime_replacements(
        self,
        query,
        *,
        framework_context=None,
        route_result=None,
    ) -> dict[str, str]:
        return {}

    async def handle(
        self,
        query,
        *,
        route_result=None,
        framework_context: str = None,
        raw_response: IntentHandlerResult | None = None,
        tool_calls=None,
    ) -> IntentHandlerResult | None:
        response = raw_response
        if response is None:
            return None

        # Suppress verbose model output. Structured payload is published via step_contexts.
        response.set_output({"text": "Source fetched and context prepared."})
        response.set_route_result(route_result=route_result)
        return response

    async def get_step_context_update(
        self,
        *,
        query: str,
        route_result=None,
        framework_context: str | None = None,
        result: IntentHandlerResult | None = None,
    ) -> dict[str, Any] | None:
        url = self._extract_url(query)
        if not url:
            return {
                "url_fetch": {
                    "status": "unresolved",
                    "reason": "missing_url",
                }
            }

        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if parsed.scheme not in {"https", "http"}:
            return {
                "url_fetch": {
                    "status": "blocked",
                    "reason": "invalid_scheme",
                    "source_url": url,
                }
            }

        if not self._is_domain_allowed(host):
            return {
                "url_fetch": {
                    "status": "blocked",
                    "reason": "domain_not_allowlisted",
                    "source_url": url,
                    "host": host,
                }
            }

        timeout = httpx.Timeout(10.0, connect=5.0)
        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
                response = await client.get(url)
                response.raise_for_status()
        except Exception as exc:
            logger.warning("URL fetch failed: %s", exc)
            return {
                "url_fetch": {
                    "status": "error",
                    "source_url": url,
                    "error": str(exc),
                }
            }

        content_type = response.headers.get("content-type", "").lower()
        body = response.text[:120000]

        extracted_text = self._extract_text(body, content_type)
        facts = self._extract_facts(extracted_text, content_type, body)

        return {
            "url_fetch": {
                "status": "resolved",
                "source_url": str(response.url),
                "host": host,
                "fetched_at": datetime.now(timezone.utc).isoformat(),
                "content_type": content_type,
                "status_code": response.status_code,
                "title": self._extract_title(body),
                "facts": facts,
                "excerpt": extracted_text[:800],
            }
        }

    def _extract_url(self, query: str) -> str | None:
        match = re.search(r"https?://[^\s)\]>]+", query)
        return match.group(0) if match else None

    def _is_domain_allowed(self, host: str) -> bool:
        if host in self._ALLOWED_DOMAINS:
            return True
        return any(host.endswith(f".{allowed}") for allowed in self._ALLOWED_DOMAINS)

    def _extract_title(self, body: str) -> str | None:
        title_match = re.search(r"<title[^>]*>(.*?)</title>", body, flags=re.IGNORECASE | re.DOTALL)
        if not title_match:
            return None
        title = unescape(title_match.group(1))
        return re.sub(r"\s+", " ", title).strip()[:200]

    def _extract_text(self, body: str, content_type: str) -> str:
        if "application/json" in content_type:
            try:
                payload = json.loads(body)
                return json.dumps(payload, ensure_ascii=True)[:8000]
            except Exception:
                return body[:8000]

        cleaned = re.sub(r"<script[\s\S]*?</script>", " ", body, flags=re.IGNORECASE)
        cleaned = re.sub(r"<style[\s\S]*?</style>", " ", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"<[^>]+>", " ", cleaned)
        cleaned = unescape(cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        return cleaned[:8000]

    def _extract_facts(self, extracted_text: str, content_type: str, body: str) -> list[dict[str, Any]]:
        facts: list[dict[str, Any]] = []

        date_matches = re.findall(r"\b\d{4}-\d{2}-\d{2}\b", extracted_text)
        seen_dates = set()
        for dt in date_matches:
            if dt in seen_dates:
                continue
            seen_dates.add(dt)
            facts.append({"key": "date", "value": dt, "confidence": 0.7})
            if len(facts) >= 8:
                break

        if "application/json" in content_type:
            try:
                payload = json.loads(body)
                if isinstance(payload, dict):
                    for key in ("title", "date"):
                        if key in payload:
                            facts.append({"key": key, "value": payload[key], "confidence": 0.8})
            except Exception:
                pass

        return facts
