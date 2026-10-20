"""Async client for the Tin Can cloud API.

Auth: WordPress "JWT Authentication for WP REST API" at archie.tincan.kids.
  POST /wp-json/jwt-auth/v1/token          {username, password} -> token (+ refresh_token)
  POST /wp-json/jwt-auth/v1/token/refresh  {refresh_token}      -> token
Data: https://api.tincan.com with "Authorization: Bearer <token>".

Mirrors the proven synchronous reference client (tc_api.py) using aiohttp.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from collections.abc import Callable
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)

AUTH_BASE = "https://archie.tincan.kids/wp-json/jwt-auth/v1"
API_BASE = "https://api.tincan.com"
USER_AGENT = "okhttp/4.12.0"
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=20)
TOKEN_MARGIN = 60  # refresh when fewer than this many seconds remain


class TinCanError(Exception):
    """Base error."""


class TinCanAuthError(TinCanError):
    """Credentials rejected (login failed, or Bearer refused)."""


class TinCanConnectionError(TinCanError):
    """Network / server problem."""


def _jwt_exp(token: str) -> int:
    """Return the JWT exp claim (unix seconds), 0 if it cannot be decoded."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return int(json.loads(base64.urlsafe_b64decode(payload)).get("exp", 0))
    except (IndexError, ValueError, json.JSONDecodeError):
        return 0


def _unwrap(body: Any) -> dict[str, Any]:
    """Auth responses are seen both as {token,...} and {data: {token,...}}."""
    if isinstance(body, dict):
        data = body.get("data")
        if isinstance(data, dict) and ("token" in data or "refresh_token" in data):
            return data
        return body
    return {}


class TinCanClient:
    """Minimal read-only client with proactive token renewal."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        email: str,
        password: str,
        refresh_token: str | None = None,
        on_token_update: Callable[[str | None], None] | None = None,
    ) -> None:
        self._session = session
        self._email = email
        self._password = password
        self._refresh_token = refresh_token
        self._on_token_update = on_token_update
        self._token: str | None = None
        self._exp = 0
        self._lock = asyncio.Lock()

    @property
    def refresh_token(self) -> str | None:
        """Current refresh token (persist this across restarts)."""
        return self._refresh_token

    # ------------------------------------------------------------------ http
    async def _request(
        self,
        method: str,
        url: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        bearer: str | None = None,
    ) -> tuple[int, Any]:
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"
        try:
            async with self._session.request(
                method,
                url,
                json=json_body,
                params=params,
                headers=headers,
                timeout=REQUEST_TIMEOUT,
            ) as resp:
                try:
                    body = await resp.json(content_type=None)
                except (aiohttp.ContentTypeError, json.JSONDecodeError, ValueError):
                    body = {"_raw": (await resp.text())[:500]}
                return resp.status, body
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise TinCanConnectionError(f"{method} {url}: {err}") from err

    # ------------------------------------------------------------------ auth
    def _store_token(self, token: str, refresh_token: str | None) -> None:
        self._token = token
        self._exp = _jwt_exp(token)
        if refresh_token and refresh_token != self._refresh_token:
            self._refresh_token = refresh_token
            if self._on_token_update is not None:
                self._on_token_update(refresh_token)

    async def async_login(self) -> None:
        """Password login. Raises TinCanAuthError on bad credentials."""
        status, body = await self._request(
            "POST",
            f"{AUTH_BASE}/token",
            json_body={"username": self._email, "password": self._password},
        )
        data = _unwrap(body)
        token = data.get("token")
        if status == 200 and token:
            self._store_token(
                token, data.get("refresh_token") or data.get("refreshToken")
            )
            _LOGGER.debug(
                "Login ok; token valid for ~%d min", (self._exp - time.time()) / 60
            )
            return
        message = body.get("message") if isinstance(body, dict) else body
        if status >= 500:
            raise TinCanConnectionError(f"login failed [{status}]: {message}")
        raise TinCanAuthError(f"login failed [{status}]: {message}")

    async def _async_try_refresh(self) -> bool:
        """Renew the JWT with the refresh token. False if the server refused."""
        if not self._refresh_token:
            return False
        status, body = await self._request(
            "POST",
            f"{AUTH_BASE}/token/refresh",
            json_body={"refresh_token": self._refresh_token},
        )
        data = _unwrap(body)
        token = data.get("token")
        if status == 200 and token:
            self._store_token(token, data.get("refresh_token") or self._refresh_token)
            _LOGGER.debug("Token refreshed; ~%d min", (self._exp - time.time()) / 60)
            return True
        if status >= 500:
            raise TinCanConnectionError(f"refresh failed [{status}]")
        _LOGGER.debug("Refresh token rejected [%s]; falling back to login", status)
        self._refresh_token = None
        return False

    async def async_ensure_token(self) -> str:
        """Return a Bearer token with at least TOKEN_MARGIN seconds left."""
        async with self._lock:
            if self._token and self._exp > time.time() + TOKEN_MARGIN:
                return self._token
            if await self._async_try_refresh():
                return self._token  # type: ignore[return-value]
            await self.async_login()
            return self._token  # type: ignore[return-value]

    def invalidate_token(self) -> None:
        """Forget the cached JWT so the next call re-authenticates."""
        self._token = None
        self._exp = 0

    # ------------------------------------------------------------------ data
    async def async_get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """Authenticated GET against api.tincan.com; one retry after a 401."""
        token = await self.async_ensure_token()
        status, body = await self._request(
            "GET", f"{API_BASE}{path}", params=params, bearer=token
        )
        if status == 401:
            self.invalidate_token()
            token = await self.async_ensure_token()
            status, body = await self._request(
                "GET", f"{API_BASE}{path}", params=params, bearer=token
            )
        if status == 401:
            raise TinCanAuthError(f"GET {path}: unauthorized")
        if status >= 500:
            raise TinCanConnectionError(f"GET {path}: HTTP {status}")
        if status != 200:
            raise TinCanError(f"GET {path}: HTTP {status}: {str(body)[:200]}")
        return body

    async def async_subscriptions(self) -> list[dict[str, Any]]:
        """GET /subscriptions -> the account's Tin Can lines."""
        body = await self.async_get("/subscriptions")
        data = body.get("data") if isinstance(body, dict) else None
        return data if isinstance(data, list) else []

    async def async_devices(self) -> list[dict[str, Any]]:
        """GET /account/devices -> physical phones (uuid, name, mac, provisioning)."""
        body = await self.async_get("/account/devices")
        data = body.get("data") if isinstance(body, dict) else None
        return data if isinstance(data, list) else []

    async def async_contacts(self, subscription_id: str) -> list[dict[str, Any]]:
        """GET /subscription/{id}/contacts -> the approved contact book."""
        body = await self.async_get(f"/subscription/{subscription_id}/contacts")
        data = body.get("data") if isinstance(body, dict) else None
        contacts = data.get("contacts") if isinstance(data, dict) else None
        return contacts if isinstance(contacts, list) else []

    async def async_cdrs(
        self, subscription_id: str, limit: int = 1, page: int = 1
    ) -> list[dict[str, Any]]:
        """GET /subscription/{id}/cdrs -> call records, newest first."""
        body = await self.async_get(
            f"/subscription/{subscription_id}/cdrs",
            params={
                "page": page,
                "limit": limit,
                "sort_by": "start_time",
                "order": "desc",
            },
        )
        data = body.get("data") if isinstance(body, dict) else None
        records = data.get("records") if isinstance(data, dict) else None
        return records if isinstance(records, list) else []

    async def async_device_status(
        self, mac_address: str, device_type: str = "TINCAN"
    ) -> dict[str, Any]:
        """GET /device/status -> {device_status: online|offline, is_claimed, ...}."""
        body = await self.async_get(
            "/device/status",
            params={"device_type": device_type.upper(), "mac_address": mac_address},
        )
        data = body.get("data") if isinstance(body, dict) else None
        return data if isinstance(data, dict) else {}
