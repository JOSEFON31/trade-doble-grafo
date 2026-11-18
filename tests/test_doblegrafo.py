import os
import tempfile
import threading
import unittest

from doblegrafo import DoubleGraph, GraphError, HandoffTooLarge, Planner, Result, Worker, instrument_key


def mercado(g):
    us = g.upsert_instrument("market", "US")
    tech = g.upsert_instrument("sector", "TECH")
    spy = g.upsert_instrument("etf", "spy", "arca", tradable=True)
    qqq = g.upsert_instrument("etf", "QQQ", "NASDAQ", tradable=True)
    g.link_instruments(tech, "belongs_to", us)
    g.link_instruments(qqq, "belongs_to", tech)
    g.link_instruments(qqq, "correlated_with", spy, 0.9)
    return spy, qqq


def analizar(g, intent):
    sym = intent.payload["symbol"]
    f = Result("fact", {"symbol": sym, "close": 100.0}, available_at="2026-11-18T21:00:00Z")
    return [f]


class MercadoTest(unittest.TestCase):
    def test_clave_estable_deduplica(self):
        g = DoubleGraph()
        a = g.upsert_instrument("etf", "spy", "arca", tradable=True)
        b = g.upsert_instrument("ETF", " SPY ", "ARCA")
        self.assertEqual(a, b)
        self.assertEqual(instrument_key("etf", "spy", "arca"), "etf:ARCA:SPY")


class ExploracionTest(unittest.TestCase):
    def setUp(self):
        self.g = DoubleGraph()
        self.spy, self.qqq = mercado(self.g)
        self.eid, self.goal = self.g.create_exploration("Cartera del 2026-11-18")

    def test_aristas_invalidas_se_rechazan(self):
        g = self.g
        fact = g.add_node(self.eid, "fact", {"x": 1})
        with self.assertRaises(GraphError):
            g.add_edge(fact, "spawns", self.goal)  # un hecho no genera objetivos
        with self.assertRaises(GraphError):
            g.add_edge(fact, "derived_from", fact)

    def test_no_ciclos(self):
        g = self.g
        f1 = g.add_node(self.eid, "finding", {"a": 1})
        f2 = g.add_node(self.eid, "finding", {"b": 2}, parents=[("derived_from", f1)])
        with self.assertRaises(GraphError):
            g.add_edge(f1, "derived_from", f2)

    def test_point_in_time(self):
        g = self.g
        futuro = g.add_node(self.eid, "fact", {"close": 1}, available_at="2026-11-19T00:00:00Z")
        with self.assertRaises(GraphError):
            g.add_node(self.eid, "finding", {"s": 1}, parents=[("derived_from", futuro)],
                       available_at="2026-11-18T00:00:00Z")

    def test_inmutables_e_integridad(self):
        g = self.g
        fact = g.add_node(self.eid, "fact", {"close": 1})
        with self.assertRaises(GraphError):
            g.update_payload(fact, {"close": 2})
        self.assertEqual(g.verify_integrity(), [])
        g.db.execute("UPDATE nodes SET payload='{\"close\": 999}' WHERE id=?", (fact,))
        self.assertEqual(g.verify_integrity(), [fact])

    def test_transiciones(self):
        g = self.g
        it = g.add_node(self.eid, "intent", {"capability": "analyze"}, parents=[("spawns", self.goal)])
        with self.assertRaises(GraphError):
            g.set_state(it, "done")  # open -> done no es válido, debe pasar por running
        g.set_state(it, "running")
        g.set_state(it, "done")


class CoordinacionTest(unittest.TestCase):
    def setUp(self):
        self.g = DoubleGraph()
        self.spy, self.qqq = mercado(self.g)
        self.eid, self.goal = self.g.create_exploration("Cartera del 2026-11-18")
        self.p = Planner(self.g, self.eid, self.goal)

    def test_planificador_cubre_huecos_y_respeta_deny(self):
        self.g.add_constraint("deny", "symbol:QQQ")
        created = self.p.run_round()
        self.assertEqual(len(created), 1)
        self.assertEqual(self.g.anchored_instruments(created[0]), [self.spy])
        self.assertEqual(self.p.run_round(), [])  # ya cubierto: no duplica

    def test_flujo_completo_y_credito(self):
        g = self.g
        self.p.run_round()
        w = Worker(g, self.eid, "analista-tecnico", {"analyze": analizar})
        self.assertEqual(w.run_until_empty(), 2)
        facts = g.nodes(self.eid, kind="fact")
        self.assertEqual(len(facts), 2)
        self.assertEqual(len(g.nodes_for_instrument(self.spy, "fact")), 1)

        # un estratega convierte el hecho de SPY en hallazgo
        spy_fact = g.nodes_for_instrument(self.spy, "fact")[0]
        find = g.add_node(self.eid, "finding", {"signal": "momentum", "z": 1.8},
                          parents=[("derived_from", spy_fact.id), ("proves", self.goal)],
                          anchors=[self.spy], owner="estratega-momentum",
                          available_at="2026-11-18T21:05:00Z")
        # el planificador detecta el hallazgo y crea la intención de proponer
        nuevos = self.p.run_round()
        self.assertEqual(len(nuevos), 1)
        self.assertEqual(g.node(nuevos[0]).payload["capability"], "propose")

        def proponer(g_, intent):
            return [Result("proposal", {"side": "buy", "qty": 10}, derived_from=[intent.payload["finding_id"]],
                           available_at="2026-11-18T21:10:00Z")]
        Worker(g, self.eid, "estratega-momentum", {"propose": proponer}).run_until_empty()
        prop = g.nodes(self.eid, kind="proposal")[0]
        g.set_state(prop.id, "approved")   # lo haría el gestor de riesgo
        g.set_state(prop.id, "executed")   # lo haría el ejecutor
        g.record_outcome(prop.id, 50.0, available_at="2026-11-20T21:00:00Z")

        self.assertEqual(g.node_credit(find), 50.0)
        self.assertEqual(g.node_credit(spy_fact.id), 50.0)
        por_agente = g.credit_by_owner()
        self.assertGreater(por_agente["analista-tecnico"], 0)
        self.assertGreater(por_agente["estratega-momentum"], 0)
        linaje = {n.kind for n in g.lineage(prop.id)}
        self.assertTrue({"proposal", "finding", "fact", "intent", "goal"} <= linaje)

    def test_error_del_trabajador_queda_bloqueado(self):
        self.p.run_round()

        def falla(g, intent):
            raise RuntimeError("sin datos")
        Worker(self.g, self.eid, "w", {"analyze": falla}).step()
        bloqueadas = self.g.nodes(self.eid, kind="intent", state="blocked")
        self.assertEqual(len(bloqueadas), 1)

    def test_ramas_frias_y_digest(self):
        self.p.run_round()
        Worker(self.g, self.eid, "w", {"analyze": analizar}).run_until_empty()
        for _ in range(4):
            self.p.run_round()
        self.assertEqual(len(self.p.cold_nodes()), 2)
        d = self.p.digest("SPY y QQQ analizados sin señal")
        self.assertIsNotNone(d)
        self.assertEqual(self.p.cold_nodes(), [])

    def test_handoff_limites_y_versiones(self):
        for i in range(25):
            self.p.save_handoff([{"t": i}])
        self.assertEqual(self.p.load_handoff()["todo"], [{"t": 24}])
        n = self.g.db.execute("SELECT COUNT(*) FROM planner_handoffs").fetchone()[0]
        self.assertEqual(n, 20)
        with self.assertRaises(HandoffTooLarge):
            self.p.save_handoff([{}] * 65)
        with self.assertRaises(HandoffTooLarge):
            self.p.save_handoff([{"x": "a" * 70000}])


class ConcurrenciaTest(unittest.TestCase):
    def test_dos_trabajadores_no_reclaman_lo_mismo(self):
        path = os.path.join(tempfile.mkdtemp(), "g.db")
        g = DoubleGraph(path)
        for i in range(40):
            g.upsert_instrument("stock", f"S{i}", "X", tradable=True)
        eid, goal = g.create_exploration("prueba")
        Planner(g, eid, goal, max_new_per_round=40).run_round()
        claimed = {"a": [], "b": []}

        def run(name):
            gg = DoubleGraph(path)
            while (n := gg.claim_intent(eid, name)) is not None:
                claimed[name].append(n.id)
            gg.close()
        ts = [threading.Thread(target=run, args=(n,)) for n in claimed]
        [t.start() for t in ts]
        [t.join() for t in ts]
        todos = claimed["a"] + claimed["b"]
        self.assertEqual(len(todos), 40)
        self.assertEqual(len(set(todos)), 40)


if __name__ == "__main__":
    unittest.main()
