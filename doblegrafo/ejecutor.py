"""Ejecutor diario de paper trading (lo corres tú o el Programador de tareas, no Claude).

Uso:
    trader-ejecutor --capital 10000            # primera vez: abre la cuenta de paper
    trader-ejecutor                            # cada día tras el cierre: baja precios y procesa
    trader-ejecutor --csv SPY=spy.csv          # precios desde archivo en vez de Stooq
    trader-ejecutor --reset-kill-switch        # solo tras revisar qué pasó
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .graph import DoubleGraph
from .paper import CostModel, PaperBroker, fetch_stooq, parse_daily_csv
from .risk import RiskLimits


def _db_path() -> Path:
    p = Path(os.environ.get("TRADER_DB", Path.home() / ".trader" / "trader.db")).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def symbols_to_price(b: PaperBroker) -> list[str]:
    rows = b.db.execute(
        "SELECT symbol FROM instruments WHERE tradable=1 UNION SELECT symbol FROM lots"
        " UNION SELECT symbol FROM orders WHERE status='pending' ORDER BY 1").fetchall()
    return [r[0] for r in rows]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Ejecutor diario de paper trading del doble grafo")
    ap.add_argument("--capital", type=float, help="abre la cuenta de paper con este capital (solo la primera vez)")
    ap.add_argument("--csv", action="append", default=[], metavar="SIMBOLO=archivo.csv",
                    help="carga precios diarios desde CSV (Date,Open,High,Low,Close,Volume)")
    ap.add_argument("--sin-descarga", action="store_true", help="no descargar precios de Stooq")
    ap.add_argument("--desde", help="no procesar días anteriores a esta fecha (YYYY-MM-DD) en la primera corrida")
    ap.add_argument("--reset-kill-switch", action="store_true")
    ap.add_argument("--slippage-bps", type=float, default=5.0)
    ap.add_argument("--comision-por-accion", type=float, default=0.0)
    args = ap.parse_args(argv)

    g = DoubleGraph(_db_path())
    try:
        return _run(g, args)
    finally:
        g.close()


def _run(g: DoubleGraph, args) -> int:
    broker = PaperBroker(g, RiskLimits.load(),
                         CostModel(slippage_bps=args.slippage_bps, commission_per_share=args.comision_por_accion))
    if args.capital:
        broker.open_account(args.capital)
    try:
        acct = broker.account()
    except Exception as exc:
        print(f"Error: {exc}. Usa --capital la primera vez.", file=sys.stderr)
        return 2
    if args.reset_kill_switch:
        broker.reset_kill_switch()
        print("Kill switch desactivado.")

    for spec in args.csv:
        sym, path = spec.split("=", 1)
        n = broker.store_prices(sym, parse_daily_csv(Path(path).read_text(encoding="utf-8")), f"csv:{path}")
        print(f"{sym.upper()}: {n} barras nuevas desde {path}")
    if not args.sin_descarga:
        for sym in symbols_to_price(broker):
            try:
                n = broker.store_prices(sym, fetch_stooq(sym), "stooq")
                print(f"{sym}: {n} barras nuevas de Stooq")
            except Exception as exc:  # una fuente caída no debe tumbar el resto
                print(f"{sym}: no se pudieron bajar precios ({exc})", file=sys.stderr)

    last = acct["last_day"]
    days = broker.trading_days(last)
    if last is None:
        # primera corrida: solo el último día disponible (o desde --desde), nunca rehacer años de historia
        days = [d for d in days if d >= args.desde] if args.desde else days[-1:]
    if not days:
        print("No hay días nuevos que procesar.")
    for d in days:
        rep = broker.run_day(d)
        print(json.dumps(rep, ensure_ascii=False))
    st = broker.state(days[-1] if days else (last or ""))
    print(f"Capital: {st.equity:,.2f} | caída desde máximo: {st.drawdown:.1%} | "
          f"kill switch: {'ACTIVO' if st.kill_switch else 'no'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
