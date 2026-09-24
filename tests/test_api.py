from __future__ import annotations

import unittest

from sniper_bot.api import _default_name, _safe_name


class ApiTests(unittest.TestCase):
    def test_safe_name(self) -> None:
        self.assertEqual(_safe_name("EUR/USD 5m"), "eur_usd_5m")

    def test_default_name_for_known_epic(self) -> None:
        self.assertEqual(_default_name("CS.D.EURUSD.MINI.IP"), "eurusd")


if __name__ == "__main__":
    unittest.main()
