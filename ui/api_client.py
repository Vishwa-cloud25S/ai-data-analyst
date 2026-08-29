"""HTTP client for the analyst API.

Kept separate from the Streamlit script so failures are surfaced explicitly
rather than being swallowed into a half-rendered page, and so CI can test the
failure paths without a browser session.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

import requests

log = logging.getLogger(__name__)

HEALTH_KEYS = {"status", "warehouse", "llm", "entities", "metrics"}

# Gateway errors mean "the upstream is not ready yet" on managed platforms: a
# sleeping free-tier instance answers 502 from the load balancer while it
# spins up (30-60s). Retrying a couple of times with a pause covers the
# common case; if it still fails the message says "waking up", not "broken".
GATEWAY_CODES = (502, 503, 504)
WAKE_DELAYS = (3.0, 8.0)
HOSTED_SUFFIXES = (".onrender.com", ".herokuapp.com", ".fly.dev", ".railway.app")


class ApiError(Exception):
    """Raised when the API is unreachable, errors, or is not our API."""


def fallback_base_urls(url: str) -> list[str]:
    """Public URLs to try when a private/internal address does not resolve.

    Render's `fromService: hostport` yields a dotless internal name such as
    `ai-data-analyst-api-krvg:10000`, which free instances cannot reach. Rather
    than leave the UI dead until someone edits an environment variable, try the
    public equivalent of the same service. Only ever attempted for a *dotless*
    host - a real hostname is never second-guessed.
    """
    from urllib.parse import urlparse

    parsed = urlparse(normalise_base_url(url))
    host = (parsed.hostname or "").strip()
    if not host or "." in host or host in ("localhost", "api", "ui"):
        return []
    return [f"https://{host}.onrender.com"]


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
        self._fallback_checked = False
        self._resolved: str | None = None

    def _unreachable_message(self) -> str:
        """Advice that fits where the app is actually running."""
        local = any(h in self.base_url for h in ("localhost", "127.0.0.1"))
        hosted = any(h in self.base_url for h in (".onrender.com", "herokuapp",
                                                  ".fly.dev", ".railway.app"))
        if local:
            hint = "Start it with:  uvicorn app.main:app --port 8000"
        elif hosted:
            hint = ("Free-tier services sleep after inactivity and can take a minute "
                    "to wake. Press Retry in a moment. If it never comes back, check "
                    "the service is deployed and healthy.")
        else:
            hint = ("If this is a deployment, API_URL is probably wrong. It must be a "
                    "URL this container can reach, e.g. https://<service>.onrender.com "
                    "- a private-network address like host:10000 will not resolve "
                    "where private networking is unavailable (Render free tier).")
        return f"Cannot reach the API at {self.base_url}. Is it running?\n\n{hint}"

    def _is_hosted(self) -> bool:
        """True on platforms where a sleeping instance answers 502 while waking."""
        from urllib.parse import urlparse

        host = (urlparse(self.base_url).hostname or "").lower()
        return any(host.endswith(suffix) for suffix in HOSTED_SUFFIXES)

    def _resolve_fallback(self) -> str | None:
        """Find a reachable public equivalent of an unreachable internal host.

        A sleeping free-tier service answers nothing at all - the connection
        times out rather than returning 502 - so a single probe fails on exactly
        the occasion the fallback is most needed. Retry a couple of times, and
        accept *any* HTTP response: a 502 from a waking instance still proves
        the host exists and routes.
        """
        if self._fallback_checked:
            return self._resolved
        self._fallback_checked = True
        probe_timeout = min(self.timeout, 25)
        for candidate in fallback_base_urls(self.base_url):
            for attempt in range(2):
                try:
                    probe = requests.get(f"{candidate}/health", timeout=probe_timeout)
                    if probe.status_code < 600:
                        log.warning(
                            "API_URL %s is unreachable; using %s instead. "
                            "Set API_URL to that value to remove this guesswork.",
                            self.base_url, candidate,
                        )
                        self._resolved = candidate
                        return candidate
                except requests.exceptions.RequestException:
                    if attempt == 0:
                        time.sleep(1.5)   # a waking instance needs a moment
        return None

    def _request(self, method: str, path: str, **kwargs) -> Any:
        url = f"{self.base_url.rstrip('/')}{path}"
        headers = dict(kwargs.pop("headers", {}) or {})
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        # A sleeping free-tier instance answers 502 from the load balancer
        # while it wakes (30-60s on Render). The first hit keeps the boot in
        # flight and the next one gets through - so retry a couple of times
        # before reporting it. Local servers are exempt: a 502 there is a
        # real error, not a cold start.
        attempts = 3 if self._is_hosted() else 1
        for attempt in range(attempts):
            try:
                resp = requests.request(method, url, timeout=self.timeout,
                                        headers=headers, **kwargs)
            except (requests.exceptions.ConnectionError,
                    requests.exceptions.ReadTimeout) as exc:
                resolved = self._resolve_fallback()
                if resolved and resolved != self.base_url:
                    self.base_url = resolved
                    return self._request(method, path, **kwargs)
                raise ApiError(self._unreachable_message()) from exc
            except requests.exceptions.Timeout as exc:
                raise ApiError(f"The API at {url} timed out after {self.timeout}s.") from exc
            except requests.exceptions.RequestException as exc:
                # Malformed URL, bad redirect, TLS failure - never a raw traceback.
                raise ApiError(f"Request to {url} failed: {type(exc).__name__}: {exc}") from exc

            if resp.status_code in GATEWAY_CODES and attempt + 1 < attempts:
                time.sleep(WAKE_DELAYS[min(attempt, len(WAKE_DELAYS) - 1)])
                continue
            break

        if resp.status_code in GATEWAY_CODES and self._is_hosted():
            raise ApiError(
                f"The API at {self.base_url} is still waking up "
                f"(HTTP {resp.status_code}). Free-tier services sleep after "
                "inactivity and can take up to a minute to start. Wait a few "
                "seconds, then press Retry connection."
            )

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
