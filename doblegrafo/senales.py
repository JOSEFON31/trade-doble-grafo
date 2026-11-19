"""Tercer grafo: registro de señales externas con crédito fuera de muestra.

Cada fuente externa (funding, basis, volatilidad implícita, reloj del halving,
stablecoins...) es un nodo `señal`. Sus valores se guardan con DOS fechas:

- `ts`: a qué momento se refiere el dato (por ejemplo, el funding de las 08:00).
- `published_at`: cuándo estuvo disponible de verdad. Las consultas `as_of()`
  solo devuelven lo publicado hasta ese instante, así un backtest nunca ve el futuro.

Los valores no se editan: reescribir un dato con otro valor lanza un error.

Cada evaluación fuera de muestra (una ventana de walk-forward que la elección de
parámetros no vio) suma o resta crédito: Sharpe de la señal menos Sharpe de la
referencia. El crédito viejo se desvanece (vida media configurable), porque la
evidencia dice que las señales de BTC se degradan ciclo a ciclo. Si tras un mínimo
de ventanas el crédito queda negativo, la señal se apaga sola; puede volver a
encenderse si el crédito se recupera.

Las señales también pueden colgarse de instrumentos del grafo de mercado
(`señal ↔ instrumento`), igual que las anclas del grafo de exploración.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    source TEXT NOT NULL,
    use TEXT NOT NULL CHECK (use IN ('direccion','tamano','carry','contexto')),
    description TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'prueba' CHECK (state IN ('prueba','activa','apagada')),
    credit REAL NOT NULL DEFAULT 0,
    windows INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE IF NOT EXISTS signal_values (
    signal_id INTEGER NOT NULL REFERENCES signals(id),
    ts TEXT NOT NULL,
    published_at TEXT NOT NULL,
    value REAL NOT NULL,
    PRIMARY KEY (signal_id, ts),
    CHECK (published_at >= ts)
);
CREATE INDEX IF NOT EXISTS signal_values_pub ON signal_values(signal_id, published_at);
CREATE TABLE IF NOT EXISTS signal_instruments (
    signal_id INTEGER NOT NULL REFERENCES signals(id),
    symbol TEXT NOT NULL,
    PRIMARY KEY (signal_id, symbol)
);
CREATE TABLE IF NOT EXISTS signal_evaluations (
    id INTEGER PRIMARY KEY,
    signal_id INTEGER NOT NULL REFERENCES signals(id),
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    params TEXT NOT NULL,
    sharpe REAL NOT NULL,
    baseline_sharpe REAL NOT NULL,
    total_return REAL NOT NULL,
    max_drawdown REAL NOT NULL,
    credit_after REAL NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE (signal_id, window_start, window_end, params)
);
CREATE TRIGGER IF NOT EXISTS signal_values_no_update BEFORE UPDATE ON signal_values
BEGIN SELECT RAISE(ABORT, 'los valores de una señal no se editan'); END;
CREATE TRIGGER IF NOT EXISTS signal_evaluations_no_update BEFORE UPDATE ON signal_evaluations
BEGIN SELECT RAISE(ABORT, 'las evaluaciones no se editan'); END;
"""


class SignalError(ValueError):
    pass


@dataclass
class Signal:
    id: int
    name: str
    source: str
    use: str
    state: str
    credit: float
    windows: int


class SignalRegistry:
    """Registro de señales. Puede compartir el archivo SQLite del doble grafo."""

    def __init__(self, path: str | Path | sqlite3.Connection = ":memory:", *, half_life: float = 4.0,
                 min_windows: int = 3):
        self.db = path if isinstance(path, sqlite3.Connection) else sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.decay = 0.5 ** (1 / half_life) if half_life > 0 else 1.0
        self.min_windows = min_windows

    # ------------------------------------------------------------ señales
    def register(self, name: str, source: str, use: str, description: str = "",
                 symbols: tuple[str, ...] = ()) -> Signal:
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO signals(name, source, use, description) VALUES (?,?,?,?)",
                            (name, source, use, description))
            sid = self.db.execute("SELECT id FROM signals WHERE name=?", (name,)).fetchone()[0]
            for s in symbols:
                self.db.execute("INSERT OR IGNORE INTO signal_instruments VALUES (?,?)", (sid, s))
        return self.signal(name)

    def signal(self, name: str) -> Signal:
        r = self.db.execute("SELECT id, name, source, use, state, credit, windows FROM signals WHERE name=?",
                            (name,)).fetchone()
        if r is None:
            raise SignalError(f"señal desconocida: {name}")
        return Signal(*r)

    def signals(self, state: str | None = None) -> list[Signal]:
        q = "SELECT id, name, source, use, state, credit, windows FROM signals"
        rows = self.db.execute(q + (" WHERE state=?" if state else "") + " ORDER BY credit DESC",
                               (state,) if state else ()).fetchall()
        return [Signal(*r) for r in rows]

    def for_symbol(self, symbol: str) -> list[Signal]:
        rows = self.db.execute("SELECT s.name FROM signals s JOIN signal_instruments i ON i.signal_id=s.id "
                               "WHERE i.symbol=? ORDER BY s.credit DESC", (symbol,)).fetchall()
        return [self.signal(r[0]) for r in rows]

    # ------------------------------------------------------------ valores
    def record(self, name: str, ts: str, value: float, published_at: str | None = None) -> None:
        self.record_many(name, [(ts, value, published_at or ts)])

    def record_many(self, name: str, rows) -> int:
        """Guarda (ts, valor, publicado) sin editar los existentes. Devuelve cuántos son nuevos."""
        sid = self.signal(name).id
        new = 0
        with self.db:
            for ts, value, pub in rows:
                pub = pub or ts
                old = self.db.execute("SELECT value FROM signal_values WHERE signal_id=? AND ts=?",
                                      (sid, ts)).fetchone()
                if old is not None:
                    if abs(old[0] - float(value)) > 1e-12:
                        raise SignalError(f"{name} {ts}: ya existe con otro valor ({old[0]} ≠ {value})")
                    continue
                if pub < ts:
                    raise SignalError(f"{name} {ts}: publicado antes de su propia fecha")
                self.db.execute("INSERT INTO signal_values VALUES (?,?,?,?)", (sid, ts, pub, float(value)))
                new += 1
        return new

    def as_of(self, name: str, when: str) -> float | None:
        """Último valor publicado hasta `when` (point-in-time)."""
        r = self.db.execute("SELECT value FROM signal_values WHERE signal_id=? AND published_at<=? "
                            "ORDER BY ts DESC LIMIT 1", (self.signal(name).id, when)).fetchone()
        return None if r is None else r[0]

    def history_as_of(self, name: str, when: str, since: str = "") -> list[tuple[str, float]]:
        rows = self.db.execute("SELECT ts, value FROM signal_values WHERE signal_id=? AND published_at<=? "
                               "AND ts>=? ORDER BY ts", (self.signal(name).id, when, since)).fetchall()
        return [(r[0], r[1]) for r in rows]

    # ------------------------------------------------------------ crédito
    def evaluate(self, name: str, window_start: str, window_end: str, *, sharpe: float, baseline_sharpe: float,
                 total_return: float, max_drawdown: float, params: dict | None = None) -> Signal:
        """Suma una ventana fuera de muestra y actualiza crédito y estado.

        Repetir la misma ventana con los mismos parámetros no cuenta dos veces.
        """
        s = self.signal(name)
        p = json.dumps(params or {}, sort_keys=True)
        if self.db.execute("SELECT 1 FROM signal_evaluations WHERE signal_id=? AND window_start=? AND "
                           "window_end=? AND params=?", (s.id, window_start, window_end, p)).fetchone():
            return s
        credit = s.credit * self.decay + (sharpe - baseline_sharpe)
        windows = s.windows + 1
        state = s.state
        if windows >= self.min_windows:
            state = "activa" if credit > 0 else "apagada"
        with self.db:
            self.db.execute("INSERT INTO signal_evaluations(signal_id, window_start, window_end, params, sharpe, "
                            "baseline_sharpe, total_return, max_drawdown, credit_after) VALUES (?,?,?,?,?,?,?,?,?)",
                            (s.id, window_start, window_end, p, sharpe, baseline_sharpe, total_return,
                             max_drawdown, credit))
            self.db.execute("UPDATE signals SET credit=?, windows=?, state=? WHERE id=?",
                            (credit, windows, state, s.id))
        return self.signal(name)

    def evaluations(self, name: str) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM signal_evaluations WHERE signal_id=? ORDER BY window_start",
                               (self.signal(name).id,)).fetchall()

    def ranking(self) -> list[dict]:
        return [{"señal": s.name, "uso": s.use, "estado": s.state, "credito": round(s.credit, 3),
                 "ventanas": s.windows} for s in self.signals()]
