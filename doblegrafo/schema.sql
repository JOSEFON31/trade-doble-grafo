-- Doble grafo para trading (SQLite). Implementación propia inspirada en la
-- estructura pública de ARTEX: capa de mercado (global) + capa de exploración
-- (por ciclo), unidas por anclas. No contiene código de ARTEX.
PRAGMA foreign_keys = ON;

-- =====================================================================
-- A. Grafo de mercado (global, compartido entre ciclos)
-- =====================================================================
CREATE TABLE IF NOT EXISTS instruments (
    id          INTEGER PRIMARY KEY,
    type        TEXT NOT NULL CHECK (type IN ('market','sector','index','etf','stock','crypto','fx')),
    -- Clave estable calculada por código (nunca por el modelo): type:venue:symbol
    nkey        TEXT NOT NULL UNIQUE,
    symbol      TEXT NOT NULL,
    venue       TEXT NOT NULL DEFAULT '',
    name        TEXT,
    tradable    INTEGER NOT NULL DEFAULT 0 CHECK (tradable IN (0,1)),
    extra       TEXT NOT NULL DEFAULT '{}',
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    last_seen   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_instr_type ON instruments(type);

CREATE TABLE IF NOT EXISTS instrument_links (
    src_id  INTEGER NOT NULL REFERENCES instruments(id) ON DELETE CASCADE,
    dst_id  INTEGER NOT NULL REFERENCES instruments(id) ON DELETE CASCADE,
    rel     TEXT NOT NULL CHECK (rel IN ('belongs_to','tracks','correlated_with','substitute_of')),
    weight  REAL,
    PRIMARY KEY (src_id, rel, dst_id),
    CHECK (src_id <> dst_id)
);

-- =====================================================================
-- B. Grafo de exploración (uno por ciclo / tarea)
-- =====================================================================
CREATE TABLE IF NOT EXISTS explorations (
    id          INTEGER PRIMARY KEY,
    goal        TEXT NOT NULL,
    description TEXT,
    status      TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','achieved','failed')),
    round_no    INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- Tipos de nodo:
--   begin    raíz del ciclo
--   goal     objetivo (p. ej. "decidir la cartera del 2026-11-18")
--   intent   hipótesis / tarea a ejecutar por un trabajador (la frontera)
--   fact     dato observado (precio, indicador, noticia)
--   finding  señal validada (backtest, verificador)
--   proposal operación propuesta (pasa por riesgo)
--   outcome  resultado realizado (PnL neto de costes)
--   hint     pista del humano o del auditor
--   digest   resumen de una rama fría
CREATE TABLE IF NOT EXISTS nodes (
    id               INTEGER PRIMARY KEY,
    exploration_id   INTEGER NOT NULL REFERENCES explorations(id) ON DELETE CASCADE,
    kind             TEXT NOT NULL CHECK (kind IN
                       ('begin','goal','intent','fact','finding','proposal','outcome','hint','digest')),
    state            TEXT NOT NULL,
    payload          TEXT NOT NULL DEFAULT '{}',
    priority         INTEGER NOT NULL DEFAULT 0,
    origin           TEXT,
    owner            TEXT,
    blocked_reason   TEXT,
    content_hash     TEXT NOT NULL,
    content_version  INTEGER NOT NULL DEFAULT 0,
    round_no         INTEGER NOT NULL DEFAULT 0,  -- ronda del planificador en que nació
    cold_since_round INTEGER,
    available_at     TEXT,  -- momento en que el dato estuvo disponible (point-in-time)
    created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    completed_at     TEXT,
    CHECK (
        (kind='begin'    AND state IN ('open')) OR
        (kind='goal'     AND state IN ('open','met','abandoned')) OR
        (kind='intent'   AND state IN ('open','running','done','blocked','exhausted','stopped')) OR
        (kind='fact'     AND state IN ('confirmed','dismissed')) OR
        (kind='finding'  AND state IN ('confirmed','dismissed')) OR
        (kind='proposal' AND state IN ('pending','approved','vetoed','executed')) OR
        (kind='outcome'  AND state IN ('realized')) OR
        (kind='hint'     AND state IN ('active','consumed')) OR
        (kind='digest'   AND state IN ('active','superseded'))
    )
);
CREATE INDEX IF NOT EXISTS idx_nodes_kind ON nodes(exploration_id, kind);
CREATE INDEX IF NOT EXISTS idx_nodes_frontier ON nodes(exploration_id, priority DESC, id)
    WHERE kind='intent' AND state='open';

CREATE TABLE IF NOT EXISTS edges (
    exploration_id INTEGER NOT NULL REFERENCES explorations(id) ON DELETE CASCADE,
    src_id         INTEGER NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    dst_id         INTEGER NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    rel            TEXT NOT NULL CHECK (rel IN ('spawns','derived_from','yields','proves','covers')),
    created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    PRIMARY KEY (exploration_id, src_id, rel, dst_id),
    CHECK (src_id <> dst_id)
);
CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(src_id, rel);
CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(dst_id, rel);

-- =====================================================================
-- C. Anclas: une nodos de exploración con instrumentos del grafo de mercado
-- =====================================================================
CREATE TABLE IF NOT EXISTS anchors (
    node_id       INTEGER NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    instrument_id INTEGER NOT NULL REFERENCES instruments(id) ON DELETE CASCADE,
    PRIMARY KEY (node_id, instrument_id)
);
CREATE INDEX IF NOT EXISTS idx_anchor_instr ON anchors(instrument_id);

-- Restricciones del operador (allow/deny) que se inyectan a planificador y trabajadores
CREATE TABLE IF NOT EXISTS constraints (
    id             INTEGER PRIMARY KEY,
    exploration_id INTEGER REFERENCES explorations(id) ON DELETE CASCADE,  -- NULL = global
    kind           TEXT NOT NULL CHECK (kind IN ('allow','deny')),
    text           TEXT NOT NULL
);

-- Crédito realizado acumulado por nodo y por agente (propagado desde outcomes)
CREATE TABLE IF NOT EXISTS credit (
    node_id  INTEGER PRIMARY KEY REFERENCES nodes(id) ON DELETE CASCADE,
    amount   REAL NOT NULL DEFAULT 0
);

-- Checkpoint versionado de la lista de pendientes del planificador
CREATE TABLE IF NOT EXISTS planner_handoffs (
    exploration_id INTEGER NOT NULL REFERENCES explorations(id) ON DELETE CASCADE,
    version        INTEGER NOT NULL,
    body           TEXT NOT NULL,
    created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    PRIMARY KEY (exploration_id, version)
);
