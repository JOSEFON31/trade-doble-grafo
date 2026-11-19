"""Carry de funding: comprar spot y vender el perpetuo (delta neutral).

No apuesta a la dirección. Gana la tasa de funding que pagan los largos del
perpetuo a los cortos cuando el mercado está apalancado al alza, y pierde costes,
funding negativo y la diferencia de precio entre spot y perpetuo.

Contabilidad diaria (cripto opera 365 días):
- Capital E repartido en spot (nocional N) y margen del corto (N / apalancamiento).
- El corto se marca a mercado cada día contra el margen y cobra o paga cada funding.
- Si el margen cae fuera de una banda, se rebalancea pagando comisiones.
- Si el máximo intradía del perpetuo hubiera liquidado el corto, se pierde el
  margen y se sale (se cuenta como evento de liquidación, no se esconde).

Sin datos del futuro:
- La decisión se toma al cierre del día D con el funding publicado hasta ese cierre.
- Se ejecuta a la apertura de D+1 y solo cobra los funding posteriores a la entrada.
- Walk-forward: los parámetros se eligen en la ventana de entrenamiento y se miden
  en la siguiente. Solo cuentan como fuera de muestra las ventanas desde `--desde-oos`.

Uso en tu PC (desde la carpeta del repo):
    python -m doblegrafo.carry --descargar --datos datos/carry --simbolos BTCUSDT ETHUSDT DOGEUSDT
    python -m doblegrafo.carry --datos datos/carry --simbolos BTCUSDT ETHUSDT DOGEUSDT --desde-oos 2024-01-01
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import sys
import time
import urllib.parse
import urllib.request
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from itertools import product
from pathlib import Path
from statistics import fmean, pstdev

from .backtest import expected_max_sharpe, probabilistic_sharpe
from .senales import SignalRegistry

DAYS = 365
GRID = {"ventana": [1, 3, 7, 14, 30], "entrada": [0.0, 0.05, 0.10, 0.15, 0.20, 0.30]}


# ============================================================ datos
@dataclass
class Market:
    symbol: str
    days: list[str]                       # días con spot y perpetuo
    spot: dict[str, dict]                 # día -> {open, high, low, close}
    perp: dict[str, dict]
    funding: list[tuple[datetime, float]]  # (momento del cobro, tasa), ordenado

    def __post_init__(self):
        self._times = [t for t, _ in self.funding]
        self._cum = [0.0]
        for _, r in self.funding:
            self._cum.append(self._cum[-1] + r)

    def funding_in_day(self, day: str) -> list[float]:
        """Funding cobrado mientras se mantiene la posición durante `day`: (apertura, apertura siguiente]."""
        a = _day_open(day)
        b = a + timedelta(days=1)
        i, j = bisect_right(self._times, a), bisect_right(self._times, b)
        return [r for _, r in self.funding[i:j]]

    def funding_annualized(self, day: str, window: int) -> float | None:
        """Funding anualizado de los últimos `window` días, conocido ANTES del cierre de `day`.

        Cobros en [inicio, cierre): la liquidación de las 00:00 UTC del día siguiente cae
        justo en el momento de decidir y se excluye, para no usar un dato aún no publicado.
        """
        end = _day_open(day) + timedelta(days=1)
        start = end - timedelta(days=window)
        i, j = bisect_left(self._times, start), bisect_left(self._times, end)
        if j <= i or (self.funding and self.funding[0][0] > start):
            return None
        return (self._cum[j] - self._cum[i]) * DAYS / window


def _day_open(day: str) -> datetime:
    return datetime.fromisoformat(day).replace(tzinfo=timezone.utc)


def _read_bars(path: Path) -> dict[str, dict]:
    with open(path, newline="") as f:
        return {r["day"]: {k: float(r[k]) for k in ("open", "high", "low", "close")} for r in csv.DictReader(f)}


def _read_funding(path: Path) -> list[tuple[datetime, float]]:
    with open(path, newline="") as f:
        rows = [(datetime.fromisoformat(r["time"].replace("Z", "+00:00")), float(r["rate"]))
                for r in csv.DictReader(f)]
    return sorted(rows)


def load_market(folder: Path, symbol: str) -> Market:
    spot = _read_bars(folder / f"spot_{symbol}.csv")
    perp = _read_bars(folder / f"perp_{symbol}.csv")
    funding = _read_funding(folder / f"funding_{symbol}.csv")
    days = sorted(set(spot) & set(perp))
    return Market(symbol, days, spot, perp, funding)


# ============================================================ simulación
@dataclass
class Costs:
    spot_fee: float = 0.0010      # comisión taker spot
    perp_fee: float = 0.0005      # comisión taker perpetuo
    slippage: float = 0.0002      # por pata
    leverage: float = 2.0         # apalancamiento del corto (el spot va sin apalancar)
    maintenance: float = 0.005    # margen de mantenimiento
    band: float = 0.30            # rebalancear si el margen se aleja ±30 % del objetivo

    def leg_cost(self, spot_notional: float, perp_notional: float) -> float:
        return (abs(spot_notional) * (self.spot_fee + self.slippage)
                + abs(perp_notional) * (self.perp_fee + self.slippage))


@dataclass
class CarryResult:
    symbol: str
    params: dict
    curve: list[tuple[str, float]]
    exposure: float
    trades: int
    liquidations: int
    funding_earned: float
    costs_paid: float
    daily: list[dict] = field(default_factory=list)  # por día: dentro, funding, costes, liquidación

    def window(self, start: str, end: str) -> dict:
        rows = [r for r in self.daily if start <= r["day"] <= end]
        return {"exposicion": sum(r["dentro"] for r in rows) / max(len(rows), 1),
                "liquidaciones": sum(r["liq"] for r in rows),
                "funding_cobrado": sum(r["funding"] for r in rows), "costes": sum(r["coste"] for r in rows)}

    def returns(self) -> list[float]:
        return [b / a - 1 for (_, a), (_, b) in zip(self.curve, self.curve[1:]) if a > 0]


def simulate(m: Market, params: dict | list[tuple[str, dict]], start: str, end: str, *,
             capital: float = 100.0, costs: Costs | None = None) -> CarryResult:
    """Simula la regla: entrar si el funding anualizado > entrada, salir si < entrada/2.

    `params = {"ventana": días, "entrada": umbral anual}`; `{"siempre": True}` = siempre dentro.
    También acepta un calendario `[(desde_día, params), ...]`: una sola cartera continua que
    cambia de parámetros en cada fecha, sin cerrar ni reabrir la posición en la frontera.
    """
    c = costs or Costs()
    days = [d for d in m.days if start <= d <= end]
    schedule = sorted(params, key=lambda x: x[0]) if isinstance(params, list) else [("", params)]
    k = 0
    cash, q_spot, q_perp, margin = capital, 0.0, 0.0, 0.0
    in_pos, want = False, False
    curve, exposure_days, trades, liqs = [], 0, 0, 0
    funding_total = cost_total = 0.0
    prev_perp_close = None
    daily: list[dict] = []

    for d in days:
        while k + 1 < len(schedule) and schedule[k + 1][0] <= d:
            k += 1
        cur = schedule[k][1]
        always = cur.get("siempre", False)
        window, enter = cur.get("ventana", 7), cur.get("entrada", 0.0)
        leave = enter / 2
        f_before, c_before, l_before, was_in = funding_total, cost_total, liqs, in_pos or want
        sp, pp = m.spot[d], m.perp[d]
        # 1) ejecutar a la apertura lo decidido ayer al cierre
        if want and not in_pos:
            equity = cash
            n = equity * c.leverage / (c.leverage + 1)
            fee = c.leg_cost(n, n)
            n = (equity - fee) * c.leverage / (c.leverage + 1)
            q_spot, q_perp = n / sp["open"], n / pp["open"]
            margin = equity - fee - n
            cash = 0.0
            cost_total += fee
            prev_perp_close = pp["open"]
            in_pos, trades = True, trades + 1
        elif not want and in_pos:
            spot_val = q_spot * sp["open"]
            margin += q_perp * (prev_perp_close - pp["open"])
            fee = c.leg_cost(spot_val, q_perp * pp["open"])
            cash = spot_val + margin - fee
            cost_total += fee
            q_spot = q_perp = margin = 0.0
            in_pos = False

        # 2) mantener durante el día: liquidación intradía, funding y marca a mercado
        if in_pos:
            exposure_days += 1
            worst = margin + q_perp * (prev_perp_close - pp["high"])
            if worst < c.maintenance * q_perp * pp["high"]:
                liqs += 1
                cash = q_spot * sp["close"] * (1 - c.spot_fee - c.slippage)  # el margen se pierde
                cost_total += q_spot * sp["close"] * (c.spot_fee + c.slippage)
                q_spot = q_perp = margin = 0.0
                in_pos = want = False
            else:
                f = sum(m.funding_in_day(d)) * q_perp * pp["close"]
                funding_total += f
                margin += q_perp * (prev_perp_close - pp["close"]) + f
                prev_perp_close = pp["close"]
                # rebalanceo si el margen sale de la banda o se descuadra la cobertura
                spot_val = q_spot * sp["close"]
                target_m = q_perp * pp["close"] / c.leverage
                if abs(margin / target_m - 1) > c.band or abs(spot_val / (q_perp * pp["close"]) - 1) > 0.05:
                    equity = spot_val + margin
                    n = equity * c.leverage / (c.leverage + 1)
                    fee = c.leg_cost(n - spot_val, n - q_perp * pp["close"])
                    equity -= fee
                    cost_total += fee
                    n = equity * c.leverage / (c.leverage + 1)
                    q_spot, q_perp, margin = n / sp["close"], n / pp["close"], equity - n

        equity = cash + (q_spot * sp["close"] + margin if in_pos else 0.0)
        curve.append((d, equity))
        daily.append({"day": d, "dentro": int(was_in and (in_pos or liqs > l_before)),
                      "funding": funding_total - f_before, "coste": cost_total - c_before, "liq": liqs - l_before})

        # 3) decidir al cierre con lo publicado hasta el cierre
        if always:
            want = True
        else:
            ann = m.funding_annualized(d, window)
            if ann is not None:
                want = ann > enter if not in_pos else ann > leave
    return CarryResult(m.symbol, schedule[-1][1], curve, exposure_days / max(len(days), 1), trades, liqs,
                       funding_total, cost_total, daily)


# ============================================================ métricas
def stats(curve: list[tuple[str, float]]) -> dict:
    rets = [b / a - 1 for (_, a), (_, b) in zip(curve, curve[1:]) if a > 0]
    if len(rets) < 2:
        return {"retorno_total": 0.0, "cagr": 0.0, "sharpe": 0.0, "caida_maxima": 0.0, "sr_p": 0.0,
                "n": len(rets), "skew": 0.0, "kurt": 3.0}
    total = curve[-1][1] / curve[0][1] - 1
    cagr = (1 + total) ** (DAYS / len(rets)) - 1 if total > -1 else -1.0
    sd = pstdev(rets)
    mu = fmean(rets)
    sr_p = mu / sd if sd > 0 else 0.0
    peak = mdd = 0.0
    for _, e in curve:
        peak = max(peak, e)
        mdd = max(mdd, 1 - e / peak if peak else 0.0)
    skew = fmean([((r - mu) / sd) ** 3 for r in rets]) if sd > 0 else 0.0
    kurt = fmean([((r - mu) / sd) ** 4 for r in rets]) if sd > 0 else 3.0
    return {"retorno_total": total, "cagr": cagr, "sharpe": sr_p * math.sqrt(DAYS), "caida_maxima": mdd,
            "sr_p": sr_p, "n": len(rets), "skew": skew, "kurt": kurt}


def _round(d: dict) -> dict:
    return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in d.items()
            if k not in ("sr_p", "n", "skew", "kurt")}


# ============================================================ walk-forward
def _add_months(day: str, months: int) -> str:
    d = date.fromisoformat(day)
    y, mo = divmod(d.month - 1 + months, 12)
    return date(d.year + y, mo + 1, min(d.day, 28)).isoformat()


def param_grid() -> list[dict]:
    return [dict(zip(GRID, v)) for v in product(*GRID.values())]


@dataclass
class WalkForward:
    symbol: str
    windows: list[dict] = field(default_factory=list)
    curve: list[tuple[str, float]] = field(default_factory=list)
    always_curve: list[tuple[str, float]] = field(default_factory=list)
    hodl_curve: list[tuple[str, float]] = field(default_factory=list)
    trial_srs: list[float] = field(default_factory=list)
    liquidations: int = 0


def walk_forward(m: Market, *, train_months: int = 24, test_months: int = 6, oos_from: str = "2024-01-01",
                 capital: float = 100.0, costs: Costs | None = None,
                 registry: SignalRegistry | None = None) -> WalkForward:
    """Elige parámetros en cada ventana de entrenamiento y corre UNA cartera continua fuera de muestra.

    Al registro van dos señales con referencias distintas:
    - `carry:<símbolo>`: la estrategia completa contra quedarse en efectivo (Sharpe 0).
    - `filtro_funding:<símbolo>`: el umbral de entrada contra estar siempre dentro del carry.
    """
    grid = param_grid()
    wf = WalkForward(m.symbol)
    first, last = m.days[0], m.days[-1]
    test_start = max(oos_from, _add_months(first, train_months))
    windows, schedule = [], []
    while test_start <= last:
        test_end = min(_add_months(test_start, test_months), last)
        train_start = _add_months(test_start, -train_months)
        train_end = (date.fromisoformat(test_start) - timedelta(days=1)).isoformat()
        scored = []
        for p in grid:
            s = stats(simulate(m, p, train_start, train_end, costs=costs).curve)
            scored.append((s["sharpe"], s["retorno_total"], p))
            wf.trial_srs.append(s["sr_p"])
        best = max(scored, key=lambda x: (x[0], x[1]))[2]
        windows.append((test_start, test_end, best))
        schedule.append((test_start, best))
        if test_end >= last:
            break
        test_start = (date.fromisoformat(test_end) + timedelta(days=1)).isoformat()
    if not windows:
        return wf
    oos_start, oos_end = windows[0][0], windows[-1][1]
    run = simulate(m, schedule, oos_start, oos_end, capital=capital, costs=costs)
    always = simulate(m, {"siempre": True}, oos_start, oos_end, capital=capital, costs=costs)
    wf.curve, wf.always_curve = run.curve, always.curve
    p0 = m.spot[run.curve[0][0]]["close"]
    wf.hodl_curve = [(d, capital * m.spot[d]["close"] / p0) for d, _ in run.curve]
    wf.liquidations = run.liquidations
    if registry:
        registry.register(f"carry:{m.symbol}", "Binance funding perpetuo", "carry",
                          "Spot largo + perpetuo corto con umbral de funding; referencia: efectivo", (m.symbol,))
        registry.register(f"filtro_funding:{m.symbol}", "Binance funding perpetuo", "direccion",
                          "Umbral de funding para entrar al carry; referencia: carry siempre dentro", (m.symbol,))

    def piece(curve, a, b):  # incluye el cierre anterior para medir el primer día de la ventana
        idx = [i for i, (d, _) in enumerate(curve) if a <= d <= b]
        return curve[max(idx[0] - 1, 0): idx[-1] + 1] if idx else []

    for a, b, best in windows:
        s_t, s_a = stats(piece(run.curve, a, b)), stats(piece(always.curve, a, b))
        extra = run.window(a, b)
        wf.windows.append({"prueba": f"{a} a {b}", "params": best,
                           "exposicion": round(extra["exposicion"], 2), "liquidaciones": extra["liquidaciones"],
                           "funding_cobrado": round(extra["funding_cobrado"], 2), "costes": round(extra["costes"], 2),
                           **_round(s_t), "siempre_dentro_sharpe": round(s_a["sharpe"], 3)})
        if registry and s_t["n"] > 2:
            common = dict(total_return=s_t["retorno_total"], max_drawdown=s_t["caida_maxima"], params=best)
            registry.evaluate(f"carry:{m.symbol}", a, b, sharpe=s_t["sharpe"], baseline_sharpe=0.0, **common)
            registry.evaluate(f"filtro_funding:{m.symbol}", a, b, sharpe=s_t["sharpe"],
                              baseline_sharpe=s_a["sharpe"], **common)
    return wf


def report(wf: WalkForward, n_trials: int) -> dict:
    s = stats(wf.curve)
    thr = expected_max_sharpe(wf.trial_srs[-n_trials:]) if wf.trial_srs else 0.0
    dsr = probabilistic_sharpe(s["sr_p"], thr, s["n"], s["skew"], s["kurt"])
    return {"simbolo": wf.symbol, "fuera_de_muestra": _round(s),
            "sharpe_deflactado_prob": round(dsr, 3), "liquidaciones": wf.liquidations,
            "siempre_dentro": _round(stats(wf.always_curve)), "comprar_y_mantener": _round(stats(wf.hodl_curve)),
            "ventanas": wf.windows}


# ============================================================ descarga (corre en tu PC)
def _get(url: str, params: dict) -> list:
    q = url + "?" + urllib.parse.urlencode(params)
    for intento in range(5):
        try:
            with urllib.request.urlopen(urllib.request.Request(q, headers={"User-Agent": "doblegrafo"}),
                                        timeout=30) as r:
                return json.loads(r.read())
        except Exception as e:  # noqa: BLE001
            if intento == 4:
                raise RuntimeError(f"no se pudo descargar {q}: {e}") from e
            time.sleep(2 ** intento)
    return []


def _klines(url: str, symbol: str, since_ms: int) -> list[dict]:
    out, start = [], since_ms
    while True:
        rows = _get(url, {"symbol": symbol, "interval": "1d", "startTime": start, "limit": 1000})
        if not rows:
            break
        for k in rows:
            day = datetime.fromtimestamp(k[0] / 1000, timezone.utc).date().isoformat()
            out.append({"day": day, "open": k[1], "high": k[2], "low": k[3], "close": k[4]})
        start = rows[-1][0] + 86_400_000
        if len(rows) < 1000:
            break
        time.sleep(0.2)
    today = datetime.now(timezone.utc).date().isoformat()
    return [r for r in out if r["day"] < today]  # sin la vela del día en curso


def download(folder: Path, symbols: list[str], since: str = "2019-09-01") -> None:
    folder.mkdir(parents=True, exist_ok=True)
    since_ms = int(_day_open(since).timestamp() * 1000)
    for sym in symbols:
        print(f"descargando {sym}...", flush=True)
        spot = _klines("https://api.binance.com/api/v3/klines", sym, since_ms)
        perp = _klines("https://fapi.binance.com/fapi/v1/klines", sym, since_ms)
        fund, start = [], since_ms
        while True:
            rows = _get("https://fapi.binance.com/fapi/v1/fundingRate",
                        {"symbol": sym, "startTime": start, "limit": 1000})
            if not rows:
                break
            fund += rows
            start = rows[-1]["fundingTime"] + 1
            if len(rows) < 1000:
                break
            time.sleep(0.2)
        for name, rows, cols in (("spot", spot, ["day", "open", "high", "low", "close"]),
                                 ("perp", perp, ["day", "open", "high", "low", "close"])):
            with open(folder / f"{name}_{sym}.csv", "w", newline="") as f:
                w = csv.DictWriter(f, cols)
                w.writeheader()
                w.writerows(rows)
        with open(folder / f"funding_{sym}.csv", "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["time", "rate"])
            for r in fund:
                t = datetime.fromtimestamp(r["fundingTime"] / 1000, timezone.utc).isoformat()
                w.writerow([t, r["fundingRate"]])
        print(f"  spot {len(spot)} días, perpetuo {len(perp)} días, funding {len(fund)} cobros", flush=True)


# ============================================================ CLI
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Carry de funding (spot largo + perpetuo corto) con walk-forward")
    ap.add_argument("--datos", default="datos/carry", help="carpeta con spot_/perp_/funding_<SIMBOLO>.csv")
    ap.add_argument("--simbolos", nargs="+", default=["BTCUSDT", "ETHUSDT"])
    ap.add_argument("--descargar", action="store_true", help="bajar datos de Binance antes de simular")
    ap.add_argument("--desde-oos", default="2024-01-01")
    ap.add_argument("--entrenar-meses", type=int, default=24)
    ap.add_argument("--probar-meses", type=int, default=6)
    ap.add_argument("--apalancamiento", type=float, default=2.0)
    ap.add_argument("--registro", default=None, help="SQLite del registro de señales (por defecto ~/.trader/senales.db)")
    ap.add_argument("--json", default=None, help="guardar el informe completo en este archivo")
    a = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):  # la consola de Windows (cp1252) no imprime tildes raras
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    folder = Path(a.datos)
    if a.descargar:
        download(folder, a.simbolos)
    reg_path = Path(a.registro) if a.registro else Path.home() / ".trader" / "senales.db"
    reg_path.parent.mkdir(parents=True, exist_ok=True)
    reg = SignalRegistry(reg_path)
    costs = Costs(leverage=a.apalancamiento)
    out = []
    for sym in a.simbolos:
        m = load_market(folder, sym)
        wf = walk_forward(m, train_months=a.entrenar_meses, test_months=a.probar_meses, oos_from=a.desde_oos,
                          costs=costs, registry=reg)
        r = report(wf, len(param_grid()))
        out.append(r)
        f, al, h = r["fuera_de_muestra"], r["siempre_dentro"], r["comprar_y_mantener"]
        print(f"\n== {sym}  fuera de muestra desde {a.desde_oos} (100 USD) ==")
        print(f"  carry con señal : {100 * (1 + f['retorno_total']):8.1f}  CAGR {f['cagr']:+.1%}  "
              f"Sharpe {f['sharpe']:.2f}  caída máx {f['caida_maxima']:.1%}  liquidaciones {r['liquidaciones']}")
        print(f"  siempre dentro  : {100 * (1 + al['retorno_total']):8.1f}  CAGR {al['cagr']:+.1%}  "
              f"Sharpe {al['sharpe']:.2f}  caída máx {al['caida_maxima']:.1%}")
        print(f"  comprar y mant. : {100 * (1 + h['retorno_total']):8.1f}  CAGR {h['cagr']:+.1%}  "
              f"Sharpe {h['sharpe']:.2f}  caída máx {h['caida_maxima']:.1%}")
        print(f"  Sharpe deflactado (prob. de que sea real): {r['sharpe_deflactado_prob']:.0%}")
        for w in r["ventanas"]:
            print(f"    {w['prueba']}  {w['params']}  ret {w['retorno_total']:+.1%}  expo {w['exposicion']:.0%}  "
                  f"funding {w['funding_cobrado']:+.2f}  costes {w['costes']:.2f}")
    print("\nRegistro de señales:")
    for row in reg.ranking():
        print(f"  {row['señal']:<28} {row['estado']:<8} crédito {row['credito']:+.2f}  ventanas {row['ventanas']}")
    if a.json:
        Path(a.json).write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
