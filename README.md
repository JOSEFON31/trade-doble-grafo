# Doble grafo para agentes de trading

Réplica propia, en Python y SQLite (el núcleo no tiene dependencias; el servidor MCP usa el paquete `mcp`), del doble grafo con el que ARTEX coordina agentes, adaptada a trading legal. Se hizo leyendo solo el esquema de base de datos publicado de ARTEX (`db/schema.sql`) y artículos públicos, sin clonar el repositorio ni copiar código: el lenguaje, el motor de base de datos, los tipos de nodo de trading y el reparto de crédito son nuevos.

## Qué replica de ARTEX

| ARTEX (Postgres) | Aquí (SQLite) |
| --- | --- |
| `assets` (dominios, IPs, servicios) con claves únicas por tipo | `instruments` (mercado, sector, índice, ETF, acción, cripto, FX) con `nkey` calculada por código |
| `explorations` con `round_no` | `explorations` con `round_no` |
| `exploration_nodes`: begin, goal, intent, fact, finding, hint, digest, con estados válidos por tipo | `nodes`: los mismos + `proposal` y `outcome` para operaciones |
| `exploration_edges`: spawns, derived_from, yields, proves, covers | `edges`: las mismas, con reglas de qué tipo puede enlazar con cuál y sin ciclos |
| `exploration_anchors` (nodo ↔ activo) | `anchors` (nodo ↔ instrumento) |
| `task_constraints` allow/deny | `constraints` allow/deny |
| Índice de frontera sobre intenciones abiertas + `FOR UPDATE SKIP LOCKED` | Índice de frontera + `BEGIN IMMEDIATE` para reclamar de forma atómica |
| `cold_since_round` + nodos `digest` | Igual |
| Checkpoints del planificador (20 versiones, 64 pendientes, 64 KiB) | `planner_handoffs` con los mismos límites |

## Qué añade para trading

- **Point-in-time:** cada nodo guarda `available_at` y no puede derivarse de evidencia posterior. Esto evita usar datos del futuro en los backtests.
- **Inmutabilidad:** hechos, hallazgos, propuestas y resultados no se editan, y cada nodo guarda un hash SHA-256 de su contenido (`verify_integrity()`).
- **Crédito realizado:** `record_outcome()` reparte la ganancia o pérdida neta hacia atrás (resultado → propuesta → hallazgos → hechos → intenciones) y `credit_by_owner()` dice qué agentes aportan valor.
- **Errores visibles:** si un trabajador falla, la intención queda `blocked` con el motivo, en vez de perderse.

## Uso rápido

```python
from doblegrafo import DoubleGraph, Planner, Worker, Result

g = DoubleGraph("trader.db")
spy = g.upsert_instrument("etf", "SPY", "ARCA", tradable=True)
eid, goal = g.create_exploration("Cartera del día")

planner = Planner(g, eid, goal)
planner.run_round()                      # crea intenciones para lo no cubierto

def analizar(g, intent):                 # aquí iría el cálculo real o la llamada a Claude
    return [Result("fact", {"symbol": intent.payload["symbol"], "close": 590.1},
                   available_at="2026-11-18T21:00:00Z")]

Worker(g, eid, "analista", {"analyze": analizar}).run_until_empty()
```

## Conectarlo a Claude Code en tu PC (plan Pro)

1. Instala Python 3.10+ y, dentro de esta carpeta: `pip install -e .`
2. Abre Claude Code en esta carpeta. El archivo `.mcp.json` registra el servidor `trader` automáticamente (Claude Code te pedirá confirmarlo la primera vez). También puedes registrarlo a mano: `claude mcp add trader -- python -m doblegrafo.mcp_server`
3. Comprueba con `/mcp` que `trader` aparece conectado.
4. Registra tus instrumentos una vez, por ejemplo: "registra SPY y QQQ como ETFs operables".
5. Ejecuta el ciclo con el comando `/ciclo-diario`.

La base de datos queda en `~/.trader/trader.db` (cámbiala con la variable `TRADER_DB`).

### Herramientas que ve Claude

| Herramienta | Para qué |
| --- | --- |
| `iniciar_ciclo`, `estado_ciclo` | Abrir y revisar el ciclo |
| `registrar_instrumento` | Añadir instrumentos al grafo de mercado |
| `ronda_planificador`, `guardar_pendientes`, `resumir_ramas_frias`, `agregar_pista` | Planificador |
| `reclamar_intencion`, `registrar_resultado`, `proponer_operacion`, `terminar_intencion` | Trabajadores |
| `consultar_instrumento`, `linaje`, `credito_por_agente` | Consultas |

No existe ninguna herramienta para aprobar o ejecutar órdenes, registrar ganancias, cambiar restricciones ni editar nodos ya escritos. Las propuestas quedan `pending` hasta que exista el gestor de riesgo (código, fase 2 del plan). El servidor además exige que cada hecho tenga `fuente`, que cada hallazgo derive de evidencia y que solo el agente que reclamó una intención pueda cerrarla.

### Subagentes incluidos (`.claude/agents/`)

- `planificador`: abre el ciclo, corre rondas y guarda pendientes.
- `analista`: toma intenciones "analyze", trae datos reales y registra hechos y hallazgos.
- `estratega`: toma intenciones "propose" y propone operaciones solo con evidencia sólida.

## Pruebas

```bash
python -m unittest discover -s tests -v
```

13 pruebas: deduplicación, reglas de aristas, ciclos, point-in-time, inmutabilidad, transiciones, planificador con restricciones, flujo completo con crédito, errores, ramas frías y digest, límites del checkpoint, dos trabajadores en paralelo sin reclamar lo mismo, y un flujo completo por MCP (arranca el servidor y lo usa como Claude Code).

## Siguiente paso

Gestor de riesgo y ejecutor en paper trading (fase 2 del plan).

## Licencia

ARTEX es AGPL-3.0. Aquí no se incluye código suyo, solo se reimplementó la estructura de datos de forma independiente.
