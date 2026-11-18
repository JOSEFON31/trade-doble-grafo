"""Doble grafo: grafo de mercado + grafo de exploración unidos por anclas.

Implementación propia (Python + SQLite, sin dependencias) del patrón de
coordinación descrito públicamente para ARTEX, adaptado a trading legal.

Reglas clave:
- Los agentes no se mandan mensajes: escriben y leen los grafos.
- Las identidades del grafo de mercado las calcula el código (`nkey`).
- Hechos, hallazgos, propuestas y resultados son inmutables una vez escritos
  (solo cambia su estado); cada nodo guarda un hash de su contenido.
- Point-in-time: un nodo no puede derivarse de evidencia disponible después que él.
- La frontera (intenciones abiertas) se reclama de forma atómica.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

SCHEMA = (Path(__file__).with_name("schema.sql")).read_text(encoding="utf-8")

# Reglas de aristas: rel -> {tipo_origen: {tipos_destino}}
EDGE_RULES: dict[str, dict[str, set[str]]] = {
    "spawns": {
        "begin": {"goal"},
        "goal": {"intent", "goal"},
        "intent": {"intent"},
        "finding": {"intent"},
        "hint": {"intent"},
        "outcome": {"intent"},
    },
    "yields": {"intent": {"fact", "finding", "proposal"}},
    # src se deriva de dst (apunta hacia la evidencia)
    "derived_from": {
        "fact": {"fact"},
        "finding": {"fact", "finding"},
        "proposal": {"finding"},
        "outcome": {"proposal"},
    },
    "proves": {"fact": {"finding", "goal"}, "finding": {"finding", "goal"}},
    "covers": {"digest": {"goal", "intent", "fact", "finding", "proposal", "outcome", "hint"}},
}

INITIAL_STATE = {
    "begin": "open", "goal": "open", "intent": "open", "fact": "confirmed",
    "finding": "confirmed", "proposal": "pending", "outcome": "realized",
    "hint": "active", "digest": "active",
}

# Transiciones de estado permitidas
TRANSITIONS: dict[str, dict[str, set[str]]] = {
    "goal": {"open": {"met", "abandoned"}},
    "intent": {
        "open": {"running", "stopped"},
        "running": {"done", "blocked", "exhausted", "open", "stopped"},
        "blocked": {"open", "stopped"},
    },
    "fact": {"confirmed": {"dismissed"}},
    "finding": {"confirmed": {"dismissed"}},
    "proposal": {"pending": {"approved", "vetoed"}, "approved": {"executed"}},
    "hint": {"active": {"consumed"}},
    "digest": {"active": {"superseded"}},
}

IMMUTABLE_KINDS = {"fact", "finding", "proposal", "outcome"}


class GraphError(ValueError):
    pass


def instrument_key(type_: str, symbol: str, venue: str = "") -> str:
    """Clave estable de deduplicación, calculada por código."""
    return f"{type_.strip().lower()}:{venue.strip().upper()}:{symbol.strip().upper()}"


def _hash(kind: str, payload: dict) -> str:
    body = json.dumps({"kind": kind, "payload": payload}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(body.encode()).hexdigest()


@dataclass
class Node:
    id: int
    exploration_id: int
    kind: str
    state: str
    payload: dict
    priority: int
    owner: str | None
    origin: str | None
    round_no: int
    available_at: str | None

    @classmethod
    def from_row(cls, r: sqlite3.Row) -> "Node":
        return cls(r["id"], r["exploration_id"], r["kind"], r["state"], json.loads(r["payload"]),
                   r["priority"], r["owner"], r["origin"], r["round_no"], r["available_at"])


class DoubleGraph:
    def __init__(self, path: str | Path = ":memory:"):
        # isolation_level=None: controlamos las transacciones a mano
        self.db = sqlite3.connect(str(path), isolation_level=None, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        if str(path) != ":memory:":
            self.db.execute("PRAGMA journal_mode = WAL")
        self.db.executescript(SCHEMA)

    # ------------------------------------------------------------------ util
    def _tx(self):
        return _Tx(self.db)

    def close(self) -> None:
        self.db.close()

    # ================================================== A. grafo de mercado
    def upsert_instrument(self, type_: str, symbol: str, venue: str = "", *, name: str | None = None,
                          tradable: bool = False, extra: dict | None = None) -> int:
        key = instrument_key(type_, symbol, venue)
        with self._tx():
            row = self.db.execute("SELECT id FROM instruments WHERE nkey=?", (key,)).fetchone()
            if row:
                self.db.execute(
                    "UPDATE instruments SET last_seen=strftime('%Y-%m-%dT%H:%M:%fZ','now'),"
                    " name=COALESCE(?,name), tradable=MAX(tradable,?) WHERE id=?",
                    (name, int(tradable), row["id"]))
                return row["id"]
            cur = self.db.execute(
                "INSERT INTO instruments(type,nkey,symbol,venue,name,tradable,extra) VALUES (?,?,?,?,?,?,?)",
                (type_, key, symbol.upper(), venue.upper(), name, int(tradable), json.dumps(extra or {})))
            return cur.lastrowid

    def link_instruments(self, src: int, rel: str, dst: int, weight: float | None = None) -> None:
        self.db.execute(
            "INSERT INTO instrument_links(src_id,dst_id,rel,weight) VALUES (?,?,?,?)"
            " ON CONFLICT(src_id,rel,dst_id) DO UPDATE SET weight=excluded.weight",
            (src, dst, rel, weight))

    def instrument(self, instrument_id: int) -> sqlite3.Row:
        return self.db.execute("SELECT * FROM instruments WHERE id=?", (instrument_id,)).fetchone()

    def tradable_instruments(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM instruments WHERE tradable=1 ORDER BY id").fetchall()

    # ============================================= B. grafo de exploración
    def create_exploration(self, goal: str, description: str | None = None) -> tuple[int, int]:
        """Crea un ciclo con su nodo raíz y su objetivo. Devuelve (exploration_id, goal_node_id)."""
        with self._tx():
            eid = self.db.execute("INSERT INTO explorations(goal,description) VALUES (?,?)",
                                  (goal, description)).lastrowid
            begin = self._insert_node(eid, "begin", {"goal": goal}, origin="system")
            gid = self._insert_node(eid, "goal", {"text": goal}, origin="system")
            self._insert_edge(eid, begin, "spawns", gid)
        return eid, gid

    def exploration(self, eid: int) -> sqlite3.Row:
        return self.db.execute("SELECT * FROM explorations WHERE id=?", (eid,)).fetchone()

    def _insert_node(self, eid: int, kind: str, payload: dict, *, priority: int = 0, origin: str | None = None,
                     owner: str | None = None, available_at: str | None = None,
                     state: str | None = None) -> int:
        if kind not in INITIAL_STATE:
            raise GraphError(f"tipo de nodo desconocido: {kind}")
        rnd = self.db.execute("SELECT round_no FROM explorations WHERE id=?", (eid,)).fetchone()
        if rnd is None:
            raise GraphError(f"exploración {eid} no existe")
        return self.db.execute(
            "INSERT INTO nodes(exploration_id,kind,state,payload,priority,origin,owner,content_hash,"
            " round_no,available_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (eid, kind, state or INITIAL_STATE[kind], json.dumps(payload, ensure_ascii=False), priority,
             origin, owner, _hash(kind, payload), rnd["round_no"], available_at)).lastrowid

    def add_node(self, eid: int, kind: str, payload: dict, *, parents: Iterable[tuple[str, int]] = (),
                 anchors: Iterable[int] = (), priority: int = 0, origin: str | None = None,
                 owner: str | None = None, available_at: str | None = None) -> int:
        """Añade un nodo y sus aristas de entrada/salida en una sola transacción.

        `parents` son pares (rel, otro_id):
          - para 'spawns' y 'yields' el otro nodo es el ORIGEN (otro -> nuevo)
          - para 'derived_from' y 'proves' el nuevo nodo es el ORIGEN (nuevo -> otro)
        """
        with self._tx():
            nid = self._insert_node(eid, kind, payload, priority=priority, origin=origin, owner=owner,
                                    available_at=available_at)
            for rel, other in parents:
                if rel in ("spawns", "yields"):
                    self._insert_edge(eid, other, rel, nid)
                else:
                    self._insert_edge(eid, nid, rel, other)
            for iid in anchors:
                self.db.execute("INSERT OR IGNORE INTO anchors(node_id,instrument_id) VALUES (?,?)", (nid, iid))
        return nid

    def add_edge(self, src: int, rel: str, dst: int) -> None:
        with self._tx():
            eid = self.node(src).exploration_id
            self._insert_edge(eid, src, rel, dst)

    def _insert_edge(self, eid: int, src: int, rel: str, dst: int) -> None:
        if src == dst:
            raise GraphError("una arista no puede apuntar a sí misma")
        s, d = self.node(src), self.node(dst)
        if s.exploration_id != eid or d.exploration_id != eid:
            raise GraphError("las aristas no cruzan exploraciones; usa anclas al grafo de mercado")
        allowed = EDGE_RULES.get(rel, {}).get(s.kind)
        if not allowed or d.kind not in allowed:
            raise GraphError(f"arista no permitida: {s.kind} -{rel}-> {d.kind}")
        if rel == "derived_from" and s.available_at and d.available_at and d.available_at > s.available_at:
            raise GraphError(
                f"point-in-time violado: el nodo {src} ({s.available_at}) usa evidencia {dst} "
                f"disponible después ({d.available_at})")
        # Dirección de flujo (causa -> efecto): spawns/yields van hacia delante;
        # derived_from/proves/covers apuntan hacia la evidencia, así que se invierten.
        a, b = (src, dst) if rel in ("spawns", "yields") else (dst, src)
        if self._flow_reaches(b, a):
            raise GraphError("la arista crearía un ciclo")
        self.db.execute("INSERT OR IGNORE INTO edges(exploration_id,src_id,dst_id,rel) VALUES (?,?,?,?)",
                        (eid, src, dst, rel))

    def _flow_reaches(self, start: int, target: int) -> bool:
        row = self.db.execute(
            "WITH RECURSIVE r(id) AS (SELECT ?"
            "  UNION SELECT e.dst_id FROM edges e JOIN r ON e.src_id=r.id WHERE e.rel IN ('spawns','yields')"
            "  UNION SELECT e.src_id FROM edges e JOIN r ON e.dst_id=r.id"
            "   WHERE e.rel IN ('derived_from','proves','covers'))"
            " SELECT 1 FROM r WHERE id=? LIMIT 1", (start, target)).fetchone()
        return row is not None

    def anchor(self, node_id: int, instrument_id: int) -> None:
        self.db.execute("INSERT OR IGNORE INTO anchors(node_id,instrument_id) VALUES (?,?)",
                        (node_id, instrument_id))

    def node(self, node_id: int) -> Node:
        r = self.db.execute("SELECT * FROM nodes WHERE id=?", (node_id,)).fetchone()
        if r is None:
            raise GraphError(f"nodo {node_id} no existe")
        return Node.from_row(r)

    def nodes(self, eid: int, kind: str | None = None, state: str | None = None) -> list[Node]:
        q, args = "SELECT * FROM nodes WHERE exploration_id=?", [eid]
        if kind:
            q += " AND kind=?"; args.append(kind)
        if state:
            q += " AND state=?"; args.append(state)
        return [Node.from_row(r) for r in self.db.execute(q + " ORDER BY id", args)]

    def set_state(self, node_id: int, new_state: str, *, reason: str | None = None) -> None:
        n = self.node(node_id)
        allowed = TRANSITIONS.get(n.kind, {}).get(n.state, set())
        if new_state not in allowed:
            raise GraphError(f"transición no permitida para {n.kind}: {n.state} -> {new_state}")
        done = new_state in ("done", "exhausted", "stopped", "met", "abandoned", "executed")
        self.db.execute(
            "UPDATE nodes SET state=?, blocked_reason=?, content_version=content_version+1,"
            " updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now'),"
            " completed_at=CASE WHEN ? THEN strftime('%Y-%m-%dT%H:%M:%fZ','now') ELSE completed_at END"
            " WHERE id=?", (new_state, reason, int(done), node_id))

    def update_payload(self, node_id: int, payload: dict) -> None:
        n = self.node(node_id)
        if n.kind in IMMUTABLE_KINDS:
            raise GraphError(f"los nodos '{n.kind}' son inmutables")
        self.db.execute(
            "UPDATE nodes SET payload=?, content_hash=?, content_version=content_version+1,"
            " updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?",
            (json.dumps(payload, ensure_ascii=False), _hash(n.kind, payload), node_id))

    def verify_integrity(self) -> list[int]:
        """Devuelve los ids de nodos cuyo contenido no coincide con su hash."""
        bad = []
        for r in self.db.execute("SELECT id,kind,payload,content_hash FROM nodes"):
            if _hash(r["kind"], json.loads(r["payload"])) != r["content_hash"]:
                bad.append(r["id"])
        return bad

    # ----------------------------------------------------- frontera (cola)
    def claim_intent(self, eid: int, owner: str, capability: str | None = None) -> Node | None:
        """Reclama atómicamente la intención abierta de mayor prioridad.

        Equivalente en SQLite al `SELECT ... FOR UPDATE SKIP LOCKED` de Postgres:
        BEGIN IMMEDIATE toma el bloqueo de escritura, así dos trabajadores nunca
        reclaman la misma intención.
        """
        with self._tx():
            q = ("SELECT id FROM nodes WHERE exploration_id=? AND kind='intent' AND state='open'")
            args: list[Any] = [eid]
            if capability:
                q += " AND json_extract(payload,'$.capability')=?"; args.append(capability)
            row = self.db.execute(q + " ORDER BY priority DESC, id LIMIT 1", args).fetchone()
            if row is None:
                return None
            self.db.execute(
                "UPDATE nodes SET state='running', owner=?, content_version=content_version+1,"
                " updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=? AND state='open'",
                (owner, row["id"]))
        return self.node(row["id"])

    # --------------------------------------------------------- consultas
    def anchored_instruments(self, node_id: int) -> list[int]:
        return [r[0] for r in self.db.execute(
            "SELECT instrument_id FROM anchors WHERE node_id=? ORDER BY instrument_id", (node_id,))]

    def nodes_for_instrument(self, instrument_id: int, kind: str | None = None) -> list[Node]:
        q = "SELECT n.* FROM nodes n JOIN anchors a ON a.node_id=n.id WHERE a.instrument_id=?"
        args: list[Any] = [instrument_id]
        if kind:
            q += " AND n.kind=?"; args.append(kind)
        return [Node.from_row(r) for r in self.db.execute(q + " ORDER BY n.id", args)]

    def uncovered_instruments(self, eid: int) -> list[sqlite3.Row]:
        """Instrumentos operables sin ninguna intención (viva o terminada) en este ciclo.

        Ojo: es cobertura del grafo, no garantía de análisis exhaustivo.
        """
        return self.db.execute(
            "SELECT i.* FROM instruments i WHERE i.tradable=1 AND NOT EXISTS ("
            "  SELECT 1 FROM anchors a JOIN nodes n ON n.id=a.node_id"
            "  WHERE a.instrument_id=i.id AND n.exploration_id=? AND n.kind='intent'"
            "  AND n.state IN ('open','running','done','exhausted')) ORDER BY i.id", (eid,)).fetchall()

    def lineage(self, node_id: int) -> list[Node]:
        """Camino de evidencia hacia atrás (derived_from / proves / yields inverso / spawns inverso)."""
        rows = self.db.execute(
            "WITH RECURSIVE up(id) AS (SELECT ? UNION"
            "  SELECT e.dst_id FROM edges e JOIN up ON e.src_id=up.id WHERE e.rel IN ('derived_from','proves')"
            "  UNION SELECT e.src_id FROM edges e JOIN up ON e.dst_id=up.id WHERE e.rel IN ('yields','spawns'))"
            " SELECT n.* FROM nodes n JOIN up ON up.id=n.id ORDER BY n.id", (node_id,)).fetchall()
        return [Node.from_row(r) for r in rows]

    def constraints(self, eid: int | None = None) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM constraints WHERE exploration_id IS NULL OR exploration_id=? ORDER BY id",
            (eid,)).fetchall()

    def add_constraint(self, kind: str, text: str, eid: int | None = None) -> int:
        return self.db.execute("INSERT INTO constraints(exploration_id,kind,text) VALUES (?,?,?)",
                               (eid, kind, text)).lastrowid

    # ================================== crédito realizado (mejora propia)
    def record_outcome(self, proposal_id: int, pnl_net: float, *, available_at: str | None = None,
                       owner: str = "auditor") -> int:
        """Registra el resultado neto de costes de una propuesta ejecutada y reparte el crédito."""
        p = self.node(proposal_id)
        if p.kind != "proposal" or p.state != "executed":
            raise GraphError("solo se registran resultados de propuestas ejecutadas")
        oid = self.add_node(p.exploration_id, "outcome", {"pnl_net": pnl_net},
                            parents=[("derived_from", proposal_id)],
                            anchors=self.anchored_instruments(proposal_id),
                            owner=owner, available_at=available_at)
        self._propagate_credit(oid, pnl_net)
        return oid

    def _propagate_credit(self, start: int, amount: float) -> None:
        """Reparte `amount` hacia atrás a partes iguales por cada nivel de evidencia.

        outcome -> propuesta -> hallazgos -> hechos, y a cada nodo se le suma
        también lo que reciben las intenciones que lo produjeron (yields inverso).
        Cada nivel recibe el total repartido entre sus nodos, así que el crédito
        mide contribución por nivel, no dinero contable. El grafo es acíclico,
        por lo que el recorrido termina.
        """
        frontier: dict[int, float] = {start: amount}
        with self._tx():
            while frontier:
                nxt: dict[int, float] = {}
                for nid, amt in frontier.items():
                    self.db.execute(
                        "INSERT INTO credit(node_id,amount) VALUES (?,?)"
                        " ON CONFLICT(node_id) DO UPDATE SET amount=amount+excluded.amount", (nid, amt))
                    producers = [r[0] for r in self.db.execute(
                        "SELECT src_id FROM edges WHERE dst_id=? AND rel='yields'", (nid,))]
                    for pid in producers:  # la intención que lo produjo recibe el mismo crédito
                        self.db.execute(
                            "INSERT INTO credit(node_id,amount) VALUES (?,?)"
                            " ON CONFLICT(node_id) DO UPDATE SET amount=amount+excluded.amount", (pid, amt))
                    parents = [r[0] for r in self.db.execute(
                        "SELECT dst_id FROM edges WHERE src_id=? AND rel='derived_from'", (nid,))]
                    for par in parents:
                        nxt[par] = nxt.get(par, 0.0) + amt / len(parents)
                frontier = nxt

    def credit_by_owner(self) -> dict[str, float]:
        return {r["owner"]: r["total"] for r in self.db.execute(
            "SELECT n.owner, ROUND(SUM(c.amount), 6) AS total FROM credit c JOIN nodes n ON n.id=c.node_id"
            " WHERE n.owner IS NOT NULL AND n.kind IN ('fact','finding','proposal','intent')"
            " GROUP BY n.owner ORDER BY total DESC")}

    def node_credit(self, node_id: int) -> float:
        r = self.db.execute("SELECT amount FROM credit WHERE node_id=?", (node_id,)).fetchone()
        return r["amount"] if r else 0.0


class _Tx:
    """Transacción con BEGIN IMMEDIATE, reentrante."""

    def __init__(self, db: sqlite3.Connection):
        self.db = db
        self.outer = False

    def __enter__(self):
        if not self.db.in_transaction:
            self.db.execute("BEGIN IMMEDIATE")
            self.outer = True
        return self

    def __exit__(self, exc_type, *_):
        if self.outer:
            self.db.execute("ROLLBACK" if exc_type else "COMMIT")
        return False
