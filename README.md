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

## Gestor de riesgo y paper trading

El gestor de riesgo (`doblegrafo/risk.py`) es código fijo que aprueba, recorta o veta cada propuesta. Sus límites están en `~/.trader/limites.json`, que se crea con estos valores la primera vez y solo tú editas:

| Límite | Valor por defecto |
| --- | --- |
| Riesgo por operación (entrada − stop) | 0,5 % del capital (la mitad si la caída supera 10 %) |
| Exposición por instrumento | 10 % del capital |
| Exposición total | 100 % (sin apalancamiento) |
| Pérdida del día | 2 % → no se abren operaciones |
| Caída desde el máximo | 20 % → kill switch |
| Precio de llenado | ±10 % del cierre previo, si no la orden se cancela |
| Antigüedad del precio | máx. 5 días |
| Tipos permitidos | ETF y acciones; sin ventas en corto |

El broker de paper (`doblegrafo/paper.py`) usa precios diarios reales y simula con honestidad: las órdenes decididas tras el cierre de un día se llenan a la apertura del siguiente, con deslizamiento (5 pb por defecto) y comisión configurable; los stops se revisan con el mínimo del día y, si el mercado abre por debajo, se sale a la apertura. Cada cierre registra su ganancia o pérdida neta en el grafo y reparte el crédito entre los agentes que originaron la compra.

### Rutina diaria en tu PC

1. Primera vez: `trader-ejecutor --capital 10000` (capital ficticio de paper).
2. Cada día, tras el cierre de EE. UU.: en Claude Code, `/ciclo-diario` (analiza y propone).
3. Después: `trader-ejecutor` (baja precios de Stooq, llena órdenes, revisa stops y pasa las propuestas por el gestor de riesgo). Puedes programar ambos con el Programador de tareas de Windows o cron.
4. Si salta el kill switch, revisa qué pasó y solo entonces: `trader-ejecutor --reset-kill-switch`.

Sin internet o sin Stooq puedes cargar precios desde CSV: `trader-ejecutor --csv SPY=spy.csv --sin-descarga`.

Claude ve la cartera con `estado_cartera` y los motivos de cada veto con `decisiones_riesgo`, pero no puede aprobar, ejecutar ni cambiar límites. `.claude/settings.json` además le niega editar `~/.trader/`, usar `sqlite3` y reiniciar el kill switch.

## Simulador con datos históricos

`trader-backtest` simula años de mercado en segundos con las mismas piezas que el paper trading: doble grafo, gestor de riesgo, llenado a la apertura siguiente, costes y stops. Corre en memoria, no toca tu cartera de paper y no gasta tu plan de Claude, porque las estrategias son reglas deterministas.

```bash
trader-backtest --descargar SPY QQQ IWM EFA TLT GLD   # baja históricos de Stooq (una vez)
trader-backtest --estrategia todas                    # walk-forward de las 4 estrategias
trader-backtest --estrategia momentum --modo simple --params '{"lookback": 63}'
trader-backtest --sintetico                           # datos inventados, solo para ver que todo corre
```

Cómo evita engañarse:

- **Sin datos del futuro:** la estrategia solo ve barras hasta el cierre del día de decisión y la orden se llena al día siguiente.
- **Walk-forward:** elige parámetros con 3 años y los prueba en el año siguiente, que no vio; luego avanza un año. Solo cuenta la curva fuera de muestra.
- **Holdout:** los últimos 12 meses quedan reservados. Úsalos una sola vez al final con `--usar-holdout`.
- **Sharpe deflactado:** cuenta todas las pruebas hechas (también las de Claude) y da la probabilidad de que el Sharpe no sea suerte. Por debajo de 95 % se marca como no fiable.
- **Referencia:** cada resultado se compara con comprar y mantener el primer símbolo.

Estrategias incluidas: `comprar_mantener`, `momentum`, `cruce_medias` y `reversion`. Claude puede probar variantes con la herramienta MCP `lanzar_backtest` (nunca usa el holdout y cada prueba queda contada) y con el comando `/evolucion-semanal`.

Nota: con los límites por defecto (10 % por instrumento) y pocos instrumentos, el sistema estará poco invertido. El simulador muestra la "exposición media" para que lo veas; con unos 10 ETFs puede llegar a invertir el 100 %.

## Pruebas

```bash
python -m unittest discover -s tests -v
```

36 pruebas: doble grafo, servidor MCP de punta a punta, gestor de riesgo, paper trading y simulador (sin datos del futuro, determinismo, límites de riesgo respetados, ventanas walk-forward sin solapes, holdout excluido, Sharpe deflactado más exigente con más pruebas, CLI).

## Siguiente paso

Descargar históricos reales de un universo de ETFs, correr el walk-forward y, solo con lo que pase, arrancar el paper trading de al menos 3 meses.

## Licencia

ARTEX es AGPL-3.0. Aquí no se incluye código suyo, solo se reimplementó la estructura de datos de forma independiente.
