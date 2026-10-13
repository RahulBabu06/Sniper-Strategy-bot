from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import requests

from sniper_bot.config import IGConfig
from sniper_bot.market import Candle


class IGApiError(RuntimeError):
    def __init__(self, status_code: int, error_code: str | None, detail: str) -> None:
        self.status_code = status_code
        self.error_code = error_code
        self.detail = detail
        hint = _hint_for_error(error_code)
        message = f"IG API error {status_code}"
        if error_code:
            message += f" ({error_code})"
        if hint:
            message += f": {hint}"
        else:
            message += f": {detail}"
        super().__init__(message)


class IGClient:
    def __init__(self, config: IGConfig, timeout: int = 20) -> None:
        self.config = config
        self.timeout = timeout
        self.session = requests.Session()
        self.authenticated = False

    def login(self) -> None:
        if not self.config.api_key or not self.config.username or not self.config.password:
            raise ValueError("IG_API_KEY, IG_USERNAME and IG_PASSWORD are required")

        headers = self._headers(version="2")
        response = self.session.post(
            f"{self.config.base_url}/session",
            json={"identifier": self.config.username, "password": self.config.password},
            headers=headers,
            timeout=self.timeout,
        )
        self._raise_for_status(response)
        self.session.headers.update(
            {
                "X-IG-API-KEY": self.config.api_key,
                "CST": response.headers["CST"],
                "X-SECURITY-TOKEN": response.headers["X-SECURITY-TOKEN"],
            }
        )
        if self.config.account_id:
            self.switch_account(self.config.account_id)
        self.authenticated = True

    def switch_account(self, account_id: str) -> None:
        response = self.session.put(
            f"{self.config.base_url}/session",
            json={"accountId": account_id, "defaultAccount": False},
            headers=self._headers(version="1"),
            timeout=self.timeout,
        )
        try:
            self._raise_for_status(response)
        except IGApiError as error:
            if error.error_code == "error.switch.accountId-must-be-different":
                return
            raise
        if "X-SECURITY-TOKEN" in response.headers:
            self.session.headers.update({"X-SECURITY-TOKEN": response.headers["X-SECURITY-TOKEN"]})

    def historical_prices(self, epic: str, resolution: str, points: int) -> list[Candle]:
        self._ensure_login()
        response = self.session.get(
            f"{self.config.base_url}/prices/{epic}",
            params={"resolution": resolution, "max": points, "pageSize": 0},
            headers=self._headers(version="3"),
            timeout=self.timeout,
        )
        self._raise_for_status(response)
        prices = response.json().get("prices", [])
        candles = [self._parse_price(item) for item in prices if self._has_ohlc(item)]
        return candles

    def historical_prices_range(self, epic: str, resolution: str, start: datetime, end: datetime) -> list[Candle]:
        self._ensure_login()
        response = self.session.get(
            f"{self.config.base_url}/prices/{epic}/{resolution}",
            params={"startdate": _ig_time(start), "enddate": _ig_time(end)},
            headers=self._headers(version="1"),
            timeout=self.timeout,
        )
        self._raise_for_status(response)
        prices = response.json().get("prices", [])
        return [self._parse_price(item) for item in prices if self._has_ohlc(item)]

    def open_positions(self) -> list[dict[str, Any]]:
        self._ensure_login()
        response = self.session.get(
            f"{self.config.base_url}/positions",
            headers=self._headers(version="2"),
            timeout=self.timeout,
        )
        self._raise_for_status(response)
        return response.json().get("positions", [])

    def working_orders(self) -> list[dict[str, Any]]:
        self._ensure_login()
        response = self.session.get(
            f"{self.config.base_url}/workingorders",
            headers=self._headers(version="2"),
            timeout=self.timeout,
        )
        self._raise_for_status(response)
        return response.json().get("workingOrders", [])

    def create_market_position(
        self,
        *,
        epic: str,
        direction: str,
        size: float,
        stop_distance: float | None = None,
        limit_distance: float | None = None,
        stop_level: float | None = None,
        limit_level: float | None = None,
        currency_code: str = "GBP",
    ) -> dict[str, Any]:
        self._ensure_login()
        payload = {
            "currencyCode": currency_code,
            "dealReference": f"SNIPER-{uuid.uuid4().hex[:20]}",
            "direction": direction,
            "epic": epic,
            "expiry": "DFB",
            "forceOpen": True,
            "guaranteedStop": False,
            "level": None,
            "limitDistance": round(limit_distance, 5) if limit_distance is not None else None,
            "limitLevel": round(limit_level, 5) if limit_level is not None else None,
            "orderType": "MARKET",
            "size": size,
            "stopDistance": round(stop_distance, 5) if stop_distance is not None else None,
            "stopLevel": round(stop_level, 5) if stop_level is not None else None,
            "timeInForce": "FILL_OR_KILL",
            "trailingStop": False,
        }
        response = self.session.post(
            f"{self.config.base_url}/positions/otc",
            json=payload,
            headers=self._headers(version="2"),
            timeout=self.timeout,
        )
        self._raise_for_status(response)
        return response.json()

    def create_working_order(
        self,
        *,
        epic: str,
        direction: str,
        size: float,
        entry_level: float,
        stop_level: float,
        limit_level: float,
        currency_code: str = "GBP",
    ) -> dict[str, Any]:
        """Place a GTC limit entry with its fixed stop and profit target."""
        self._ensure_login()
        payload = {
            "currencyCode": currency_code,
            "dealReference": f"SNIPER-{uuid.uuid4().hex[:20]}",
            "direction": direction,
            "epic": epic,
            "expiry": "DFB",
            "forceOpen": True,
            "goodTillDate": None,
            "guaranteedStop": False,
            "level": round(entry_level, 5),
            "limitLevel": round(limit_level, 5),
            "type": "LIMIT",
            "size": size,
            "stopLevel": round(stop_level, 5),
            "timeInForce": "GOOD_TILL_CANCELLED",
        }
        response = self.session.post(
            f"{self.config.base_url}/workingorders/otc",
            json=payload,
            headers=self._headers(version="2"),
            timeout=self.timeout,
        )
        self._raise_for_status(response)
        return response.json()

    def deal_confirmation(self, deal_reference: str) -> dict[str, Any]:
        self._ensure_login()
        response = self.session.get(
            f"{self.config.base_url}/confirms/{deal_reference}",
            headers=self._headers(version="1"),
            timeout=self.timeout,
        )
        self._raise_for_status(response)
        return response.json()

    def _ensure_login(self) -> None:
        if not self.authenticated:
            self.login()

    def _headers(self, version: str) -> dict[str, str]:
        return {
            "Accept": "application/json; charset=UTF-8",
            "Content-Type": "application/json; charset=UTF-8",
            "X-IG-API-KEY": self.config.api_key,
            "VERSION": version,
        }

    def _raise_for_status(self, response: requests.Response) -> None:
        if response.ok:
            return
        detail = response.text[:500]
        error_code = None
        try:
            body = response.json()
            if isinstance(body, dict):
                error_code = body.get("errorCode")
        except ValueError:
            pass
        raise IGApiError(response.status_code, error_code, detail)

    def _has_ohlc(self, item: dict[str, Any]) -> bool:
        for key in ("openPrice", "highPrice", "lowPrice", "closePrice"):
            price = item.get(key) or {}
            if price.get("bid") is None or price.get("ask") is None:
                return False
        return True

    def _parse_price(self, item: dict[str, Any]) -> Candle:
        if item.get("snapshotTimeUTC"):
            parsed_timestamp = self._parse_timestamp(item["snapshotTimeUTC"])
        else:
            # IG's snapshotTime is UK local time, not UTC.
            local = self._parse_timestamp(item["snapshotTime"])
            parsed_timestamp = local.replace(tzinfo=UK_TIME).astimezone(timezone.utc)
        return Candle(
            timestamp=parsed_timestamp,
            open_bid=float(item["openPrice"]["bid"]),
            open_ask=float(item["openPrice"]["ask"]),
            high_bid=float(item["highPrice"]["bid"]),
            high_ask=float(item["highPrice"]["ask"]),
            low_bid=float(item["lowPrice"]["bid"]),
            low_ask=float(item["lowPrice"]["ask"]),
            close_bid=float(item["closePrice"]["bid"]),
            close_ask=float(item["closePrice"]["ask"]),
            volume=float(item.get("lastTradedVolume") or 1),
        )

    def _parse_timestamp(self, value: str) -> datetime:
        normalized = value.replace("Z", "+00:00")
        for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%Y/%m/%d %H:%M:%S"):
            try:
                parsed = datetime.strptime(normalized, fmt)
                if parsed.tzinfo is None:
                    return parsed.replace(tzinfo=timezone.utc)
                return parsed
            except ValueError:
                continue
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed


UK_TIME = ZoneInfo("Europe/London")


def _hint_for_error(error_code: str | None) -> str:
    hints = {
        "error.security.api-key-invalid": (
            "IG rejected the API key. Check DEMO vs LIVE, copy the key value not the key name, "
            "and revoke/regenerate the key if it has been exposed."
        ),
        "error.security.api-key-disabled": "This API key is disabled in My IG > Settings > API keys.",
        "error.security.api-key-revoked": "This API key has been revoked. Generate a new active key.",
        "error.security.api-key-restricted": "This API key is restricted for the requested endpoint/account.",
        "error.security.client-token-missing": "Login did not return a CST token. Retry after checking credentials.",
        "error.security.account-token-missing": "Login did not return an account token. Retry after checking credentials.",
        "error.public-api.failure.encryption.required": "IG requires encrypted password login for this account.",
    }
    if not error_code:
        return ""
    return hints.get(error_code, "")


def _ig_time(value: datetime) -> str:
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value.strftime("%Y:%m:%d-%H:%M:%S")
