from __future__ import annotations

import argparse
import json

from sniper_bot.config import load_config


def main() -> None:
    parser = argparse.ArgumentParser(description="Check IG API credentials and optionally download one price sample.")
    parser.add_argument("--epic", default=None, help="Optional EPIC to test price download. Defaults to first MARKET_EPICS.")
    parser.add_argument("--resolution", default="MINUTE_5", help="Price resolution for the sample request.")
    args = parser.parse_args()

    from sniper_bot.ig_client import IGApiError, IGClient

    config = load_config()
    epic = args.epic or config.strategy.market_epics[0]
    report: dict[str, object] = {
        "environment": config.ig.environment.upper(),
        "base_url": config.ig.base_url,
        "api_key_present": bool(config.ig.api_key),
        "api_key_length": len(config.ig.api_key),
        "username_present": bool(config.ig.username),
        "password_present": bool(config.ig.password),
        "account_id": config.ig.account_id or "",
        "epic": epic,
        "login": "not_started",
        "prices": "not_started",
    }

    client = IGClient(config.ig)
    try:
        client.login()
        report["login"] = "ok"
        candles = client.historical_prices(epic, args.resolution, 5)
        report["prices"] = "ok"
        report["price_count"] = len(candles)
        if candles:
            report["first_price_time"] = candles[0].timestamp.isoformat()
            report["last_price_time"] = candles[-1].timestamp.isoformat()
    except IGApiError as error:
        report["login"] = "error" if report["login"] == "not_started" else report["login"]
        report["error_status"] = error.status_code
        report["error_code"] = error.error_code or ""
        report["error_message"] = str(error)
    except Exception as error:
        report["error_message"] = str(error)

    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
