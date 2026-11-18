"""Gestor de riesgo: código determinista que decide si una propuesta se aprueba, se recorta o se veta.

Los límites se leen de un archivo que solo edita el humano (por defecto
~/.trader/limites.json). No hay ninguna herramienta MCP que los cambie.
"""
from __future__ import annotations

import json
import math
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class RiskLimits:
    risk_per_trade: float = 0.005        # 0,5 % del capital entre entrada y stop
    max_position_pct: float = 0.10       # 10 % del capital por instrumento
    max_gross_exposure: float = 1.00     # 100 % (sin apalancamiento)
    daily_loss_stop: float = 0.02        # 2 % de pérdida en el día -> no se abren operaciones
    drawdown_half_risk: float = 0.10     # 10 % desde el máximo -> riesgo a la mitad
    drawdown_kill: float = 0.20          # 20 % desde el máximo -> kill switch
    max_price_deviation: float = 0.10    # orden a más de 10 % del último precio = dedo gordo
    max_price_age_days: int = 5          # precio más viejo que esto = datos no fiables
    allow_short: bool = False
    min_qty: float = 1.0                 # acciones enteras por defecto
    allowed_types: tuple[str, ...] = ("etf", "stock")

    @classmethod
    def load(cls, path: str | Path | None = None) -> "RiskLimits":
        p = Path(path or os.environ.get("TRADER_LIMITS", Path.home() / ".trader" / "limites.json")).expanduser()
        if not p.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(asdict(cls()), indent=2), encoding="utf-8")
            return cls()
        data = json.loads(p.read_text(encoding="utf-8"))
        if "allowed_types" in data:
            data["allowed_types"] = tuple(data["allowed_types"])
        unknown = set(data) - set(cls.__dataclass_fields__)
        if unknown:
            raise ValueError(f"claves desconocidas en {p}: {sorted(unknown)}")
        return cls(**data)


@dataclass
class AccountState:
    equity: float
    peak_equity: float
    day_start_equity: float
    positions: dict[str, float]          # símbolo -> valor de mercado (positivo = largo)
    quantities: dict[str, float] = field(default_factory=dict)
    kill_switch: bool = False

    @property
    def drawdown(self) -> float:
        return 0.0 if self.peak_equity <= 0 else max(0.0, 1 - self.equity / self.peak_equity)

    @property
    def day_pnl_pct(self) -> float:
        return 0.0 if self.day_start_equity <= 0 else self.equity / self.day_start_equity - 1

    @property
    def gross_exposure(self) -> float:
        return sum(abs(v) for v in self.positions.values())


@dataclass
class Decision:
    verdict: str                         # approved | vetoed
    qty: float
    reasons: list[str]
    kill: bool = False                   # True si hay que activar el kill switch


@dataclass
class ProposalView:
    symbol: str
    side: str                            # buy | sell
    qty: float
    stop: float
    instrument_type: str
    tradable: bool


def evaluate(p: ProposalView, acct: AccountState, last_price: float | None, price_age_days: int | None,
             limits: RiskLimits) -> Decision:
    """Aplica los límites en orden. Recorta cuando puede; veta cuando no."""
    reasons: list[str] = []

    def veto(msg: str, kill: bool = False) -> Decision:
        return Decision("vetoed", 0.0, reasons + [msg], kill)

    kill_now = acct.drawdown >= limits.drawdown_kill - 1e-9
    held_qty = acct.quantities.get(p.symbol, 0.0)

    # 1) ventas: siempre se permite reducir o cerrar una posición larga, incluso con el
    #    kill switch o el límite diario activos (reducen riesgo). Sin cortos.
    if p.side == "sell":
        if held_qty <= 0 and not limits.allow_short:
            return veto("ventas en corto no permitidas", kill=kill_now and not acct.kill_switch)
        qty = min(p.qty, held_qty)
        if qty < p.qty:
            reasons.append(f"venta recortada a la posición existente ({qty:g})")
        return Decision("approved", qty, reasons + ["cierre o reducción de posición"],
                        kill=kill_now and not acct.kill_switch)

    # 2) interruptores globales: bloquean nuevas compras
    if acct.kill_switch:
        return veto("kill switch activo")
    if kill_now:
        return veto(f"caída {acct.drawdown:.1%} >= {limits.drawdown_kill:.0%}: se activa kill switch", kill=True)
    if acct.day_pnl_pct <= -limits.daily_loss_stop + 1e-9:
        return veto(f"pérdida del día {acct.day_pnl_pct:.1%} alcanza el límite de {limits.daily_loss_stop:.0%}")

    # 3) instrumento y datos
    if not p.tradable or p.instrument_type not in limits.allowed_types:
        return veto(f"{p.symbol} no es operable o su tipo '{p.instrument_type}' no está permitido")
    if last_price is None or last_price <= 0:
        return veto(f"sin precio para {p.symbol}")
    if price_age_days is None or price_age_days > limits.max_price_age_days:
        return veto(f"precio de {p.symbol} demasiado viejo ({price_age_days} días)")

    # 4) compras: stop coherente y precio sano
    if p.stop >= last_price:
        return veto(f"stop {p.stop} no está por debajo del precio {last_price}")
    if abs(p.stop / last_price - 1) > 0.5:
        return veto("stop a más de 50 % del precio: probablemente un error")

    risk_frac = limits.risk_per_trade
    if acct.drawdown >= limits.drawdown_half_risk - 1e-9:
        risk_frac /= 2
        reasons.append(f"caída {acct.drawdown:.1%}: riesgo por operación a la mitad")

    per_share_risk = last_price - p.stop
    caps = {
        "riesgo por operación": acct.equity * risk_frac / per_share_risk,
        "exposición por instrumento": max(0.0, acct.equity * limits.max_position_pct
                                          - acct.positions.get(p.symbol, 0.0)) / last_price,
        "exposición total": max(0.0, acct.equity * limits.max_gross_exposure - acct.gross_exposure) / last_price,
    }
    qty = p.qty
    for name, cap in caps.items():
        if cap < qty:
            qty = cap
            reasons.append(f"recortada por {name}")
    qty = math.floor(qty / limits.min_qty) * limits.min_qty
    if qty <= 0:
        return veto("tras aplicar los límites la cantidad queda en 0")
    return Decision("approved", qty, reasons or ["dentro de límites"])


def sane_fill_price(order_price: float, last_price: float, limits: RiskLimits) -> bool:
    """Control de 'dedo gordo' antes de enviar cualquier orden."""
    return last_price > 0 and abs(order_price / last_price - 1) <= limits.max_price_deviation
