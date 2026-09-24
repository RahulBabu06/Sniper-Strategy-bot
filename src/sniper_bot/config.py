from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _csv_env(name: str, default: list[str]) -> list[str]:
    value = os.getenv(name)
    if not value:
        return default
    return [item.strip() for item in value.split(",") if item.strip()]


def load_env_file(path: str | Path = ".env") -> None:
    env_path = Path(path)
    if not env_path.exists():
        return
    for raw_line in env_path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


@dataclass(frozen=True)
class IGConfig:
    environment: str
    api_key: str
    username: str
    password: str
    account_id: str | None

    @property
    def base_url(self) -> str:
        env = self.environment.upper()
        if env == "LIVE":
            return "https://api.ig.com/gateway/deal"
        if env == "DEMO":
            return "https://demo-api.ig.com/gateway/deal"
        raise ValueError("IG_ENV must be DEMO or LIVE")


@dataclass(frozen=True)
class StrategyConfig:
    market_epics: list[str]
    resolution: str
    price_points: int
    use_spread_filter: bool
    max_spread_points: float
    fast_ema: int
    slow_ema: int
    rsi_period: int
    atr_period: int
    adx_period: int
    macd_fast: int
    macd_slow: int
    macd_signal: int
    min_bull_score_long: float
    min_bear_score_short: float
    require_short_bear_dominance: bool
    min_risk_reward: float
    stop_atr_multiple: float
    target_multiples: list[float]
    take_profit_allocations: list[float]
    break_even_after_tp1: bool
    broker_target_index: int
    default_size: float
    spread_bet_price_decimals: int
    spread_bet_point_size: float
    starting_balance: float
    # The public Pine indicator uses the scores for its dashboard only.  Keep
    # score-gated entries as an explicit, opt-in Python extension.
    apply_score_entry_filters: bool = False
    fixed_bracket_points: float = 10.0


@dataclass(frozen=True)
class RuntimeConfig:
    dry_run: bool
    poll_seconds: int


@dataclass(frozen=True)
class AppConfig:
    ig: IGConfig
    strategy: StrategyConfig
    runtime: RuntimeConfig


def load_config() -> AppConfig:
    load_env_file()
    return AppConfig(
        ig=IGConfig(
            environment=os.getenv("IG_ENV", "DEMO"),
            api_key=os.getenv("IG_API_KEY", ""),
            username=os.getenv("IG_USERNAME", ""),
            password=os.getenv("IG_PASSWORD", ""),
            account_id=os.getenv("IG_ACCOUNT_ID") or None,
        ),
        strategy=StrategyConfig(
            market_epics=_csv_env("MARKET_EPICS", ["CS.D.EURUSD.MINI.IP"]),
            resolution=os.getenv("RESOLUTION", "MINUTE_5"),
            price_points=int(os.getenv("PRICE_POINTS", "120")),
            use_spread_filter=_bool_env("USE_SPREAD_FILTER", False),
            max_spread_points=float(os.getenv("MAX_SPREAD_POINTS", "2.5")),
            fast_ema=int(os.getenv("FAST_EMA", "9")),
            slow_ema=int(os.getenv("SLOW_EMA", "21")),
            rsi_period=int(os.getenv("RSI_PERIOD", "14")),
            atr_period=int(os.getenv("ATR_PERIOD", "14")),
            adx_period=int(os.getenv("ADX_PERIOD", "14")),
            macd_fast=int(os.getenv("MACD_FAST", "12")),
            macd_slow=int(os.getenv("MACD_SLOW", "26")),
            macd_signal=int(os.getenv("MACD_SIGNAL", "9")),
            min_bull_score_long=float(os.getenv("MIN_BULL_SCORE_LONG", "60")),
            min_bear_score_short=float(os.getenv("MIN_BEAR_SCORE_SHORT", "0")),
            require_short_bear_dominance=_bool_env("REQUIRE_SHORT_BEAR_DOMINANCE", True),
            min_risk_reward=float(os.getenv("MIN_RISK_REWARD", "1.0")),
            stop_atr_multiple=float(os.getenv("STOP_ATR_MULTIPLE", "1.5")),
            target_multiples=[
                float(item)
                for item in _csv_env("TARGET_MULTIPLES", ["1", "2", "3", "4", "5"])
            ],
            take_profit_allocations=[
                float(item)
                for item in _csv_env("TAKE_PROFIT_ALLOCATIONS", ["1", "0", "0"])
            ],
            break_even_after_tp1=_bool_env("BREAK_EVEN_AFTER_TP1", False),
            broker_target_index=int(os.getenv("BROKER_TARGET_INDEX", "2")),
            default_size=float(os.getenv("DEFAULT_SIZE", "1")),
            spread_bet_price_decimals=int(os.getenv("SPREAD_BET_PRICE_DECIMALS", "1")),
            spread_bet_point_size=float(os.getenv("SPREAD_BET_POINT_SIZE", "1.0")),
            starting_balance=float(os.getenv("BACKTEST_STARTING_BALANCE", "10000")),
            apply_score_entry_filters=_bool_env("APPLY_SCORE_ENTRY_FILTERS", False),
            fixed_bracket_points=float(os.getenv("FIXED_BRACKET_POINTS", "10")),
        ),
        runtime=RuntimeConfig(
            dry_run=_bool_env("DRY_RUN", True),
            poll_seconds=int(os.getenv("POLL_SECONDS", "60")),
        ),
    )
