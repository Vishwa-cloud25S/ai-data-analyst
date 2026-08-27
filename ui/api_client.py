"""HTTP client for the analyst API.

Kept separate from the Streamlit script so failures are surfaced explicitly
rather than being swallowed into a half-rendered page, and so CI can test the
failure paths without a browser session.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import requests

HEALTH_KEYS = {"status", "warehouse", "llm", "entities", "metrics"}


class ApiError(Exception):
    """Raised when the API is unreachable, errors, or is not our API."""


def normalise_base_url(url: str) -> str:
    """Accept 'host:port' as well as full URLs.

    Render's `fromService: property: hostport` injects a bare host:port with no
    scheme, and requests refuses such URLs outright. Defaulting to http:// keeps
    private-network service discovery working.
    """
    url = (url or "").strip().rstrip("/")
    if not url:
        return ""
    if "://" not in url:
        url = f"http://{url}"
    return url


@dataclass
class ApiClient:
    base_url: str
    timeout: int = 120
    api_key: str | None = None

    def __post_init__(self) -> None:
        self.base_url = normalise_base_url(self.base_url)

    def _request(self, method: str, path: str, **kwargs) -> Any:
        url = f"{self.base_url.rstrip('/')}{path}"
        headers = dict(kwargs.pop("headers", {}) or {})
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        try:
            resp = requests.request(method, url, timeout=self.timeout,
                                    headers=headers, **kwargs)
        except requests.exceptions.ConnectionError as exc:
            raise ApiError(
                f"Cannot reach the API at {self.base_url}. Is it running?\n\n"
                f"Start it with:  uvicorn app.main:app --port 8000"
            ) from exc
        except requests.exceptions.Timeout as exc:
            raise ApiError(f"The API at {url} timed out after {self.timeout}s.") from exc
        except requests.exceptions.RequestException as exc:
            # Malformed URL, bad redirect, TLS failure - never surface a raw traceback.
            raise ApiError(f"Request to {url} failed: {type(exc).__name__}: {exc}") from exc

        if resp.status_code >= 400:
            detail = ""
            try:
                detail = resp.json().get("detail", "")
            except Exception:
                detail = (resp.text or "")[:200]
            if resp.status_code == 401:
                raise ApiError(
                    "The API rejected the credentials. Set API_KEY for this UI to a "
                    "key configured in the API's API_KEYS."
                )
            if resp.status_code == 403:
                raise ApiError(f"Not permitted: {detail or 'insufficient role'}.")
            if resp.status_code == 404:
                raise ApiError(
                    f"{url} returned 404 Not Found.\n\n"
                    f"Something is listening on {self.base_url}, but it is not the "
                    f"AI Data Analyst API - most likely another service is using that "
                    f"port, or the API was started from a different project.\n\n"
                    f"Check with:  curl {self.base_url}/health"
                )
            raise ApiError(f"{url} returned HTTP {resp.status_code}. {detail}")

        try:
            return resp.json()
        except ValueError as exc:
            raise ApiError(
                f"{url} did not return JSON. Received: {(resp.text or '')[:120]!r}"
            ) from exc

    def health(self) -> dict:
        data = self._request("GET", "/health")
        missing = HEALTH_KEYS - set(data)
        if missing:
            raise ApiError(
                f"{self.base_url}/health responded, but the payload is not from this "
                f"API (missing: {', '.join(sorted(missing))}). Received: {data}"
            )
        return data

    def get(self, path: str) -> Any:
        return self._request("GET", path)

    def post_json(self, path: str, payload: dict) -> Any:
        return self._request("POST", path, json=payload)

    def put_json(self, path: str, payload: dict) -> Any:
        return self._request("PUT", path, json=payload)

    def ask(self, question: str, use_llm: bool) -> dict:
        data = self._request("POST", "/ask",
                             json={"question": question, "use_llm": use_llm})
        if not isinstance(data, dict) or "status" not in data:
            raise ApiError(f"Unexpected /ask payload from the API: {data}")
        return data
