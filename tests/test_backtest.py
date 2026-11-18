import os
import tempfile
import unittest
from pathlib import Path

from doblegrafo.backtest import (expected_max_sharpe, metrics, probabilistic_sharpe, run_backtest,
                                 synthetic_prices, walk_forward)
from doblegrafo.strategies import Signal, Strategy, grid, make

PRICES = {"A": synthetic_prices("A", days=1300, seed=11, drift=0.10),
          "B": synthetic_prices("B", days=1300, seed=12, drift=0.02)}


class Espia(Strategy):
    """Registra qué barras ve y compra cada lunes para generar operaciones."""
    name = "espia"
    PARAM_GRID = {}

    def __init__(self):
        super().__init__()
        self.seen = []

    def decide(self, symbol, bars, held):
        self.seen.append((symbol, bars[-1]["day"], len(bars)))
        c = bars[-1]["close"]
        if not held:
            return Signal(symbol, "buy", round(c * 0.95, 4), "prueba")
        return Signal(symbol, "sell", 0.0, "prueba") if len(bars) % 5 == 0 else None


class MetricasTest(unittest.TestCase):
    def test_curva_conocida(self):
        curve = [(f"d{i}", 100 * 2 ** (i / 252)) for i in range(253)]
        m = metrics(curve)
        self.assertAlmostEqual(m.cagr, 1.0, places=2)
        self.assertAlmostEqual(m.max_drawdown, 0.0)
        m2 = metrics([("a", 100), ("b", 120), ("c", 90), ("d", 130)])
        self.assertAlmostEqual(m2.max_drawdown, 0.25)

    def test_deflactado_es_mas_exigente_con_mas_pruebas(self):
        p1 = probabilistic_sharpe(0.05, 0.0, 1000, 0.0, 3.0)
        p2 = probabilistic_sharpe(0.05, expected_max_sharpe([0.0, 0.02, -0.01, 0.04, 0.03] * 20), 1000, 0.0, 3.0)
        self.assertGreater(p1, p2)


class BacktestTest(unittest.TestCase):
    def test_sin_datos_del_futuro_y_llenado_al_dia_siguiente(self):
        s = Espia()
        r = run_backtest(PRICES, s, "2010-03-01", "2010-06-30")
        for sym, last_day, n in s.seen:
            idx = [b["day"] for b in PRICES[sym]].index(last_day)
            self.assertEqual(n, idx + 1)       # ve exactamente hasta el día de decisión
        self.assertGreater(r.metrics.trades, 0)
        # ninguna compra se llena el mismo día en que se decidió
        self.assertGreater(r.metrics.trades, 0)

    def test_determinista(self):
        a = run_backtest(PRICES, make("momentum", lookback=63), "2010-01-04", "2013-12-31")
        b = run_backtest(PRICES, make("momentum", lookback=63), "2010-01-04", "2013-12-31")
        self.assertEqual(a.curve, b.curve)

    def test_respeta_el_gestor_de_riesgo(self):
        r = run_backtest(PRICES, make("cruce_medias", fast=20, slow=100), "2010-01-04", "2014-12-31")
        self.assertLessEqual(r.avg_exposure, 0.2 + 1e-6)  # 10 % por instrumento × 2 instrumentos

    def test_grid(self):
        self.assertEqual(len(grid("momentum")), 9)
        with self.assertRaises(ValueError):
            make("momentum", inventado=1)


class WalkForwardTest(unittest.TestCase):
    def test_ventanas_y_holdout(self):
        r = walk_forward(PRICES, "cruce_medias", train_years=2, test_years=1, holdout_months=6,
                         param_grid=[{"fast": 20, "slow": 100}, {"fast": 50, "slow": 100}])
        self.assertGreaterEqual(len(r.folds), 2)
        for f in r.folds:
            self.assertLess(f["entrena"][1], f["prueba"][0])     # se prueba después de entrenar
        self.assertTrue(all(d < r.holdout_from for d, _ in r.oos_curve))
        days = [d for d, _ in r.oos_curve]
        self.assertEqual(days, sorted(days))
        self.assertEqual(len(days), len(set(days)))              # sin solapes entre ventanas
        self.assertEqual(r.trials, 2 * len(r.folds))
        r2 = walk_forward(PRICES, "cruce_medias", train_years=2, test_years=1, holdout_months=6,
                          param_grid=[{"fast": 20, "slow": 100}, {"fast": 50, "slow": 100}], extra_trials=500)
        self.assertLessEqual(r2.deflated_sharpe_prob, r.deflated_sharpe_prob + 1e-9)


class SimuladorCLITest(unittest.TestCase):
    def test_cli_sintetico(self):
        from doblegrafo.simulador import main
        tmp = Path(tempfile.mkdtemp())
        os.environ["TRADER_DB"] = str(tmp / "t.db")
        os.environ["TRADER_LIMITS"] = str(tmp / "l.json")
        try:
            self.assertEqual(main(["--sintetico", "--estrategia", "reversion", "--json", str(tmp / "r.json")]), 0)
            self.assertTrue((tmp / "r.json").exists())
            self.assertEqual(main(["--estrategia", "momentum"]), 2)  # sin precios guardados
        finally:
            os.environ.pop("TRADER_DB", None)
            os.environ.pop("TRADER_LIMITS", None)


if __name__ == "__main__":
    unittest.main()
