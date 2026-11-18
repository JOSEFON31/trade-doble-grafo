"""CLI del simulador (`trader-backtest`) y utilidades compartidas con el servidor MCP.

Ejemplos:
    trader-backtest --descargar SPY QQQ IWM --estrategia todas --modo walk-forward
    trader-backtest --csv SPY=spy.csv --estrategia momentum --params '{"lookback": 63}'
    trader-backtest --sintetico --estrategia cruce_medias --modo walk-forward
    trader-backtest --estrategia momentum --modo walk-forward --usar-holdout   # UNA sola vez, al final
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path

from .backtest import run_backtest, synthetic_prices, walk_forward
from .graph import DoubleGraph
from .paper import PaperBroker, fetch_stooq, parse_daily_csv
from .risk import RiskLimits
from .strategies import REGISTRY, make

TRIALS_SCHEMA = """
CREATE TABLE IF NOT EXISTS backtest_trials (
    id INTEGER PRIMARY KEY, strategy TEXT NOT NULL, params TEXT NOT NULL, start TEXT NOT NULL,
    end TEXT NOT NULL, sharpe REAL, cagr REAL, max_drawdown REAL, origin TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
"""


def db_path() -> Path:
    p = Path(os.environ.get("TRADER_DB", Path.home() / ".trader" / "trader.db")).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def load_prices(g: DoubleGraph, symbols: list[str] | None = None) -> dict[str, list[dict]]:
    PaperBroker(g)  # asegura las tablas
    q = "SELECT * FROM prices"
    args: list = []
    if symbols:
        q += " WHERE symbol IN (%s)" % ",".join("?" * len(symbols))
        args = [s.upper() for s in symbols]
    out: dict[str, list[dict]] = {}
    for r in g.db.execute(q + " ORDER BY symbol, day", args):
        out.setdefault(r["symbol"], []).append(
            {"day": r["day"], "open": r["open"], "high": r["high"], "low": r["low"], "close": r["close"],
             "volume": r["volume"]})
    return out


def holdout_start(prices: dict[str, list[dict]], months: int = 12) -> str:
    """Último día que se puede usar fuera del holdout (el corte en sí ya queda reservado)."""
    last = max(b["day"] for bars in prices.values() for b in bars)
    return (date.fromisoformat(last) - timedelta(days=round(30.44 * months) + 1)).isoformat()


def record_trial(g: DoubleGraph, strategy: str, params: dict, start: str, end: str, m, origin: str) -> int:
    g.db.executescript(TRIALS_SCHEMA)
    g.db.execute("INSERT INTO backtest_trials(strategy,params,start,end,sharpe,cagr,max_drawdown,origin)"
                 " VALUES (?,?,?,?,?,?,?,?)",
                 (strategy, json.dumps(params, sort_keys=True), start, end, m.sharpe, m.cagr, m.max_drawdown, origin))
    return g.db.execute("SELECT COUNT(*) FROM backtest_trials WHERE strategy=?", (strategy,)).fetchone()[0]


def prior_sharpes(g: DoubleGraph, strategy: str) -> list[float]:
    """Sharpe anualizado de todas las pruebas anteriores registradas para la estrategia."""
    g.db.executescript(TRIALS_SCHEMA)
    return [r[0] for r in g.db.execute("SELECT sharpe FROM backtest_trials WHERE strategy=? AND sharpe IS NOT NULL",
                                       (strategy,))]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Simulador con datos históricos (backtest y walk-forward)")
    ap.add_argument("--estrategia", default="todas", help=f"una de {sorted(REGISTRY)} o 'todas'")
    ap.add_argument("--modo", choices=["simple", "walk-forward"], default="walk-forward")
    ap.add_argument("--params", default="{}", help="JSON con parámetros (solo modo simple)")
    ap.add_argument("--simbolos", nargs="*", help="limitar a estos símbolos (por defecto todos los guardados)")
    ap.add_argument("--descargar", nargs="*", default=[], metavar="SIMBOLO",
                    help="descarga históricos de Stooq y los guarda en tu base de datos")
    ap.add_argument("--csv", action="append", default=[], metavar="SIMBOLO=archivo.csv")
    ap.add_argument("--sintetico", action="store_true", help="usa datos inventados (solo para probar que todo corre)")
    ap.add_argument("--desde")
    ap.add_argument("--hasta")
    ap.add_argument("--capital", type=float, default=10_000)
    ap.add_argument("--entrena-anios", type=float, default=3)
    ap.add_argument("--prueba-anios", type=float, default=1)
    ap.add_argument("--holdout-meses", type=int, default=12)
    ap.add_argument("--usar-holdout", action="store_true", help="incluye el tramo reservado (úsalo una sola vez)")
    ap.add_argument("--json", help="guarda el resultado completo en este archivo")
    args = ap.parse_args(argv)

    g = DoubleGraph(db_path())
    try:
        broker = PaperBroker(g)
        for sym in args.descargar:
            try:
                n = broker.store_prices(sym, fetch_stooq(sym), "stooq")
                print(f"{sym.upper()}: {n} barras nuevas")
            except Exception as exc:
                print(f"{sym}: no se pudo descargar ({exc})", file=sys.stderr)
        for spec in args.csv:
            sym, path = spec.split("=", 1)
            broker.store_prices(sym, parse_daily_csv(Path(path).read_text(encoding="utf-8")), f"csv:{path}")

        if args.sintetico:
            prices = {"SINT_A": synthetic_prices("SINT_A", seed=1), "SINT_B": synthetic_prices("SINT_B", seed=2, drift=0.04)}
            print("Aviso: datos sintéticos, los resultados no dicen nada del mercado real.")
        else:
            prices = load_prices(g, args.simbolos)
        if not prices:
            print("No hay precios. Usa --descargar SPY QQQ ... o --csv.", file=sys.stderr)
            return 2
        types = {r["symbol"]: r["type"] for r in g.db.execute("SELECT symbol, type FROM instruments")}
        names = sorted(REGISTRY) if args.estrategia == "todas" else [args.estrategia]
        limits = RiskLimits.load()
        out = []
        for name in names:
            if args.modo == "simple":
                params = json.loads(args.params)
                ho = None if args.usar_holdout else holdout_start(prices, args.holdout_meses)
                start = args.desde or min(b["day"] for bars in prices.values() for b in bars)
                end = min(x for x in [args.hasta, ho] if x) if (args.hasta or ho) else "9999"
                r = run_backtest(prices, make(name, **params), start, end, capital=args.capital, limits=limits,
                                 instrument_types=types)
                n = record_trial(g, name, params, r.start, r.end, r.metrics, "cli") if not args.sintetico else 0
                d = r.as_dict() | {"pruebas_acumuladas": n}
            else:
                prev = [] if args.sintetico else prior_sharpes(g, name)
                r = walk_forward(prices, name, start=args.desde, end=args.hasta, train_years=args.entrena_anios,
                                 test_years=args.prueba_anios, holdout_months=args.holdout_meses,
                                 use_holdout=args.usar_holdout, capital=args.capital, limits=limits,
                                 prior_trial_sharpes=prev)
                d = r.as_dict()
            out.append(d)
            _print_summary(d)
        if args.json:
            Path(args.json).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"Resultado completo en {args.json}")
        return 0
    finally:
        g.close()


def _print_summary(d: dict) -> None:
    m = d.get("fuera_de_muestra") or d.get("metricas")
    b = d["comprar_y_mantener"]
    print(f"\n== {d['estrategia']} ==")
    print(f"  CAGR {m['cagr']:+.1%} | Sharpe {m['sharpe']:.2f} | caída máx {m['caida_maxima']:.1%} | "
          f"operaciones {m['operaciones']} | invertido en promedio {m.get('exposicion_media', 0):.0%}")
    print(f"  Comprar y mantener: CAGR {b['cagr']:+.1%} | Sharpe {b['sharpe']:.2f} | caída máx {b['caida_maxima']:.1%}")
    if "prob_sharpe_real_positivo_deflactado" in d:
        p = d["prob_sharpe_real_positivo_deflactado"]
        print(f"  Probabilidad de que el Sharpe sea real tras {d['pruebas_realizadas']} pruebas: {p:.0%}"
              + ("  (no fiable: < 95 %)" if p < 0.95 else ""))
        if d.get("holdout_reservado_desde"):
            print(f"  Datos desde {d['holdout_reservado_desde']} reservados (holdout), no usados.")


if __name__ == "__main__":
    sys.exit(main())
