"""Simulador con datos históricos: backtest y walk-forward.

Usa EXACTAMENTE las mismas piezas que el paper trading en vivo: doble grafo,
gestor de riesgo y broker de paper (llenado a la apertura siguiente, costes,
stops). Corre en una base de datos en memoria, así que nunca toca tu cartera real
de paper, y no llama a Claude: las estrategias son reglas deterministas.

Garantías contra datos del futuro:
- La estrategia solo recibe barras hasta el cierre del día de decisión.
- Las órdenes decididas al cierre de D se llenan a la apertura de D+1.
- En walk-forward los parámetros se eligen en la ventana de entrenamiento y se
  evalúan en la ventana siguiente, que la elección nunca vio.
- Un tramo final (holdout) queda fuera salvo que se pida explícitamente.
"""
from __future__ import annotations

import math
from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import date, timedelta
from statistics import NormalDist, fmean, pstdev

from .graph import DoubleGraph
from .paper import CostModel, PaperBroker
from .risk import RiskLimits
from .strategies import Strategy, grid, make

TRADING_DAYS = 252
_N = NormalDist()


# ================================================================ métricas
@dataclass
class Metrics:
    days: int
    total_return: float
    cagr: float
    sharpe: float
    max_drawdown: float
    trades: int
    win_rate: float | None
    sharpe_period: float = 0.0
    skew: float = 0.0
    kurtosis: float = 3.0

    def as_dict(self) -> dict:
        return {"dias": self.days, "retorno_total": round(self.total_return, 4), "cagr": round(self.cagr, 4),
                "sharpe": round(self.sharpe, 3), "caida_maxima": round(self.max_drawdown, 4),
                "operaciones": self.trades,
                "acierto": None if self.win_rate is None else round(self.win_rate, 3)}


def returns_of(curve: list[tuple[str, float]]) -> list[float]:
    return [b / a - 1 for (_, a), (_, b) in zip(curve, curve[1:]) if a > 0]


def metrics(curve: list[tuple[str, float]], pnls: list[float] | None = None) -> Metrics:
    pnls = pnls or []
    if len(curve) < 2:
        return Metrics(len(curve), 0.0, 0.0, 0.0, 0.0, len(pnls), None)
    rets = returns_of(curve)
    total = curve[-1][1] / curve[0][1] - 1
    years = max(len(rets), 1) / TRADING_DAYS
    cagr = (1 + total) ** (1 / years) - 1 if total > -1 else -1.0
    sd = pstdev(rets) if len(rets) > 1 else 0.0
    sr_p = fmean(rets) / sd if sd > 0 else 0.0
    peak, mdd = curve[0][1], 0.0
    for _, e in curve:
        peak = max(peak, e)
        mdd = max(mdd, 1 - e / peak)
    skew = kurt = 0.0
    if sd > 0:
        m = fmean(rets)
        skew = fmean([((r - m) / sd) ** 3 for r in rets])
        kurt = fmean([((r - m) / sd) ** 4 for r in rets])
    return Metrics(len(curve), total, cagr, sr_p * math.sqrt(TRADING_DAYS), mdd, len(pnls),
                   (sum(p > 0 for p in pnls) / len(pnls)) if pnls else None, sr_p, skew, kurt or 3.0)


def probabilistic_sharpe(sr_p: float, sr_ref_p: float, n: int, skew: float, kurt: float) -> float:
    """Probabilidad de que el Sharpe real supere `sr_ref_p` (Bailey y López de Prado). Por periodo."""
    if n < 3:
        return 0.0
    denom = 1 - skew * sr_p + (kurt - 1) / 4 * sr_p ** 2
    if denom <= 0:
        return 0.0
    return _N.cdf((sr_p - sr_ref_p) * math.sqrt(n - 1) / math.sqrt(denom))


def expected_max_sharpe(trial_srs_p: list[float]) -> float:
    """Sharpe máximo esperado por azar tras N pruebas (umbral del Sharpe deflactado). Por periodo."""
    n = len(trial_srs_p)
    if n < 2:
        return 0.0
    v = pstdev(trial_srs_p) ** 2
    g = 0.5772156649
    return math.sqrt(v) * ((1 - g) * _N.inv_cdf(1 - 1 / n) + g * _N.inv_cdf(1 - 1 / (n * math.e)))


# =============================================================== backtest
@dataclass
class BacktestResult:
    strategy: str
    start: str
    end: str
    curve: list[tuple[str, float]]
    metrics: Metrics
    benchmark: Metrics
    credit: dict[str, float] = field(default_factory=dict)
    vetoes: int = 0
    pnls: list[float] = field(default_factory=list)
    avg_exposure: float = 0.0

    def as_dict(self) -> dict:
        return {"estrategia": self.strategy, "desde": self.start, "hasta": self.end,
                "metricas": self.metrics.as_dict() | {"exposicion_media": round(self.avg_exposure, 3)},
                "comprar_y_mantener": self.benchmark.as_dict(),
                "vetos_de_riesgo": self.vetoes, "credito": self.credit}


Prices = dict[str, list[dict]]  # símbolo -> barras ordenadas por día


def _benchmark(prices: Prices, symbol: str, days: list[str]) -> Metrics:
    bars = {b["day"]: b["close"] for b in prices[symbol]}
    curve = [(d, bars[d]) for d in days if d in bars]
    return metrics(curve)


def run_backtest(prices: Prices, strategy: Strategy, start: str, end: str, *, capital: float = 10_000,
                 limits: RiskLimits | None = None, costs: CostModel | None = None,
                 instrument_types: dict[str, str] | None = None, record_graph: bool = True) -> BacktestResult:
    """Simula la estrategia día a día entre `start` y `end` (incluidos)."""
    g = DoubleGraph()
    broker = PaperBroker(g, limits or RiskLimits(), costs or CostModel())
    types = instrument_types or {}
    for sym, bars in prices.items():
        g.upsert_instrument(types.get(sym, "etf"), sym, tradable=True)
        broker.store_prices(sym, bars, "backtest")
    broker.open_account(capital)
    inst_id = {r["symbol"]: r["id"] for r in g.db.execute("SELECT id, symbol FROM instruments")}
    day_index = {s: [b["day"] for b in bars] for s, bars in prices.items()}
    days = sorted({b["day"] for bars in prices.values() for b in bars if start <= b["day"] <= end})
    owner = strategy.label

    eid = goal = None
    month = None
    for d in days:
        rep = broker.open_day(d)
        if eid is None or (record_graph and d[:7] != month):
            month = d[:7]
            eid, goal = g.create_exploration(f"backtest {owner} {month}")
        held = {r["symbol"] for r in g.db.execute("SELECT DISTINCT symbol FROM lots")}
        held |= {r["symbol"] for r in g.db.execute("SELECT symbol FROM orders WHERE status='pending' AND side='buy'")}
        for sym, bars in prices.items():
            i = bisect_right(day_index[sym], d)
            if i == 0 or day_index[sym][i - 1] != d:
                continue  # sin barra hoy: no se decide sobre datos viejos
            sig = strategy.decide(sym, bars[:i], sym in held)
            if sig is None:
                continue
            payload = {"symbol": sym, "side": sig.side, "qty": 1e9, "stop": sig.stop or 1e-9,
                       "reason": sig.reason}
            at = f"{d}T21:00:00Z"
            it = g.add_node(eid, "intent", {"capability": "analyze", "symbol": sym}, anchors=[inst_id[sym]],
                            parents=[("spawns", goal)])
            g.set_state(it, "running")
            fact = g.add_node(eid, "fact", {"fuente": "backtest", "close": bars[i - 1]["close"], **sig.features},
                              parents=[("yields", it)], anchors=[inst_id[sym]], owner=owner, available_at=at)
            find = g.add_node(eid, "finding", {"reason": sig.reason}, parents=[("yields", it), ("derived_from", fact)],
                              anchors=[inst_id[sym]], owner=owner, available_at=at)
            g.add_node(eid, "proposal", payload, parents=[("yields", it), ("derived_from", find)],
                       anchors=[inst_id[sym]], owner=owner, available_at=at)
            g.set_state(it, "done")
        broker.close_day(d, rep)

    rows = g.db.execute("SELECT day, equity, cash FROM equity_curve ORDER BY day").fetchall()
    curve = [(r["day"], r["equity"]) for r in rows]
    exposure = fmean([1 - r["cash"] / r["equity"] for r in rows if r["equity"] > 0]) if rows else 0.0
    pnls = [n.payload["pnl_net"] for r in g.db.execute("SELECT id FROM nodes WHERE kind='outcome'")
            for n in [g.node(r["id"])]]
    vetoes = g.db.execute("SELECT COUNT(*) FROM risk_decisions WHERE verdict='vetoed'").fetchone()[0]
    first = next(iter(prices))
    res = BacktestResult(owner, days[0] if days else start, days[-1] if days else end, curve,
                         metrics(curve, pnls), _benchmark(prices, first, days), g.credit_by_owner(), vetoes,
                         pnls, exposure)
    g.close()
    return res


def synthetic_prices(symbol: str = "SINT", start: str = "2010-01-04", days: int = 2520, drift: float = 0.08,
                     vol: float = 0.18, seed: int = 7) -> list[dict]:
    """Serie diaria sintética (paseo aleatorio geométrico) para probar sin internet. No es el mercado real."""
    import random
    rng = random.Random(seed)
    mu, sd = drift / TRADING_DAYS, vol / math.sqrt(TRADING_DAYS)
    d, px, out = date.fromisoformat(start), 100.0, []
    while len(out) < days:
        if d.weekday() < 5:
            o = px * (1 + rng.gauss(0, sd / 4))
            c = o * (1 + rng.gauss(mu, sd))
            hi = max(o, c) * (1 + abs(rng.gauss(0, sd / 3)))
            lo = min(o, c) * (1 - abs(rng.gauss(0, sd / 3)))
            out.append({"day": d.isoformat(), "open": round(o, 4), "high": round(hi, 4), "low": round(lo, 4),
                        "close": round(c, 4), "volume": 1e6})
            px = c
        d += timedelta(days=1)
    return out


# ============================================================ walk-forward
def _add_years(day: str, years: float) -> str:
    d = date.fromisoformat(day)
    return (d + timedelta(days=round(365.25 * years))).isoformat()


@dataclass
class WalkForwardResult:
    strategy: str
    folds: list[dict]
    oos_curve: list[tuple[str, float]]
    oos: Metrics
    benchmark: Metrics
    trials: int
    deflated_sharpe_prob: float
    holdout_from: str | None
    avg_exposure: float = 0.0

    def as_dict(self) -> dict:
        return {"estrategia": self.strategy, "ventanas": self.folds,
                "fuera_de_muestra": self.oos.as_dict() | {"exposicion_media": round(self.avg_exposure, 3)}, "comprar_y_mantener": self.benchmark.as_dict(),
                "pruebas_realizadas": self.trials,
                "prob_sharpe_real_positivo_deflactado": round(self.deflated_sharpe_prob, 3),
                "holdout_reservado_desde": self.holdout_from}


class _Scheduled(Strategy):
    """Cambia de parámetros en cada frontera de ventana sin cerrar la cartera (walk-forward continuo)."""
    name = "walk_forward"

    def __init__(self, base: str, schedule: list[tuple[str, Strategy]]):
        self.params = {}
        self.base = base
        self.schedule = sorted(schedule, key=lambda x: x[0])

    @property
    def label(self) -> str:
        return f"walk_forward({self.base})"

    def decide(self, symbol, bars, held):
        day = bars[-1]["day"]
        active = None
        for d0, strat in self.schedule:
            if d0 <= day:
                active = strat
        return active.decide(symbol, bars, held) if active else None


def walk_forward(prices: Prices, strategy_name: str, *, start: str | None = None, end: str | None = None,
                 train_years: float = 3, test_years: float = 1, holdout_months: int = 12,
                 use_holdout: bool = False, capital: float = 10_000, limits: RiskLimits | None = None,
                 costs: CostModel | None = None, param_grid: list[dict] | None = None,
                 prior_trial_sharpes: list[float] | None = None) -> WalkForwardResult:
    """Elige parámetros en cada ventana de entrenamiento y los aplica en la siguiente.

    La parte fuera de muestra se simula como UNA sola cartera continua que cambia de
    parámetros en cada frontera: posiciones y órdenes pendientes pasan de una ventana a otra.
    `prior_trial_sharpes`: Sharpe anualizado de pruebas anteriores (CLI o Claude); cuentan para
    el número de pruebas y para el umbral del Sharpe deflactado.
    """
    all_days = sorted({b["day"] for bars in prices.values() for b in bars})
    start = start or all_days[0]
    end = end or all_days[-1]
    holdout_from = None
    if not use_holdout and holdout_months > 0:
        hf = date.fromisoformat(end) - timedelta(days=round(30.44 * holdout_months))
        holdout_from = hf.isoformat()
        end = (hf - timedelta(days=1)).isoformat()  # el día de corte ya es holdout
    combos = param_grid or grid(strategy_name)
    folds: list[dict] = []
    trial_srs: list[float] = []
    schedule: list[tuple[str, Strategy]] = []
    t0 = start
    while True:
        train_end = _add_years(t0, train_years)
        test_end = _add_years(train_end, test_years)
        if train_end >= end:
            break
        test_end = min(test_end, end)
        best, best_res = None, None
        for params in combos:
            r = run_backtest(prices, make(strategy_name, **params), t0, train_end, capital=capital,
                             limits=limits, costs=costs, record_graph=False)
            trial_srs.append(r.metrics.sharpe_period)
            if best_res is None or (r.metrics.sharpe, r.metrics.cagr) > (best_res.metrics.sharpe, best_res.metrics.cagr):
                best, best_res = params, r
        test_start = _add_years(train_end, 1 / 365.25)
        schedule.append((test_start, make(strategy_name, **best)))
        folds.append({"entrena": [t0, train_end], "prueba": [test_start, test_end], "parametros": best,
                      "sharpe_entrenamiento": round(best_res.metrics.sharpe, 3)})
        if test_end >= end:
            break
        t0 = _add_years(t0, test_years)

    oos_curve: list[tuple[str, float]] = []
    oos_pnls: list[float] = []
    exposure = 0.0
    if schedule:
        run = run_backtest(prices, _Scheduled(strategy_name, schedule), schedule[0][0], folds[-1]["prueba"][1],
                           capital=capital, limits=limits, costs=costs)
        oos_curve, oos_pnls, exposure = run.curve, run.pnls, run.avg_exposure
        for f in folds:
            part = [(d, e) for d, e in oos_curve if f["prueba"][0] <= d <= f["prueba"][1]]
            m = metrics(part)
            f["prueba"] = [part[0][0], part[-1][0]] if part else f["prueba"]
            f.update(sharpe_prueba=round(m.sharpe, 3), cagr_prueba=round(m.cagr, 4))
    oos = metrics(oos_curve, oos_pnls)
    prior = [x / math.sqrt(TRADING_DAYS) for x in (prior_trial_sharpes or []) if x is not None]
    all_srs = trial_srs + prior
    n_trials = len(all_srs)
    sr_star = expected_max_sharpe(all_srs) if n_trials > 1 else 0.0
    dsr = probabilistic_sharpe(oos.sharpe_period, sr_star, max(len(oos_curve) - 1, 0), oos.skew, oos.kurtosis)
    first = next(iter(prices))
    bench = _benchmark(prices, first, [d for d, _ in oos_curve])
    return WalkForwardResult(strategy_name, folds, oos_curve, oos, bench, n_trials, dsr, holdout_from, exposure)
