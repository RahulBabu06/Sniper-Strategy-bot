from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Candle:
    timestamp: datetime
    open_bid: float
    open_ask: float
    high_bid: float
    high_ask: float
    low_bid: float
    low_ask: float
    close_bid: float
    close_ask: float
    volume: float = 1.0

    @property
    def open_mid(self) -> float:
        return (self.open_bid + self.open_ask) / 2

    @property
    def high_mid(self) -> float:
        return (self.high_bid + self.high_ask) / 2

    @property
    def low_mid(self) -> float:
        return (self.low_bid + self.low_ask) / 2

    @property
    def close_mid(self) -> float:
        return (self.close_bid + self.close_ask) / 2

    @property
    def spread(self) -> float:
        return self.close_ask - self.close_bid
