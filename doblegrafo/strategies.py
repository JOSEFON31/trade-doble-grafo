"""Estrategias base deterministas (genomas) para backtest y para el ciclo diario.

Cada estrategia recibe SOLO las barras hasta el día de decisión (incluido su cierre)
y devuelve señales. El tamaño lo decide el gestor de riesgo, no la estrategia.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from statistics import fmean, pstdev

Bar = dict  # {"day","open","high","low","close","volume"}


@dataclass
class Signal:
    symbol: str
    side: str                     # buy | sell
    stop: float                   # para ventas se ignora
    reason: str
    features: dict = field(default_factory=dict)


def sma(xs: list[float], n: int) -> float | None:
    return fmean(xs[-n:]) if len(xs) >= n else None


def atr(bars: list[Bar], n: int = 20) -> float | None:
    if len(bars) < n + 1:
        return None
    trs = []
    for prev, b in zip(bars[-n - 1:-1], bars[-n:]):
        trs.append(max(b["high"] - b["low"], abs(b["high"] - prev["close"]), abs(b["low"] - prev["close"])))
    return fmean(trs)


class Strategy:
    name = "base"
    PARAM_GRID: dict[str, list] = {}

    def __init__(self, **params):
        unknown = set(params) - set(self.PARAM_GRID)
        if unknown:
            raise ValueError(f"parámetros desconocidos para {self.name}: {sorted(unknown)}")
        self.params = {k: v[0] for k, v in self.PARAM_GRID.items()} | params

    @property
    def label(self) -> str:
        return self.name + "(" + ",".join(f"{k}={v}" for k, v in sorted(self.params.items())) + ")"

    def warmup(self) -> int:
        return 0

    def decide(self, symbol: str, bars: list[Bar], held: bool) -> Signal | None:
        raise NotImplementedError

    def _stop(self, bars: list[Bar], k: float) -> float | None:
        a = atr(bars)
        if a is None:
            return None
        return round(bars[-1]["close"] - k * a, 4)


class BuyAndHold(Strategy):
    """Línea base: compra una vez con un stop muy lejano."""
    name = "comprar_mantener"
    PARAM_GRID = {"stop_pct": [0.4]}

    def decide(self, symbol, bars, held):
        if held or not bars:
            return None
        c = bars[-1]["close"]
        return Signal(symbol, "buy", round(c * (1 - self.params["stop_pct"]), 4), "línea base")


class Momentum(Strategy):
    """Compra si el retorno de `lookback` días es positivo y el precio está sobre su media; vende si se vuelve negativo."""
    name = "momentum"
    PARAM_GRID = {"lookback": [126, 63, 252], "atr_k": [3.0, 2.0, 4.0]}

    def warmup(self):
        return self.params["lookback"] + 21

    def decide(self, symbol, bars, held):
        n = self.params["lookback"]
        if len(bars) <= n + 20:
            return None
        closes = [b["close"] for b in bars]
        ret = closes[-1] / closes[-n - 1] - 1
        mean = sma(closes, n)
        f = {"ret": round(ret, 4), "sma": round(mean, 4)}
        if not held and ret > 0 and closes[-1] > mean:
            stop = self._stop(bars, self.params["atr_k"])
            return Signal(symbol, "buy", stop, f"momentum {n}d {ret:+.1%}", f) if stop else None
        if held and ret < 0:
            return Signal(symbol, "sell", 0.0, f"momentum {n}d negativo {ret:+.1%}", f)
        return None


class SmaCross(Strategy):
    """Cruce de medias: largo mientras la rápida está sobre la lenta."""
    name = "cruce_medias"
    PARAM_GRID = {"fast": [50, 20], "slow": [200, 100], "atr_k": [3.0, 4.0]}

    def warmup(self):
        return self.params["slow"] + 1

    def decide(self, symbol, bars, held):
        fast, slow = self.params["fast"], self.params["slow"]
        if fast >= slow or len(bars) < slow + 1:
            return None
        closes = [b["close"] for b in bars]
        f, s = sma(closes, fast), sma(closes, slow)
        feats = {"sma_rapida": round(f, 4), "sma_lenta": round(s, 4)}
        if not held and f > s:
            stop = self._stop(bars, self.params["atr_k"])
            return Signal(symbol, "buy", stop, f"SMA{fast} > SMA{slow}", feats) if stop else None
        if held and f < s:
            return Signal(symbol, "sell", 0.0, f"SMA{fast} < SMA{slow}", feats)
        return None


class MeanReversion(Strategy):
    """Compra caídas fuertes (z-score bajo) solo en tendencia alcista de largo plazo; vende al volver a la media."""
    name = "reversion"
    PARAM_GRID = {"window": [20, 10], "entry_z": [-2.0, -1.5], "trend": [200], "atr_k": [3.0]}

    def warmup(self):
        return max(self.params["window"], self.params["trend"]) + 21

    def decide(self, symbol, bars, held):
        w, t = self.params["window"], self.params["trend"]
        if len(bars) < max(w, t) + 21:
            return None
        closes = [b["close"] for b in bars]
        m, sd = fmean(closes[-w:]), pstdev(closes[-w:])
        if sd == 0 or math.isnan(sd):
            return None
        z = (closes[-1] - m) / sd
        feats = {"z": round(z, 3)}
        if not held and z < self.params["entry_z"] and closes[-1] > sma(closes, t) * 0.9:
            stop = self._stop(bars, self.params["atr_k"])
            return Signal(symbol, "buy", stop, f"z={z:.2f} bajo la media", feats) if stop else None
        if held and z > 0:
            return Signal(symbol, "sell", 0.0, f"z={z:.2f} volvió a la media", feats)
        return None


REGISTRY: dict[str, type[Strategy]] = {c.name: c for c in (BuyAndHold, Momentum, SmaCross, MeanReversion)}


def make(name: str, **params) -> Strategy:
    if name not in REGISTRY:
        raise ValueError(f"estrategia desconocida: {name}. Disponibles: {sorted(REGISTRY)}")
    return REGISTRY[name](**params)


def grid(name: str) -> list[dict]:
    """Todas las combinaciones de parámetros de la estrategia."""
    combos: list[dict] = [{}]
    for k, vals in REGISTRY[name].PARAM_GRID.items():
        combos = [c | {k: v} for c in combos for v in vals]
    return combos
