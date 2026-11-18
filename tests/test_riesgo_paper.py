import json
import os
import tempfile
import unittest
from pathlib import Path

from doblegrafo import DoubleGraph
from doblegrafo.paper import CostModel, PaperBroker, parse_daily_csv
from doblegrafo.risk import AccountState, ProposalView, RiskLimits, evaluate

L = RiskLimits()


def acct(equity=10_000.0, peak=None, day_start=None, positions=None, qtys=None, kill=False):
    return AccountState(equity, peak or equity, day_start or equity, positions or {}, qtys or {}, kill)


def buy(qty=100, stop=95.0, sym="SPY", typ="etf", tradable=True):
    return ProposalView(sym, "buy", qty, stop, typ, tradable)


class RiesgoTest(unittest.TestCase):
    def test_tamano_por_riesgo_y_exposicion(self):
        d = evaluate(buy(), acct(), 100.0, 0, L)
        self.assertEqual(d.verdict, "approved")
        self.assertEqual(d.qty, 10)  # 0,5 % de 10 000 / 5 por acción = 10; 10 % del capital = 10

    def test_mitad_de_riesgo_con_caida(self):
        d = evaluate(buy(), acct(equity=9_000, peak=10_000), 100.0, 0, L)
        self.assertEqual(d.qty, 4)  # 22,5 / 5 = 4,5 -> 4

    def test_vetos(self):
        self.assertEqual(evaluate(buy(stop=101), acct(), 100.0, 0, L).verdict, "vetoed")
        self.assertEqual(evaluate(buy(), acct(), None, None, L).verdict, "vetoed")
        self.assertEqual(evaluate(buy(), acct(), 100.0, 9, L).verdict, "vetoed")       # precio viejo
        self.assertEqual(evaluate(buy(typ="crypto"), acct(), 100.0, 0, L).verdict, "vetoed")
        self.assertEqual(evaluate(buy(tradable=False), acct(), 100.0, 0, L).verdict, "vetoed")
        self.assertEqual(evaluate(buy(), acct(day_start=10_300), 100.0, 0, L).verdict, "vetoed")  # -2,9 % hoy
        short = ProposalView("SPY", "sell", 5, 105, "etf", True)
        self.assertEqual(evaluate(short, acct(), 100.0, 0, L).verdict, "vetoed")
        self.assertEqual(evaluate(buy(), acct(positions={"SPY": 1_000}), 100.0, 0, L).verdict, "vetoed")

    def test_caida_maxima_activa_kill_switch(self):
        d = evaluate(buy(), acct(equity=7_900, peak=10_000), 100.0, 0, L)
        self.assertEqual(d.verdict, "vetoed")
        self.assertTrue(d.kill)
        self.assertEqual(evaluate(buy(), acct(kill=True), 100.0, 0, L).verdict, "vetoed")

    def test_ventas_permitidas_con_limites_activos(self):
        sell = ProposalView("SPY", "sell", 5, 1, "etf", True)
        held = dict(qtys={"SPY": 10}, positions={"SPY": 1000})
        self.assertEqual(evaluate(sell, acct(day_start=10_300, **held), 100.0, 0, L).verdict, "approved")
        self.assertEqual(evaluate(sell, acct(kill=True, **held), 100.0, 0, L).verdict, "approved")
        d = evaluate(sell, acct(equity=7_900, peak=10_000, **held), 100.0, 0, L)
        self.assertEqual(d.verdict, "approved")
        self.assertTrue(d.kill)

    def test_venta_recortada_a_posicion(self):
        sell = ProposalView("SPY", "sell", 50, 1, "etf", True)
        d = evaluate(sell, acct(qtys={"SPY": 10}, positions={"SPY": 1000}), 100.0, 0, L)
        self.assertEqual((d.verdict, d.qty), ("approved", 10))

    def test_archivo_de_limites(self):
        p = Path(tempfile.mkdtemp()) / "limites.json"
        self.assertEqual(RiskLimits.load(p), RiskLimits())
        self.assertTrue(p.exists())
        p.write_text(json.dumps({"risk_per_trade": 0.01}))
        self.assertEqual(RiskLimits.load(p).risk_per_trade, 0.01)
        p.write_text(json.dumps({"apalancamiento_infinito": True}))
        with self.assertRaises(ValueError):
            RiskLimits.load(p)


BARS = {
    "2026-11-16": dict(open=99, high=100.5, low=98.5, close=100),
    "2026-11-17": dict(open=101, high=103, low=100.5, close=102),
    "2026-11-18": dict(open=101, high=101.5, low=94, close=95.5),
}


class PaperTest(unittest.TestCase):
    def setUp(self):
        self.g = DoubleGraph()
        self.spy = self.g.upsert_instrument("etf", "SPY", "ARCA", tradable=True)
        self.b = PaperBroker(self.g, RiskLimits(), CostModel(slippage_bps=10, commission_per_share=0.01,
                                                              commission_min=1.0))
        self.b.open_account(10_000)
        self.b.store_prices("SPY", [{"day": d, **v} for d, v in BARS.items()], "test")
        self.eid, self.goal = self.g.create_exploration("prueba")

    def propose(self, side="buy", qty=100, stop=95.0, at="2026-11-16T21:00:00Z"):
        g = self.g
        it = g.add_node(self.eid, "intent", {"capability": "analyze"}, parents=[("spawns", self.goal)],
                        anchors=[self.spy])
        fact = g.add_node(self.eid, "fact", {"fuente": "test"}, parents=[("yields", it)], anchors=[self.spy],
                          owner="analista", available_at=at)
        find = g.add_node(self.eid, "finding", {"s": 1}, parents=[("derived_from", fact)], anchors=[self.spy],
                          owner="analista", available_at=at)
        it2 = g.add_node(self.eid, "intent", {"capability": "propose"}, parents=[("spawns", find)])
        return g.add_node(self.eid, "proposal", {"symbol": "SPY", "side": side, "qty": qty, "stop": stop},
                          parents=[("yields", it2), ("derived_from", find)], anchors=[self.spy],
                          owner="estratega", available_at=at), find

    def test_ciclo_completo_con_stop_y_credito(self):
        pid, find = self.propose()
        r1 = self.b.run_day("2026-11-16")
        self.assertEqual(r1["decisions"][0]["verdict"], "approved")
        self.assertEqual(r1["decisions"][0]["qty"], 10)
        self.assertEqual(r1["fills"], [])                       # nunca se llena el mismo día
        self.assertEqual(self.g.node(pid).state, "approved")

        r2 = self.b.run_day("2026-11-17")
        self.assertEqual(r2["fills"][0]["qty"], 10)
        self.assertAlmostEqual(r2["fills"][0]["price"], 101 * 1.001, places=4)
        self.assertEqual(self.g.node(pid).state, "executed")

        r3 = self.b.run_day("2026-11-18")
        self.assertEqual(len(r3["stops"]), 1)
        exit_px = 95 * 0.999
        expected_pnl = (exit_px - 101 * 1.001) * 10 - 1.0 - 1.0
        out = self.g.nodes(self.eid, kind="outcome")[0]
        self.assertAlmostEqual(out.payload["pnl_net"], expected_pnl, places=4)
        self.assertAlmostEqual(self.g.node_credit(find), expected_pnl, places=4)
        self.assertAlmostEqual(r3["equity"], 10_000 + expected_pnl, places=2)
        self.assertEqual(self.b.db.execute("SELECT COUNT(*) FROM lots").fetchone()[0], 0)
        self.assertLess(self.g.credit_by_owner()["analista"], 0)

    def test_stop_absurdo_se_veta(self):
        self.propose(stop=10.0)  # stop a 90 % del precio: casi seguro un error
        r = self.b.run_day("2026-11-16")
        self.assertEqual(r["decisions"][0]["verdict"], "vetoed")
        self.assertEqual(self.g.node(r["decisions"][0]["proposal"]).state, "vetoed")

    def test_orden_cancelada_por_apertura_anomala(self):
        self.propose()
        self.b.run_day("2026-11-16")
        self.b.db.execute("UPDATE prices SET open=130, high=131 WHERE day='2026-11-17'")
        r = self.b.run_day("2026-11-17")
        self.assertEqual(r["fills"], [])
        self.assertTrue(any("anómalo" in s for s in r["skipped"]))

    def test_compra_pendiente_reserva_exposicion(self):
        self.propose()
        self.b.run_day("2026-11-16")                     # aprobada: 10 acciones = 10 % del capital
        self.b.db.execute("DELETE FROM prices WHERE symbol='SPY' AND day='2026-11-17'")
        self.b.store_prices("QQQ", [{"day": "2026-11-17", "open": 1, "high": 1, "low": 1, "close": 1}], "t")
        self.propose(at="2026-11-16T22:00:00Z")
        r = self.b.run_day("2026-11-17")                 # SPY sin barra: la orden sigue pendiente
        self.assertEqual(r["decisions"][0]["verdict"], "vetoed")

    def test_no_se_repite_un_dia_y_no_hay_cortos(self):
        self.propose(side="sell", qty=5, stop=105)
        r = self.b.run_day("2026-11-16")
        self.assertEqual(r["decisions"][0]["verdict"], "vetoed")
        with self.assertRaises(Exception):
            self.b.run_day("2026-11-16")

    def test_kill_switch_por_caida(self):
        self.b.db.execute("UPDATE account SET peak_equity=20000 WHERE id=1")
        self.propose()
        r = self.b.run_day("2026-11-16")
        self.assertEqual(r["decisions"][0]["verdict"], "vetoed")
        self.assertTrue(r["kill_switch"])

    def test_csv(self):
        rows = parse_daily_csv("Date,Open,High,Low,Close,Volume\n2026-11-16,1,2,0.5,1.5,100\nbasura\n")
        self.assertEqual(rows, [{"day": "2026-11-16", "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5,
                                 "volume": 100.0}])


class EjecutorCLITest(unittest.TestCase):
    def test_cli_con_csv(self):
        from doblegrafo.ejecutor import main
        tmp = Path(tempfile.mkdtemp())
        os.environ["TRADER_DB"] = str(tmp / "t.db")
        os.environ["TRADER_LIMITS"] = str(tmp / "limites.json")
        try:
            g = DoubleGraph(tmp / "t.db")
            g.upsert_instrument("etf", "SPY", "ARCA", tradable=True)
            g.close()
            csvp = tmp / "spy.csv"
            csvp.write_text("Date,Open,High,Low,Close,Volume\n" + "".join(
                f"{d},{v['open']},{v['high']},{v['low']},{v['close']},1000\n" for d, v in BARS.items()))
            self.assertEqual(main(["--capital", "5000", "--csv", f"SPY={csvp}", "--sin-descarga",
                                   "--desde", "2026-11-16"]), 0)
            self.assertEqual(main(["--sin-descarga"]), 0)  # sin días nuevos
        finally:
            os.environ.pop("TRADER_DB", None)
            os.environ.pop("TRADER_LIMITS", None)


if __name__ == "__main__":
    unittest.main()
