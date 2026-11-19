import random
import unittest
from datetime import date, datetime, timedelta, timezone

from doblegrafo.carry import Costs, Market, simulate, stats, walk_forward
from doblegrafo.senales import SignalError, SignalRegistry


def mercado(days=900, rate=lambda i: 0.0001, seed=1, vol=0.03, spike=None):
    rnd = random.Random(seed)
    d0 = date(2021, 1, 1)
    spot, perp, fund, dias = {}, {}, [], []
    px = 100.0
    for i in range(days):
        d = (d0 + timedelta(days=i)).isoformat()
        o = px
        px *= (1 + rnd.gauss(0, vol))
        hi = max(o, px) * 1.01
        if spike == i:
            hi = o * 1.9
        bar = {"open": o, "high": hi, "low": min(o, px) * 0.99, "close": px}
        spot[d], perp[d] = dict(bar), dict(bar)
        dias.append(d)
        t0 = datetime(d0.year, d0.month, d0.day, tzinfo=timezone.utc) + timedelta(days=i)
        for h in (8, 16, 24):
            fund.append((t0 + timedelta(hours=h), rate(i)))
    return Market("SINT", dias, spot, perp, fund)


class Registro(unittest.TestCase):
    def test_point_in_time_e_inmutable(self):
        r = SignalRegistry()
        r.register("funding:BTC", "binance", "carry")
        r.record("funding:BTC", "2024-01-01T08:00", 0.0001, "2024-01-01T08:00")
        r.record("funding:BTC", "2024-01-01T16:00", 0.0003, "2024-01-01T16:05")
        self.assertEqual(r.as_of("funding:BTC", "2024-01-01T16:01"), 0.0001)
        self.assertEqual(r.as_of("funding:BTC", "2024-01-01T17:00"), 0.0003)
        self.assertIsNone(r.as_of("funding:BTC", "2023-12-31"))
        r.record("funding:BTC", "2024-01-01T08:00", 0.0001)  # repetir igual no falla
        with self.assertRaises(SignalError):
            r.record("funding:BTC", "2024-01-01T08:00", 0.0009)
        with self.assertRaises(Exception):
            r.db.execute("UPDATE signal_values SET value=1")

    def test_credito_apaga_y_no_cuenta_doble(self):
        r = SignalRegistry(min_windows=3)
        r.register("mala", "x", "direccion")
        for k in range(3):
            s = r.evaluate("mala", f"2024-0{k + 1}-01", f"2024-0{k + 1}-28", sharpe=-0.5, baseline_sharpe=0.2,
                           total_return=-0.01, max_drawdown=0.05)
        self.assertEqual(s.state, "apagada")
        s2 = r.evaluate("mala", "2024-03-01", "2024-03-28", sharpe=9, baseline_sharpe=0, total_return=1,
                        max_drawdown=0)
        self.assertEqual((s2.windows, s2.credit), (s.windows, s.credit))


class Carry(unittest.TestCase):
    def test_delta_neutral_cobra_funding(self):
        m = mercado(rate=lambda i: 0.0001)  # ~11 % anual
        res = simulate(m, {"siempre": True}, m.days[0], m.days[-1])
        st = stats(res.curve)
        self.assertEqual(res.liquidations, 0)
        self.assertGreater(st["retorno_total"], 0.05)  # gana aunque el precio sea aleatorio
        self.assertLess(st["caida_maxima"], 0.05)

    def test_umbral_evita_funding_negativo(self):
        m = mercado(rate=lambda i: 0.0002 if (i // 60) % 2 == 0 else -0.0002)
        dentro = stats(simulate(m, {"siempre": True}, m.days[0], m.days[-1]).curve)
        senal = stats(simulate(m, {"ventana": 3, "entrada": 0.05}, m.days[0], m.days[-1]).curve)
        self.assertGreater(senal["retorno_total"], dentro["retorno_total"])

    def test_sin_datos_del_futuro(self):
        k = 400
        a = mercado(rate=lambda i: 0.0001)
        b = mercado(rate=lambda i: 0.0001 if i < k else 0.01)
        ca = simulate(a, {"ventana": 1, "entrada": 0.05}, a.days[0], a.days[-1]).curve
        cb = simulate(b, {"ventana": 1, "entrada": 0.05}, b.days[0], b.days[-1]).curve
        self.assertEqual(ca[:k], cb[:k])

    def test_liquidacion_se_cuenta(self):
        m = mercado(spike=300, vol=0.0)
        res = simulate(m, {"siempre": True}, m.days[0], m.days[-1], costs=Costs(leverage=2))
        self.assertEqual(res.liquidations, 1)

    def test_walk_forward_registra(self):
        m = mercado(days=1200, rate=lambda i: 0.0002 if (i // 90) % 2 == 0 else -0.0001)
        reg = SignalRegistry(min_windows=2)
        wf = walk_forward(m, train_months=12, test_months=6, oos_from="2022-01-01", registry=reg)
        self.assertGreaterEqual(len(wf.windows), 3)
        s = reg.signal("carry_funding:SINT")
        self.assertEqual(s.windows, len(wf.windows))
        self.assertIn(s.state, ("activa", "apagada"))
        self.assertTrue(all(w["prueba"] >= "2022-01-01" for w in wf.windows))


if __name__ == "__main__":
    unittest.main()
