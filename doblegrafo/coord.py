"""Coordinación sobre el doble grafo: planificador único + trabajadores sin estado.

El planificador despierta, busca huecos en los grafos y crea intenciones.
Los trabajadores reclaman una intención, la ejecutan y escriben los resultados.
Nadie se manda mensajes: el grafo es el mecanismo de coordinación.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Callable

from .graph import DoubleGraph, GraphError, Node

# Límites del checkpoint del planificador (se rechaza en vez de truncar)
MAX_TODO_ITEMS = 64
MAX_HANDOFF_BYTES = 64 * 1024
MAX_HANDOFF_VERSIONS = 20
COLD_ROUNDS_BEFORE_DIGEST = 3


class HandoffTooLarge(GraphError):
    pass


class Planner:
    def __init__(self, g: DoubleGraph, eid: int, goal_id: int, *, max_new_per_round: int = 10):
        self.g, self.eid, self.goal_id = g, eid, goal_id
        self.max_new = max_new_per_round

    # ------------------------------------------------------------- ronda
    def run_round(self) -> list[int]:
        """Una ronda: sube el contador, crea intenciones para huecos y marca ramas frías."""
        g = self.g
        g.db.execute("UPDATE explorations SET round_no=round_no+1 WHERE id=?", (self.eid,))
        denied = self._denied_symbols()
        created: list[int] = []

        # 1) cobertura: instrumentos operables sin analizar en este ciclo
        for inst in g.uncovered_instruments(self.eid):
            if len(created) >= self.max_new:
                break
            if inst["symbol"] in denied:
                continue
            created.append(g.add_node(
                self.eid, "intent",
                {"capability": "analyze", "symbol": inst["symbol"], "text": f"Analizar {inst['symbol']}"},
                parents=[("spawns", self.goal_id)], anchors=[inst["id"]], priority=10, origin="planner"))

        # 2) hallazgos confirmados sin propuesta ni intención de evaluarlos -> evaluar propuesta
        for f in g.nodes(self.eid, kind="finding", state="confirmed"):
            if len(created) >= self.max_new:
                break
            if self._has_followup(f.id):
                continue
            created.append(g.add_node(
                self.eid, "intent",
                {"capability": "propose", "finding_id": f.id, "text": f"Evaluar operación desde hallazgo {f.id}"},
                parents=[("spawns", f.id)], anchors=g.anchored_instruments(f.id), priority=20,
                origin="planner"))

        # 3) pistas activas del humano -> intención
        for h in g.nodes(self.eid, kind="hint", state="active"):
            if len(created) >= self.max_new:
                break
            created.append(g.add_node(
                self.eid, "intent", {"capability": h.payload.get("capability", "analyze"), **h.payload},
                parents=[("spawns", h.id)], anchors=g.anchored_instruments(h.id), priority=30, origin="hint"))
            g.set_state(h.id, "consumed")

        self._mark_cold()
        return created

    def _denied_symbols(self) -> set[str]:
        out = set()
        for c in self.g.constraints(self.eid):
            if c["kind"] == "deny" and c["text"].startswith("symbol:"):
                out.add(c["text"].split(":", 1)[1].strip().upper())
        return out

    def _has_followup(self, finding_id: int) -> bool:
        r = self.g.db.execute(
            "SELECT 1 FROM edges WHERE (src_id=? AND rel='spawns')"
            " OR (dst_id=? AND rel='derived_from' AND src_id IN (SELECT id FROM nodes WHERE kind='proposal'))"
            " LIMIT 1", (finding_id, finding_id)).fetchone()
        return r is not None

    # ----------------------------------------------------- ramas frías
    def _mark_cold(self) -> None:
        """Una intención terminada sin descendencia viva queda 'fría'; tras N rondas se resume."""
        g = self.g
        rnd = g.exploration(self.eid)["round_no"]
        for it in g.nodes(self.eid, kind="intent"):
            if it.state in ("open", "running", "blocked"):
                g.db.execute("UPDATE nodes SET cold_since_round=NULL WHERE id=?", (it.id,))
                continue
            live = g.db.execute(
                "WITH RECURSIVE d(id) AS (SELECT ? UNION SELECT e.dst_id FROM edges e JOIN d ON e.src_id=d.id"
                "  WHERE e.rel IN ('spawns','yields'))"
                " SELECT 1 FROM nodes n JOIN d ON d.id=n.id WHERE n.kind='intent'"
                "  AND n.state IN ('open','running','blocked') LIMIT 1", (it.id,)).fetchone()
            if live:
                g.db.execute("UPDATE nodes SET cold_since_round=NULL WHERE id=?", (it.id,))
            else:
                g.db.execute("UPDATE nodes SET cold_since_round=COALESCE(cold_since_round, ?) WHERE id=?",
                             (rnd, it.id))

    def cold_nodes(self) -> list[int]:
        rnd = self.g.exploration(self.eid)["round_no"]
        return [r[0] for r in self.g.db.execute(
            "SELECT id FROM nodes n WHERE exploration_id=? AND cold_since_round IS NOT NULL"
            " AND ?-cold_since_round>=? AND NOT EXISTS (SELECT 1 FROM edges e JOIN nodes d ON d.id=e.src_id"
            "  WHERE e.dst_id=n.id AND e.rel='covers' AND d.state='active')",
            (self.eid, rnd, COLD_ROUNDS_BEFORE_DIGEST))]

    def digest(self, summary: str) -> int | None:
        """Crea un nodo resumen que cubre las ramas frías (para no recargar el contexto del LLM)."""
        cold = self.cold_nodes()
        if not cold:
            return None
        did = self.g.add_node(self.eid, "digest", {"summary": summary, "members": cold}, origin="planner")
        for nid in cold:
            self.g.add_edge(did, "covers", nid)
        return did

    # --------------------------------------------- checkpoint (handoff)
    def save_handoff(self, todo: list[dict], notes: str = "") -> int:
        if len(todo) > MAX_TODO_ITEMS:
            raise HandoffTooLarge(f"{len(todo)} pendientes > {MAX_TODO_ITEMS}")
        body = json.dumps({"todo": todo, "notes": notes}, ensure_ascii=False)
        if len(body.encode()) > MAX_HANDOFF_BYTES:
            raise HandoffTooLarge(f"checkpoint de {len(body.encode())} bytes > {MAX_HANDOFF_BYTES}")
        db = self.g.db
        v = (db.execute("SELECT COALESCE(MAX(version),0) FROM planner_handoffs WHERE exploration_id=?",
                        (self.eid,)).fetchone()[0]) + 1
        db.execute("INSERT INTO planner_handoffs(exploration_id,version,body) VALUES (?,?,?)",
                   (self.eid, v, body))
        db.execute("DELETE FROM planner_handoffs WHERE exploration_id=? AND version<=?",
                   (self.eid, v - MAX_HANDOFF_VERSIONS))
        return v

    def load_handoff(self) -> dict | None:
        r = self.g.db.execute(
            "SELECT body FROM planner_handoffs WHERE exploration_id=? ORDER BY version DESC LIMIT 1",
            (self.eid,)).fetchone()
        return json.loads(r[0]) if r else None


# ====================================================================== workers
@dataclass
class Result:
    """Lo que un trabajador escribe al terminar una intención."""
    kind: str                       # fact | finding | proposal
    payload: dict
    derived_from: list[int] = field(default_factory=list)
    proves: list[int] = field(default_factory=list)
    anchors: list[int] | None = None  # None = hereda las anclas de la intención
    available_at: str | None = None


Handler = Callable[[DoubleGraph, Node], list[Result]]


class Worker:
    """Trabajador sin estado: reclama, ejecuta, escribe y suelta."""

    def __init__(self, g: DoubleGraph, eid: int, name: str, handlers: dict[str, Handler]):
        self.g, self.eid, self.name, self.handlers = g, eid, name, handlers

    def step(self) -> int | None:
        intent = None
        for cap in self.handlers:
            intent = self.g.claim_intent(self.eid, self.name, cap)
            if intent:
                break
        if intent is None:
            return None
        handler = self.handlers[intent.payload["capability"]]
        try:
            results = handler(self.g, intent)
        except Exception as exc:  # el error queda en el grafo, no se pierde
            self.g.set_state(intent.id, "blocked", reason=f"{type(exc).__name__}: {exc}")
            return intent.id
        written = []
        for r in results:
            parents = [("yields", intent.id)]
            parents += [("derived_from", x) for x in r.derived_from]
            parents += [("proves", x) for x in r.proves]
            anchors = self.g.anchored_instruments(intent.id) if r.anchors is None else r.anchors
            written.append(self.g.add_node(self.eid, r.kind, r.payload, parents=parents, anchors=anchors,
                                           owner=self.name, origin="worker", available_at=r.available_at))
        self.g.set_state(intent.id, "done" if written else "exhausted")
        return intent.id

    def run_until_empty(self, max_steps: int = 1000) -> int:
        n = 0
        while n < max_steps and self.step() is not None:
            n += 1
        return n
