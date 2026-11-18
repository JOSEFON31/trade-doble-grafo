"""Prueba de punta a punta: arranca el servidor MCP por stdio y lo usa como lo haría Claude Code."""
import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

try:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
except ImportError:  # pragma: no cover
    ClientSession = None

ROOT = Path(__file__).resolve().parents[1]


async def _flujo(db_path: str) -> dict:
    params = StdioServerParameters(command=sys.executable, args=["-m", "doblegrafo.mcp_server"],
                                   env={**os.environ, "TRADER_DB": db_path, "PYTHONPATH": str(ROOT)})
    out = {}
    async with stdio_client(params) as (r, w), ClientSession(r, w) as s:
        await s.initialize()

        async def call(name, **args):
            res = await s.call_tool(name, args)
            text = res.content[0].text if res.content else "{}"
            return {"error": text} if res.isError else json.loads(text)

        out["tools"] = sorted(t.name for t in (await s.list_tools()).tools)
        await call("registrar_instrumento", tipo="etf", simbolo="SPY", mercado="ARCA", operable=True)
        await call("iniciar_ciclo", objetivo="Cartera del 2026-11-18")
        ronda = await call("ronda_planificador")
        out["nuevas"] = len(ronda["nuevas_intenciones"])
        it = (await call("reclamar_intencion", agente="analista", capacidad="analyze"))["intencion"]
        out["sin_fuente"] = await call("registrar_resultado", intencion_id=it["id"], agente="analista",
                                       tipo="fact", contenido={"close": 590.1},
                                       disponible_en="2026-11-18T21:00:00Z")
        fact = await call("registrar_resultado", intencion_id=it["id"], agente="analista", tipo="fact",
                          contenido={"close": 590.1, "fuente": "stooq"}, disponible_en="2026-11-18T21:00:00Z")
        find = await call("registrar_resultado", intencion_id=it["id"], agente="analista", tipo="finding",
                          contenido={"signal": "momentum"}, deriva_de=[fact["nodo_id"]],
                          disponible_en="2026-11-18T21:01:00Z")
        out["otro_agente"] = await call("terminar_intencion", intencion_id=it["id"], agente="intruso",
                                        estado="done")
        await call("terminar_intencion", intencion_id=it["id"], agente="analista", estado="done")
        ronda2 = await call("ronda_planificador")
        prop_it = ronda2["nuevas_intenciones"][0]
        out["capacidad2"] = prop_it["contenido"]["capability"]
        claimed = (await call("reclamar_intencion", agente="estratega", capacidad="propose"))["intencion"]
        out["propuesta"] = await call("proponer_operacion", intencion_id=claimed["id"], agente="estratega",
                                      simbolo="SPY", lado="buy", cantidad=1, precio_stop=580.0,
                                      razon="momentum", hallazgos=[find["nodo_id"]],
                                      disponible_en="2026-11-18T21:02:00Z")
        out["estado"] = await call("estado_ciclo")
        out["linaje"] = await call("linaje", nodo_id=out["propuesta"]["propuesta_id"])
    return out


@unittest.skipIf(ClientSession is None, "paquete mcp no instalado")
class MCPTest(unittest.TestCase):
    def test_flujo_por_mcp(self):
        db = os.path.join(tempfile.mkdtemp(), "t.db")
        out = asyncio.run(_flujo(db))
        for forbidden in ("aprobar", "ejecutar", "record_outcome", "add_constraint"):
            self.assertFalse(any(forbidden in t for t in out["tools"]))
        self.assertIn("estado_cartera", out["tools"])
        self.assertIn("decisiones_riesgo", out["tools"])
        self.assertEqual(out["nuevas"], 1)
        self.assertIn("fuente", out["sin_fuente"]["error"])
        self.assertIn("error", out["otro_agente"])
        self.assertEqual(out["capacidad2"], "propose")
        self.assertEqual(out["propuesta"]["estado"], "pending")
        tipos = {n["tipo"] for n in out["linaje"]["linaje"]}
        self.assertTrue({"proposal", "finding", "fact"} <= tipos)
        self.assertEqual(out["estado"]["ronda"], 2)


if __name__ == "__main__":
    unittest.main()
