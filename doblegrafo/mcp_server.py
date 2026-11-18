"""Servidor MCP del doble grafo para Claude Code.

Claude Code (con tu plan Pro) se conecta a este servidor y actúa como planificador
y como agentes trabajadores. El servidor es la única puerta al grafo y hace
cumplir las reglas: Claude puede leer, analizar y PROPONER, pero no hay ninguna
herramienta para aprobar o ejecutar órdenes, registrar resultados de dinero,
cambiar restricciones ni editar lo ya escrito.

Registro en Claude Code (una vez):
    claude mcp add trader -- python -m doblegrafo.mcp_server

Base de datos: variable de entorno TRADER_DB (por defecto ~/.trader/trader.db).
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from mcp.server.fastmcp import FastMCP

from .coord import Planner
from .graph import DoubleGraph, GraphError, Node

mcp = FastMCP(
    "trader-doble-grafo",
    instructions=(
        "Doble grafo de un sistema de trading legal. El grafo de mercado lista instrumentos; "
        "el grafo de exploración guarda objetivos, intenciones, hechos, hallazgos y propuestas de cada ciclo. "
        "Flujo: iniciar_ciclo -> ronda_planificador -> (trabajadores) reclamar_intencion -> "
        "registrar_resultado / proponer_operacion -> terminar_intencion. "
        "Nunca inventes datos de mercado: cada hecho debe venir de una fuente y llevar disponible_en. "
        "Las propuestas solo quedan pendientes; las aprueba el gestor de riesgo, no tú."
    ),
)

_graph: DoubleGraph | None = None


def g() -> DoubleGraph:
    global _graph
    if _graph is None:
        path = Path(os.environ.get("TRADER_DB", Path.home() / ".trader" / "trader.db")).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        _graph = DoubleGraph(path)
    return _graph


def _node_dict(n: Node) -> dict:
    return {"id": n.id, "tipo": n.kind, "estado": n.state, "contenido": n.payload, "agente": n.owner,
            "prioridad": n.priority, "ronda": n.round_no, "disponible_en": n.available_at,
            "instrumentos": [g().instrument(i)["symbol"] for i in g().anchored_instruments(n.id)]}


def _current_eid(exploration_id: int | None) -> int:
    if exploration_id is not None:
        if g().exploration(exploration_id) is None:
            raise GraphError(f"el ciclo {exploration_id} no existe")
        return exploration_id
    r = g().db.execute("SELECT id FROM explorations WHERE status='open' ORDER BY id DESC LIMIT 1").fetchone()
    if r is None:
        raise GraphError("no hay ciclo abierto; usa iniciar_ciclo primero")
    return r["id"]


def _goal_id(eid: int) -> int:
    return g().nodes(eid, kind="goal")[0].id


def _symbol_ids(symbols: list[str]) -> list[int]:
    out = []
    for s in symbols:
        r = g().db.execute("SELECT id FROM instruments WHERE symbol=? ORDER BY tradable DESC, id LIMIT 1",
                           (s.strip().upper(),)).fetchone()
        if r is None:
            raise GraphError(f"instrumento desconocido: {s}")
        out.append(r["id"])
    return out


def _running_intent(intencion_id: int, agente: str) -> Node:
    n = g().node(intencion_id)
    if n.kind != "intent" or n.state != "running":
        raise GraphError(f"el nodo {intencion_id} no es una intención en curso")
    if n.owner != agente:
        raise GraphError(f"la intención {intencion_id} la reclamó {n.owner}, no {agente}")
    return n


# ============================================================ ciclo y mercado
@mcp.tool()
def iniciar_ciclo(objetivo: str, descripcion: str = "") -> dict:
    """Abre un ciclo de decisión nuevo (grafo de exploración) con su objetivo."""
    eid, gid = g().create_exploration(objetivo, descripcion or None)
    return {"ciclo_id": eid, "objetivo_id": gid}


@mcp.tool()
def registrar_instrumento(tipo: Literal["market", "sector", "index", "etf", "stock", "crypto", "fx"],
                          simbolo: str, mercado: str = "", nombre: str = "", operable: bool = False,
                          pertenece_a: str = "") -> dict:
    """Añade o actualiza un instrumento en el grafo de mercado (la clave la calcula el código)."""
    iid = g().upsert_instrument(tipo, simbolo, mercado, name=nombre or None, tradable=operable)
    if pertenece_a:
        g().link_instruments(iid, "belongs_to", _symbol_ids([pertenece_a])[0])
    return {"instrumento_id": iid, "clave": g().instrument(iid)["nkey"]}


@mcp.tool()
def estado_ciclo(ciclo_id: int | None = None) -> dict:
    """Resumen del ciclo: ronda, nodos por tipo y estado, frontera, cobertura, restricciones y pendientes."""
    eid = _current_eid(ciclo_id)
    db = g().db
    counts = [dict(r) for r in db.execute(
        "SELECT kind AS tipo, state AS estado, COUNT(*) AS n FROM nodes WHERE exploration_id=?"
        " GROUP BY kind, state ORDER BY kind, state", (eid,))]
    frontier = [_node_dict(n) for n in g().nodes(eid, kind="intent", state="open")[:10]]
    ex = g().exploration(eid)
    handoff = Planner(g(), eid, _goal_id(eid)).load_handoff()
    return {"ciclo_id": eid, "objetivo": ex["goal"], "ronda": ex["round_no"], "estado": ex["status"],
            "nodos": counts, "frontera": frontier,
            "instrumentos_sin_cubrir": [r["symbol"] for r in g().uncovered_instruments(eid)],
            "restricciones": [{"tipo": c["kind"], "texto": c["text"]} for c in g().constraints(eid)],
            "pendientes_planificador": handoff}


# ================================================================ planificador
@mcp.tool()
def ronda_planificador(ciclo_id: int | None = None, max_nuevas: int = 10) -> dict:
    """Ejecuta una ronda: crea intenciones para huecos, hallazgos sin evaluar y pistas; marca ramas frías."""
    eid = _current_eid(ciclo_id)
    p = Planner(g(), eid, _goal_id(eid), max_new_per_round=max(1, min(max_nuevas, 50)))
    created = p.run_round()
    return {"nuevas_intenciones": [_node_dict(g().node(i)) for i in created],
            "ramas_frias": len(p.cold_nodes())}


@mcp.tool()
def guardar_pendientes(pendientes: list[dict], notas: str = "", ciclo_id: int | None = None) -> dict:
    """Guarda la lista de pendientes del planificador (máx. 64 elementos y 64 KiB; se conservan 20 versiones)."""
    eid = _current_eid(ciclo_id)
    v = Planner(g(), eid, _goal_id(eid)).save_handoff(pendientes, notas)
    return {"version": v}


@mcp.tool()
def resumir_ramas_frias(resumen: str, ciclo_id: int | None = None) -> dict:
    """Crea un nodo resumen que cubre las ramas sin actividad, para no recargar el contexto."""
    eid = _current_eid(ciclo_id)
    did = Planner(g(), eid, _goal_id(eid)).digest(resumen)
    return {"resumen_id": did}


@mcp.tool()
def agregar_pista(texto: str, simbolos: list[str] | None = None,
                  capacidad: Literal["analyze", "propose", "verify"] = "analyze",
                  ciclo_id: int | None = None) -> dict:
    """Añade una pista (idea a investigar). El planificador la convertirá en intención en su próxima ronda."""
    eid = _current_eid(ciclo_id)
    sym = simbolos or []
    hid = g().add_node(eid, "hint", {"text": texto, "capability": capacidad,
                                     **({"symbol": sym[0].upper()} if len(sym) == 1 else {})},
                       anchors=_symbol_ids(sym), origin="claude")
    return {"pista_id": hid}


# ================================================================== trabajadores
@mcp.tool()
def reclamar_intencion(agente: str, capacidad: Literal["analyze", "propose", "verify"] | None = None,
                       ciclo_id: int | None = None) -> dict:
    """Reclama la intención abierta de mayor prioridad (nadie más podrá tomarla)."""
    eid = _current_eid(ciclo_id)
    n = g().claim_intent(eid, agente, capacidad)
    if n is None:
        return {"intencion": None, "mensaje": "no hay intenciones abiertas"}
    contexto = [_node_dict(x) for x in g().lineage(n.id) if x.id != n.id and x.kind != "begin"]
    previos = []
    for iid in g().anchored_instruments(n.id):
        previos += [_node_dict(x) for x in g().nodes_for_instrument(iid, "finding")[-5:]]
    return {"intencion": _node_dict(n), "contexto": contexto, "hallazgos_previos_del_instrumento": previos}


@mcp.tool()
def registrar_resultado(intencion_id: int, agente: str, tipo: Literal["fact", "finding"], contenido: dict,
                        disponible_en: str, deriva_de: list[int] | None = None,
                        prueba: list[int] | None = None, simbolos: list[str] | None = None) -> dict:
    """Escribe un hecho (dato observado, con su fuente) o un hallazgo (señal validada) producido por la intención.

    disponible_en: momento ISO-8601 UTC en que el dato existía (no puede depender de evidencia posterior).
    deriva_de: ids de hechos/hallazgos en los que se basa. prueba: ids de hallazgos u objetivos que respalda.
    """
    it = _running_intent(intencion_id, agente)
    if tipo == "fact" and not contenido.get("fuente"):
        raise GraphError("un hecho necesita 'fuente' en su contenido (de dónde salió el dato)")
    if tipo == "finding" and not deriva_de:
        raise GraphError("un hallazgo debe derivar de al menos un hecho o hallazgo")
    parents = [("yields", it.id)] + [("derived_from", x) for x in (deriva_de or [])] \
        + [("proves", x) for x in (prueba or [])]
    anchors = _symbol_ids(simbolos) if simbolos else g().anchored_instruments(it.id)
    nid = g().add_node(it.exploration_id, tipo, contenido, parents=parents, anchors=anchors, owner=agente,
                       origin="claude", available_at=disponible_en)
    return {"nodo_id": nid}


@mcp.tool()
def proponer_operacion(intencion_id: int, agente: str, simbolo: str, lado: Literal["buy", "sell"],
                       cantidad: float, precio_stop: float, razon: str, hallazgos: list[int],
                       disponible_en: str) -> dict:
    """Propone una operación basada en hallazgos confirmados. Queda PENDIENTE: no se ejecuta nada.

    El gestor de riesgo (código fuera de tu alcance) decide si se aprueba, recorta o veta.
    """
    it = _running_intent(intencion_id, agente)
    if cantidad <= 0 or precio_stop <= 0:
        raise GraphError("cantidad y precio_stop deben ser positivos")
    if not hallazgos:
        raise GraphError("una propuesta necesita al menos un hallazgo que la respalde")
    for h in hallazgos:
        n = g().node(h)
        if n.kind != "finding" or n.state != "confirmed":
            raise GraphError(f"el nodo {h} no es un hallazgo confirmado")
    pid = g().add_node(it.exploration_id, "proposal",
                       {"symbol": simbolo.upper(), "side": lado, "qty": cantidad, "stop": precio_stop,
                        "reason": razon},
                       parents=[("yields", it.id)] + [("derived_from", h) for h in hallazgos],
                       anchors=_symbol_ids([simbolo]), owner=agente, origin="claude",
                       available_at=disponible_en)
    return {"propuesta_id": pid, "estado": "pending",
            "nota": "La propuesta espera al gestor de riesgo; no se ha enviado ninguna orden."}


@mcp.tool()
def terminar_intencion(intencion_id: int, agente: str,
                       estado: Literal["done", "exhausted", "blocked", "open"], motivo: str = "") -> dict:
    """Cierra la intención: done (produjo algo), exhausted (no había nada), blocked (error), open (devolverla)."""
    _running_intent(intencion_id, agente)
    if estado == "blocked" and not motivo:
        raise GraphError("indica el motivo del bloqueo")
    g().set_state(intencion_id, estado, reason=motivo or None)
    return {"intencion_id": intencion_id, "estado": estado}


# ===================================================================== consultas
@mcp.tool()
def consultar_instrumento(simbolo: str, tipo: Literal["intent", "fact", "finding", "proposal", "outcome"] | None = None,
                          limite: int = 20) -> dict:
    """Todo lo que el grafo sabe de un instrumento en todos los ciclos (vía anclas), lo más reciente primero."""
    iid = _symbol_ids([simbolo])[0]
    nodes = g().nodes_for_instrument(iid, tipo)[-max(1, min(limite, 100)):]
    rel = [dict(r) for r in g().db.execute(
        "SELECT l.rel AS relacion, i.symbol AS con, l.weight AS peso FROM instrument_links l"
        " JOIN instruments i ON i.id=l.dst_id WHERE l.src_id=?", (iid,))]
    return {"instrumento": dict(g().instrument(iid)), "relaciones": rel,
            "nodos": [_node_dict(n) for n in reversed(nodes)]}


@mcp.tool()
def linaje(nodo_id: int) -> dict:
    """Cadena de evidencia de un nodo: de qué hechos, hallazgos e intenciones salió."""
    return {"linaje": [_node_dict(n) for n in g().lineage(nodo_id)]}


@mcp.tool()
def credito_por_agente() -> dict:
    """Crédito realizado (ganancia o pérdida neta repartida) acumulado por cada agente."""
    return {"credito": g().credit_by_owner()}


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
