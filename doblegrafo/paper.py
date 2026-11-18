"""Broker de paper trading y ejecutor diario.

Flujo de cada día de mercado (después del cierre de D):
  1. Llenar las órdenes aprobadas el día anterior a la apertura de D (con deslizamiento y comisión).
  2. Revisar stops con el mínimo del día; si se tocan, cerrar al stop (o a la apertura si abrió por debajo).
  3. Valorar la cartera al cierre de D, actualizar máximo, caída y curva de capital.
  4. Pasar las propuestas pendientes del grafo por el gestor de riesgo con el cierre de D:
     aprobadas -> orden para la apertura siguiente; vetadas -> se registran con su motivo.
Cada cierre de lote registra su resultado neto en el grafo y reparte el crédito.

Nunca se opera dentro del mismo día con información del cierre: las órdenes de D se llenan en D+1.
"""
from __future__ import annotations

import csv
import io
import json
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timezone

from .graph import DoubleGraph, GraphError
from .risk import AccountState, ProposalView, RiskLimits, evaluate, sane_fill_price

PAPER_SCHEMA = """
CREATE TABLE IF NOT EXISTS prices (
    symbol TEXT NOT NULL, day TEXT NOT NULL,
    open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL, volume REAL,
    source TEXT NOT NULL, PRIMARY KEY (symbol, day)
);
CREATE TABLE IF NOT EXISTS account (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    initial_cash REAL NOT NULL, cash REAL NOT NULL, peak_equity REAL NOT NULL,
    last_day TEXT, last_equity REAL NOT NULL,
    kill_switch INTEGER NOT NULL DEFAULT 0, kill_reason TEXT
);
CREATE TABLE IF NOT EXISTS lots (
    id INTEGER PRIMARY KEY, symbol TEXT NOT NULL, qty REAL NOT NULL CHECK (qty > 0),
    entry REAL NOT NULL, stop REAL NOT NULL, entry_commission REAL NOT NULL,
    proposal_id INTEGER NOT NULL, opened_day TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY, proposal_id INTEGER NOT NULL UNIQUE, symbol TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('buy','sell')), qty REAL NOT NULL, stop REAL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','filled','cancelled')),
    created_day TEXT NOT NULL, note TEXT
);
CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY, proposal_id INTEGER, lot_id INTEGER, symbol TEXT NOT NULL, side TEXT NOT NULL,
    qty REAL NOT NULL, price REAL NOT NULL, commission REAL NOT NULL, day TEXT NOT NULL, reason TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS risk_decisions (
    proposal_id INTEGER PRIMARY KEY, verdict TEXT NOT NULL, qty REAL NOT NULL,
    reasons TEXT NOT NULL, day TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS equity_curve (day TEXT PRIMARY KEY, equity REAL NOT NULL, cash REAL NOT NULL);
"""


@dataclass(frozen=True)
class CostModel:
    slippage_bps: float = 5.0          # 0,05 % en contra en cada llenado
    commission_per_share: float = 0.0
    commission_min: float = 0.0

    def commission(self, qty: float) -> float:
        return max(self.commission_min, qty * self.commission_per_share) if qty else 0.0

    def slip(self, price: float, side: str) -> float:
        f = self.slippage_bps / 10_000
        return price * (1 + f) if side == "buy" else price * (1 - f)


# ============================================================== precios
def parse_daily_csv(text: str) -> list[dict]:
    """CSV con columnas Date,Open,High,Low,Close[,Volume] (formato de Stooq y de muchas exportaciones)."""
    rows = []
    for r in csv.DictReader(io.StringIO(text)):
        k = {key.strip().lower(): v for key, v in r.items() if key}
        try:
            rows.append({"day": k["date"], "open": float(k["open"]), "high": float(k["high"]),
                         "low": float(k["low"]), "close": float(k["close"]),
                         "volume": float(k["volume"]) if k.get("volume") else None})
        except (KeyError, ValueError, TypeError):
            continue
    return rows


def fetch_stooq(symbol: str, venue_suffix: str = "us", timeout: float = 30) -> list[dict]:
    """Precios diarios gratis de Stooq (p. ej. SPY -> spy.us)."""
    url = f"https://stooq.com/q/d/l/?s={symbol.lower()}.{venue_suffix}&i=d"
    req = urllib.request.Request(url, headers={"User-Agent": "trade-doble-grafo/0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        text = resp.read().decode("utf-8", errors="replace")
    rows = parse_daily_csv(text)
    if not rows:
        raise RuntimeError(f"Stooq no devolvió precios para {symbol} ({text[:80]!r})")
    return rows


def _check_bar(b: dict) -> None:
    if not (b["low"] <= min(b["open"], b["close"]) and b["high"] >= max(b["open"], b["close"]) and b["low"] > 0):
        raise GraphError(f"barra imposible {b}")


# ================================================================ broker
class PaperBroker:
    def __init__(self, g: DoubleGraph, limits: RiskLimits | None = None, costs: CostModel | None = None):
        self.g = g
        self.db = g.db
        self.limits = limits or RiskLimits()
        self.costs = costs or CostModel()
        self.db.executescript(PAPER_SCHEMA)

    # -------------------------------------------------------- cuenta
    def open_account(self, cash: float) -> None:
        if cash <= 0:
            raise ValueError("el capital inicial debe ser positivo")
        self.db.execute("INSERT OR IGNORE INTO account(id,initial_cash,cash,peak_equity,last_equity)"
                        " VALUES (1,?,?,?,?)", (cash, cash, cash, cash))

    def account(self):
        a = self.db.execute("SELECT * FROM account WHERE id=1").fetchone()
        if a is None:
            raise GraphError("no hay cuenta de paper; ábrela con open_account(capital)")
        return a

    def activate_kill_switch(self, reason: str) -> None:
        self.db.execute("UPDATE account SET kill_switch=1, kill_reason=? WHERE id=1", (reason,))

    def reset_kill_switch(self) -> None:
        """Solo para el humano, tras revisar. No se expone por MCP."""
        self.db.execute("UPDATE account SET kill_switch=0, kill_reason=NULL WHERE id=1")

    # -------------------------------------------------------- precios
    def store_prices(self, symbol: str, bars: list[dict], source: str) -> int:
        n = 0
        for b in bars:
            _check_bar(b)
            n += self.db.execute(
                "INSERT OR IGNORE INTO prices(symbol,day,open,high,low,close,volume,source) VALUES (?,?,?,?,?,?,?,?)",
                (symbol.upper(), b["day"], b["open"], b["high"], b["low"], b["close"], b.get("volume"), source)
            ).rowcount
        return n

    def bar(self, symbol: str, day: str):
        return self.db.execute("SELECT * FROM prices WHERE symbol=? AND day=?", (symbol, day)).fetchone()

    def last_bar(self, symbol: str, on_or_before: str):
        return self.db.execute("SELECT * FROM prices WHERE symbol=? AND day<=? ORDER BY day DESC LIMIT 1",
                               (symbol, on_or_before)).fetchone()

    def trading_days(self, after: str | None) -> list[str]:
        q = "SELECT DISTINCT day FROM prices"
        args: tuple = ()
        if after:
            q += " WHERE day>?"; args = (after,)
        return [r[0] for r in self.db.execute(q + " ORDER BY day", args)]

    # ------------------------------------------------------ valoración
    def state(self, day: str) -> AccountState:
        a = self.account()
        positions: dict[str, float] = {}
        qtys: dict[str, float] = {}
        for lot in self.db.execute("SELECT symbol, SUM(qty) q FROM lots GROUP BY symbol"):
            b = self.last_bar(lot["symbol"], day)
            px = b["close"] if b else 0.0
            positions[lot["symbol"]] = lot["q"] * px
            qtys[lot["symbol"]] = lot["q"]
        equity = a["cash"] + sum(positions.values())
        return AccountState(equity=equity, peak_equity=max(a["peak_equity"], equity),
                            day_start_equity=a["last_equity"], positions=positions, quantities=qtys,
                            kill_switch=bool(a["kill_switch"]))

    # ---------------------------------------------------------- día
    def run_day(self, day: str) -> dict:
        """Día completo: apertura (llenados y stops) y cierre (valoración y propuestas)."""
        report = self.open_day(day)
        return self.close_day(day, report)

    def open_day(self, day: str) -> dict:
        """Fase de apertura de D: llena órdenes pendientes y revisa stops."""
        a = self.account()
        if a["last_day"] and day <= a["last_day"]:
            raise GraphError(f"el día {day} ya se procesó (último: {a['last_day']})")
        report = {"day": day, "fills": [], "stops": [], "decisions": [], "skipped": []}
        with self.g._tx():
            self._fill_pending(day, report)
            self._check_stops(day, report)
        return report

    def close_day(self, day: str, report: dict) -> dict:
        """Fase de cierre de D: valora, pasa propuestas por riesgo y guarda la curva de capital.

        Entre open_day y close_day los agentes pueden escribir propuestas con el cierre de D.
        """
        with self.g._tx():
            st = self.state(day)
            self.db.execute("UPDATE account SET peak_equity=MAX(peak_equity,?) WHERE id=1", (st.equity,))
            self._process_proposals(day, st, report)
            st = self.state(day)
            self.db.execute("INSERT OR REPLACE INTO equity_curve(day,equity,cash) VALUES (?,?,?)",
                            (day, st.equity, self.account()["cash"]))
            self.db.execute("UPDATE account SET last_day=?, last_equity=? WHERE id=1", (day, st.equity))
        report.update(equity=round(st.equity, 2), drawdown=round(st.drawdown, 4),
                      kill_switch=bool(self.account()["kill_switch"]))
        return report

    def _fill_pending(self, day: str, report: dict) -> None:
        for o in self.db.execute("SELECT * FROM orders WHERE status='pending' AND created_day<? ORDER BY id",
                                 (day,)).fetchall():
            b = self.bar(o["symbol"], day)
            if b is None:
                report["skipped"].append(f"sin barra de {o['symbol']} el {day}; la orden {o['id']} espera")
                continue
            prev = self.last_bar(o["symbol"], o["created_day"])
            px = self.costs.slip(b["open"], o["side"])
            if prev is None or not sane_fill_price(px, prev["close"], self.limits):
                self._cancel(o, f"precio de apertura {b['open']} fuera de rango frente al cierre previo")
                report["skipped"].append(f"orden {o['id']} cancelada por precio anómalo")
                continue
            if o["side"] == "buy":
                self._buy(o, px, day, report)
            else:
                self._sell_fifo(o["symbol"], o["qty"], px, day, f"venta propuesta {o['proposal_id']}",
                                o["proposal_id"], report)
                self._mark_executed(o["proposal_id"])
            self.db.execute("UPDATE orders SET status='filled' WHERE id=?", (o["id"],))

    def _buy(self, o, px: float, day: str, report: dict) -> None:
        qty = o["qty"]
        cash = self.account()["cash"]
        comm = self.costs.commission(qty)
        if qty * px + comm > cash:  # nunca saldo negativo
            qty = float(int((cash - comm) / px))
            comm = self.costs.commission(qty)
        if qty <= 0:
            self._cancel(o, "sin efectivo suficiente")
            report["skipped"].append(f"orden {o['id']} cancelada por falta de efectivo")
            return
        self.db.execute("UPDATE account SET cash=cash-? WHERE id=1", (qty * px + comm,))
        lot = self.db.execute(
            "INSERT INTO lots(symbol,qty,entry,stop,entry_commission,proposal_id,opened_day) VALUES (?,?,?,?,?,?,?)",
            (o["symbol"], qty, px, o["stop"], comm, o["proposal_id"], day)).lastrowid
        self.db.execute("INSERT INTO fills(proposal_id,lot_id,symbol,side,qty,price,commission,day,reason)"
                        " VALUES (?,?,?,?,?,?,?,?,?)",
                        (o["proposal_id"], lot, o["symbol"], "buy", qty, px, comm, day, "entrada"))
        self._mark_executed(o["proposal_id"])
        report["fills"].append({"symbol": o["symbol"], "side": "buy", "qty": qty, "price": round(px, 4)})

    def _check_stops(self, day: str, report: dict) -> None:
        for lot in self.db.execute("SELECT * FROM lots WHERE opened_day<=? ORDER BY id", (day,)).fetchall():
            b = self.bar(lot["symbol"], day)
            if b is None or b["low"] > lot["stop"]:
                continue
            exit_px = self.costs.slip(min(b["open"], lot["stop"]), "sell")
            self._close_lot(lot, lot["qty"], exit_px, day, "stop", None, report)
            report["stops"].append({"symbol": lot["symbol"], "qty": lot["qty"], "price": round(exit_px, 4)})

    def _sell_fifo(self, symbol: str, qty: float, px: float, day: str, reason: str, sell_pid: int | None,
                   report: dict) -> None:
        left = qty
        for lot in self.db.execute("SELECT * FROM lots WHERE symbol=? ORDER BY id", (symbol,)).fetchall():
            if left <= 0:
                break
            q = min(left, lot["qty"])
            self._close_lot(lot, q, px, day, reason, sell_pid, report)
            left -= q
        report["fills"].append({"symbol": symbol, "side": "sell", "qty": qty - left, "price": round(px, 4)})

    def _close_lot(self, lot, qty: float, px: float, day: str, reason: str, sell_pid: int | None,
                   report: dict) -> None:
        comm = self.costs.commission(qty)
        entry_comm_share = lot["entry_commission"] * qty / lot["qty"]
        pnl = (px - lot["entry"]) * qty - comm - entry_comm_share
        self.db.execute("UPDATE account SET cash=cash+? WHERE id=1", (qty * px - comm,))
        self.db.execute("INSERT INTO fills(proposal_id,lot_id,symbol,side,qty,price,commission,day,reason)"
                        " VALUES (?,?,?,?,?,?,?,?,?)",
                        (sell_pid, lot["id"], lot["symbol"], "sell", qty, px, comm, day, reason))
        if qty >= lot["qty"] - 1e-9:
            self.db.execute("DELETE FROM lots WHERE id=?", (lot["id"],))
        else:
            self.db.execute("UPDATE lots SET qty=qty-?, entry_commission=entry_commission-? WHERE id=?",
                            (qty, entry_comm_share, lot["id"]))
        # el resultado se acredita a la cadena que originó la COMPRA
        self.g.record_outcome(lot["proposal_id"], round(pnl, 6), available_at=f"{day}T23:59:59Z",
                              owner="ejecutor")

    def _mark_executed(self, proposal_id: int) -> None:
        n = self.g.node(proposal_id)
        if n.state == "approved":
            self.g.set_state(proposal_id, "executed")

    def _cancel(self, o, note: str) -> None:
        self.db.execute("UPDATE orders SET status='cancelled', note=? WHERE id=?", (note, o["id"]))

    # ---------------------------------------------------- propuestas
    def _process_proposals(self, day: str, st: AccountState, report: dict) -> None:
        rows = self.db.execute(
            "SELECT id FROM nodes WHERE kind='proposal' AND state='pending' ORDER BY id").fetchall()
        # reservar la exposición de compras aprobadas antes que aún esperan llenarse
        for o in self.db.execute("SELECT symbol, SUM(qty) q FROM orders WHERE side='buy' AND status='pending'"
                                 " GROUP BY symbol").fetchall():
            ob = self.last_bar(o["symbol"], day)
            if ob is not None:
                st.positions[o["symbol"]] = st.positions.get(o["symbol"], 0.0) + o["q"] * ob["close"]
        for r in rows:
            p = self.g.node(r["id"])
            sym = str(p.payload.get("symbol", "")).upper()
            inst = self.db.execute("SELECT * FROM instruments WHERE symbol=? ORDER BY tradable DESC LIMIT 1",
                                   (sym,)).fetchone()
            b = self.last_bar(sym, day)
            age = None if b is None else (date.fromisoformat(day) - date.fromisoformat(b["day"])).days
            view = ProposalView(symbol=sym, side=p.payload.get("side", ""), qty=float(p.payload.get("qty", 0)),
                                stop=float(p.payload.get("stop", 0)),
                                instrument_type=inst["type"] if inst else "", tradable=bool(inst and inst["tradable"]))
            pending_sells = self.db.execute(
                "SELECT COALESCE(SUM(qty),0) FROM orders WHERE symbol=? AND side='sell' AND status='pending'",
                (sym,)).fetchone()[0]
            if pending_sells:
                st.quantities[sym] = max(0.0, st.quantities.get(sym, 0.0) - pending_sells)
            d = evaluate(view, st, b["close"] if b else None, age, self.limits)
            self.db.execute("INSERT OR REPLACE INTO risk_decisions(proposal_id,verdict,qty,reasons,day)"
                            " VALUES (?,?,?,?,?)", (p.id, d.verdict, d.qty, json.dumps(d.reasons, ensure_ascii=False), day))
            if d.kill:
                self.activate_kill_switch(d.reasons[-1])
                st.kill_switch = True
            self.g.set_state(p.id, d.verdict, reason="; ".join(d.reasons))
            if d.verdict == "approved":
                self.db.execute("INSERT INTO orders(proposal_id,symbol,side,qty,stop,created_day) VALUES (?,?,?,?,?,?)",
                                (p.id, sym, view.side, d.qty, view.stop if view.side == "buy" else None, day))
                if view.side == "buy":  # reservar exposición para las siguientes propuestas del mismo día
                    st.positions[sym] = st.positions.get(sym, 0.0) + d.qty * b["close"]
                else:
                    st.quantities[sym] = st.quantities.get(sym, 0.0) - d.qty
            report["decisions"].append({"proposal": p.id, "symbol": sym, "side": view.side,
                                        "verdict": d.verdict, "qty": d.qty, "reasons": d.reasons})


def now_utc_day() -> str:
    return datetime.now(timezone.utc).date().isoformat()
