from __future__ import annotations

import argparse
import csv
import json
import os
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from sniper_bot.backtest import filter_candles, run_backtest, write_summary_json, write_trades_csv
from sniper_bot.config import load_config
from sniper_bot.csv_loader import load_candles_csv, write_candles_csv
from sniper_bot.strategy import SniperStrategy


DEFAULT_RESULTS_DIR = Path("backtest-results")
DEFAULT_DATA_DIR = Path("data")


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve the sniper bot results API.")
    parser.add_argument("--host", default=os.getenv("API_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("API_PORT", "8000")))
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), SniperApiHandler)
    print(f"sniper API listening on http://{args.host}:{args.port}")
    server.serve_forever()


class SniperApiHandler(BaseHTTPRequestHandler):
    server_version = "SniperStrategyApi/0.1"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if parsed.path == "/health":
            self._send_json({"status": "ok"})
            return
        if parsed.path == "/api/results":
            self._send_result(query)
            return
        if parsed.path == "/api/trades":
            self._send_trades(query)
            return
        self._send_json({"error": "not_found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != "/api/backtests/run":
            self._send_json({"error": "not_found"}, HTTPStatus.NOT_FOUND)
            return
        if not self._authorized():
            self._send_json({"error": "unauthorized"}, HTTPStatus.UNAUTHORIZED)
            return
        try:
            payload = self._read_json()
            result = run_ig_backtest(payload)
            self._send_json(result)
        except Exception as error:
            self._send_json({"error": str(error)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def log_message(self, format: str, *args) -> None:
        print(f"{self.address_string()} - {format % args}")

    def _send_result(self, query: dict[str, list[str]]) -> None:
        name = _safe_name(_query_one(query, "name", "eurusd"))
        path = DEFAULT_RESULTS_DIR / f"{name}_summary.json"
        if not path.exists():
            self._send_json({"error": "result_not_found", "path": str(path)}, HTTPStatus.NOT_FOUND)
            return
        self._send_json(json.loads(path.read_text()))

    def _send_trades(self, query: dict[str, list[str]]) -> None:
        name = _safe_name(_query_one(query, "name", "eurusd"))
        path = DEFAULT_RESULTS_DIR / f"{name}_trades.csv"
        if not path.exists():
            self._send_json({"error": "trades_not_found", "path": str(path)}, HTTPStatus.NOT_FOUND)
            return
        with path.open(newline="") as file:
            rows = list(csv.DictReader(file))
        self._send_json({"trades": rows})

    def _send_json(self, payload: object, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Access-Control-Allow-Origin", os.getenv("API_CORS_ORIGIN", "*"))
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict[str, object]:
        length = int(self.headers.get("Content-Length", "0"))
        if length == 0:
            return {}
        body = self.rfile.read(length)
        return json.loads(body.decode("utf-8"))

    def _authorized(self) -> bool:
        token = os.getenv("API_ADMIN_TOKEN", "")
        if not token:
            return True
        return self.headers.get("Authorization") == f"Bearer {token}"


def run_ig_backtest(payload: dict[str, object]) -> dict[str, object]:
    from sniper_bot.ig_client import IGClient

    config = load_config()
    epic = str(payload.get("epic") or config.strategy.market_epics[0])
    resolution = str(payload.get("resolution") or config.strategy.resolution)
    points = int(payload.get("points") or 1000)
    name = _safe_name(str(payload.get("name") or _default_name(epic)))
    size = float(payload.get("size") or config.strategy.default_size)
    from_time = str(payload.get("from_time") or "") or None
    to_time = str(payload.get("to_time") or "") or None

    candles = IGClient(config.ig).historical_prices(epic, resolution, points)
    if not candles:
        raise RuntimeError("IG returned no candles")

    csv_path = DEFAULT_DATA_DIR / f"{name}_{resolution.lower()}.csv"
    trades_path = DEFAULT_RESULTS_DIR / f"{name}_trades.csv"
    summary_path = DEFAULT_RESULTS_DIR / f"{name}_summary.json"
    write_candles_csv(candles, csv_path)

    loaded_candles = load_candles_csv(csv_path)
    result = run_backtest(loaded_candles, SniperStrategy(config.strategy), size, config.strategy.starting_balance, from_time, to_time)
    write_trades_csv(result.trades, trades_path)
    write_summary_json(result, summary_path)
    summary = json.loads(summary_path.read_text())
    summary["data_path"] = str(csv_path)
    summary["trades_path"] = str(trades_path)
    summary["summary_path"] = str(summary_path)
    summary["epic"] = epic
    summary["resolution"] = resolution
    summary["points"] = len(candles)
    summary["backtest_points"] = len(filter_candles(loaded_candles, from_time, to_time))
    summary["from_time"] = from_time or ""
    summary["to_time"] = to_time or ""
    return summary


def _query_one(query: dict[str, list[str]], key: str, default: str) -> str:
    values = query.get(key)
    if not values:
        return default
    return values[0]


def _safe_name(value: str) -> str:
    cleaned = "".join(char.lower() if char.isalnum() else "_" for char in value)
    return cleaned.strip("_") or "result"


def _default_name(epic: str) -> str:
    if "EURUSD" in epic.upper():
        return "eurusd"
    if "GBPUSD" in epic.upper():
        return "gbpusd"
    return epic


if __name__ == "__main__":
    main()
