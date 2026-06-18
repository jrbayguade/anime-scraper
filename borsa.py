"""
borsa.py — Heatmap diari del tancament de l'S&P 500 (cinquena sortida).

Cada dia de mercat publica a r/lapelaeslapela un heatmap dels 11 sectors GICS de
l'S&P 500 (via ETFs SPDR + SPY pel titular), pintat verd/vermell segons el % del
dia, amb un comentari en català generat per DeepSeek a partir de les dades reals.

Flux:
    yfinance → % per sector → matplotlib (PNG) → R2 (URL pública)
            → DeepSeek (comentari) → queue_store.enqueue(tipus="imatge")

Decisions clau (vegeu docs/superpowers/specs/2026-06-18-borsa-heatmap-design.md):
- DeepSeek MAI inventa dades: rep els números calculats i només els relata.
- Imatge generada → s'ha d'hostatjar (R2) per tenir-ne una URL.
- El relat va al PRIMER COMENTARI (comment_markdown); un post d'imatge no té cos.
- Fallback de DeepSeek NO fatal: si cau, comentari determinista (la imatge mana).
- Idempotència: borsa_history.json guarda l'última sessió; si no n'hi ha de nova
  (cap de setmana / festiu de borsa), no es publica res.

Ús:
    python borsa.py --post    # preview: desa el PNG a output/, no puja ni encua
    python borsa.py --push    # genera, puja a R2 i encua a la cua del Worker
    python borsa.py --no-llm  # comentari determinista (sense DeepSeek)
    python borsa.py --debug   # imprimeix la taula crua de % per sector
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass
from datetime import date, datetime

import config

log = logging.getLogger("anime-scraper.borsa")

try:
    import queue_store
    _HAS_QUEUE = True
except Exception:  # pragma: no cover
    _HAS_QUEUE = False

SPY = "SPY"

# Sectors GICS via ETFs SPDR → etiqueta catalana, en l'ordre canònic de sector.
SECTORS: list[tuple[str, str]] = [
    ("XLK", "Tecnologia"),
    ("XLC", "Comunicacions"),
    ("XLY", "Consum discrecional"),
    ("XLP", "Consum bàsic"),
    ("XLE", "Energia"),
    ("XLF", "Finances"),
    ("XLV", "Salut"),
    ("XLI", "Indústria"),
    ("XLB", "Materials"),
    ("XLRE", "Immobiliari"),
    ("XLU", "Serveis públics"),
]
_LABELS = dict(SECTORS)
_SECTOR_TICKERS = [SPY] + [t for t, _ in SECTORS]

# Principals valors de l'S&P 500 (≈top 40 per pes) per al treemap estil Finviz.
# Llista calibrable: (ticker yfinance, nom curt per a l'etiqueta). Una sola classe
# d'Alphabet (GOOGL) per no duplicar. La mida del quadre la marca la capitalització
# (en viu via yfinance), no aquest ordre.
CONSTITUENTS: list[tuple[str, str]] = [
    ("AAPL", "Apple"), ("MSFT", "Microsoft"), ("NVDA", "Nvidia"),
    ("AMZN", "Amazon"), ("GOOGL", "Alphabet"), ("META", "Meta"),
    ("AVGO", "Broadcom"), ("TSLA", "Tesla"), ("BRK-B", "Berkshire"),
    ("JPM", "JPMorgan"), ("LLY", "Eli Lilly"), ("V", "Visa"),
    ("UNH", "UnitedHealth"), ("XOM", "Exxon"), ("MA", "Mastercard"),
    ("COST", "Costco"), ("HD", "Home Depot"), ("PG", "P&G"),
    ("JNJ", "J&J"), ("WMT", "Walmart"), ("NFLX", "Netflix"),
    ("BAC", "Bank of America"), ("ABBV", "AbbVie"), ("CRM", "Salesforce"),
    ("ORCL", "Oracle"), ("CVX", "Chevron"), ("KO", "Coca-Cola"),
    ("MRK", "Merck"), ("AMD", "AMD"), ("PEP", "PepsiCo"),
    ("ADBE", "Adobe"), ("LIN", "Linde"), ("MCD", "McDonald's"),
    ("CSCO", "Cisco"), ("ACN", "Accenture"), ("ABT", "Abbott"),
    ("GE", "GE"), ("WFC", "Wells Fargo"), ("QCOM", "Qualcomm"),
    ("TXN", "Texas Instr."),
]
_CONSTITUENT_NAMES = dict(CONSTITUENTS)
_CONSTITUENT_TICKERS = [t for t, _ in CONSTITUENTS]
_ALL_TICKERS = _SECTOR_TICKERS + _CONSTITUENT_TICKERS

# To de marca per al comentari (català, comunitat de finances, sense farciment).
SYSTEM_PROMPT = (
    "Ets el divulgador de mercats de r/lapelaeslapela, una comunitat catalana de "
    "diners i economia. Expliques el tancament de Wall Street en català natural, "
    "directe i sense floritures, per a gent interessada en finances però no "
    "necessàriament experta. Vas al gra, fas servir Markdown amb mesura i no "
    "inventes mai dades: només comentes els números que et donen."
)


@dataclass
class SectorRow:
    label: str
    ticker: str
    pct: float


@dataclass
class Stock:
    ticker: str
    name: str
    market_cap: float
    pct: float


# --------------------------------------------------------------------------- #
# Format                                                                       #
# --------------------------------------------------------------------------- #
def fmt_pct(p: float) -> str:
    """Percentatge amb signe i coma decimal catalana: 0.82 -> '+0,82%'."""
    return f"{p:+.2f}%".replace(".", ",")


# --------------------------------------------------------------------------- #
# Dades (I/O: yfinance). Aïllat perquè els tests el puguin mockejar.           #
# --------------------------------------------------------------------------- #
def fetch_closes(tickers: list[str]) -> dict[str, list[tuple[date, float]]]:
    """Tancaments diaris recents per ticker, ordenats per data (mín. 2 punts).

    Retorna {ticker: [(data, close), ...]}. Els tickers sense prou dades s'ometen.
    """
    import yfinance as yf  # import diferit: només si realment es baixen dades

    raw = yf.download(
        tickers, period="7d", interval="1d",
        auto_adjust=True, progress=False, group_by="ticker",
    )
    out: dict[str, list[tuple[date, float]]] = {}
    for t in tickers:
        try:
            ser = raw[t]["Close"].dropna()
        except Exception:  # noqa: BLE001
            log.warning("Sense dades de tancament per a %s", t)
            continue
        closes = sorted(
            ((idx.date(), float(v)) for idx, v in ser.items()),
            key=lambda x: x[0],
        )
        if len(closes) >= 2:
            out[t] = closes
    return out


def fetch_market_caps(tickers: list[str]) -> dict[str, float]:
    """Capitalització en viu per ticker (yfinance fast_info). Fail-soft per ticker.

    Si un ticker falla o no en té, simplement no surt al diccionari (el treemap
    l'ignorarà). Si falla tot, retorna {} i el treemap es desactiva sol.
    """
    import yfinance as yf

    caps: dict[str, float] = {}
    for t in tickers:
        try:
            # Accés per ATRIBUT: el .get() de FastInfo no normalitza la clau i
            # retorna None; fast_info.market_cap sí que dona el valor.
            cap = yf.Ticker(t).fast_info.market_cap
            if cap:
                caps[t] = float(cap)
        except Exception:  # noqa: BLE001
            log.warning("Sense capitalització per a %s", t)
    return caps


# --------------------------------------------------------------------------- #
# Lògica pura                                                                  #
# --------------------------------------------------------------------------- #
def compute_changes(
    closes: dict[str, list[tuple[date, float]]],
) -> tuple[date | None, dict[str, float]]:
    """(data de l'última sessió, {ticker: % vs tancament anterior}).

    La data de sessió la marca SPY si hi és (si no, la més recent disponible).
    """
    changes: dict[str, float] = {}
    for t, series in closes.items():
        (_, prev_c), (_, last_c) = series[-2], series[-1]
        if prev_c:
            changes[t] = (last_c - prev_c) / prev_c * 100.0

    session: date | None = None
    if SPY in closes:
        session = closes[SPY][-1][0]
    else:
        for series in closes.values():
            d = series[-1][0]
            if session is None or d > session:
                session = d
    return session, changes


def build_rows(changes: dict[str, float]) -> list[SectorRow]:
    """Files de sectors (sense SPY), ordenades de més a menys % del dia."""
    rows = [
        SectorRow(_LABELS[t], t, changes[t])
        for t, _ in SECTORS
        if t in changes
    ]
    rows.sort(key=lambda r: r.pct, reverse=True)
    return rows


def build_stock_rows(changes: dict[str, float],
                     caps: dict[str, float]) -> list[Stock]:
    """Valors amb % i capitalització, ordenats de més gran a més petit (treemap).

    Només inclou els que tenen alhora % i capitalització; si no n'hi ha cap, el
    treemap es desactiva (només es publica el heatmap de sectors).
    """
    stocks = [
        Stock(t, _CONSTITUENT_NAMES[t], caps[t], changes[t])
        for t, _ in CONSTITUENTS
        if t in changes and t in caps
    ]
    stocks.sort(key=lambda s: s.market_cap, reverse=True)
    return stocks


def build_title(session: date, spy_pct: float | None) -> str:
    cap = f" — S&P 500 {fmt_pct(spy_pct)}" if spy_pct is not None else ""
    return f"📊 Tancament de Wall Street · {session.strftime('%d/%m/%Y')}{cap}"


# --------------------------------------------------------------------------- #
# Imatge (matplotlib → PNG bytes): heatmap de sectors + treemap de valors      #
# --------------------------------------------------------------------------- #
def _text_color(rgba) -> str:
    """Blanc o negre segons la lluminositat del fons, per llegibilitat."""
    r_, g_, b_, _ = rgba
    return "white" if (0.299 * r_ + 0.587 * g_ + 0.114 * b_) < 0.55 else "black"


def _draw_sector_grid(ax, rows: list[SectorRow], spy_pct: float | None, cmap) -> None:
    """Graella de caselles: resum S&P + 11 sectors, color clampat a ±2%."""
    from matplotlib.colors import TwoSlopeNorm
    from matplotlib.patches import Rectangle

    norm = TwoSlopeNorm(vmin=-2.0, vcenter=0.0, vmax=2.0)
    cells: list[tuple[str, float, bool]] = []
    if spy_pct is not None:
        cells.append(("S&P 500", spy_pct, True))
    cells.extend((r.label, r.pct, False) for r in rows)

    ncols = 4
    nrows = -(-len(cells) // ncols)  # ceil
    ax.set_xlim(0, ncols)
    ax.set_ylim(0, nrows)
    ax.axis("off")
    ax.set_title("Per sectors", fontsize=14, weight="bold", loc="left", pad=6)

    for i, (label, pct, is_index) in enumerate(cells):
        col, row = i % ncols, i // ncols
        y = nrows - 1 - row
        color = cmap(norm(max(-2.0, min(2.0, pct))))
        ax.add_patch(Rectangle((col, y), 1, 1, facecolor=color,
                               edgecolor="white", linewidth=2))
        tc = _text_color(color)
        ax.text(col + 0.5, y + 0.58, label, ha="center", va="center",
                fontsize=12, weight="bold" if is_index else "normal", color=tc)
        ax.text(col + 0.5, y + 0.28, fmt_pct(pct), ha="center", va="center",
                fontsize=15, weight="bold", color=tc)


def _draw_treemap(ax, stocks: list[Stock], cmap) -> None:
    """Treemap estil Finviz: mida per capitalització, color per % (clamp ±3%)."""
    import squarify
    from matplotlib.colors import TwoSlopeNorm
    from matplotlib.patches import Rectangle

    norm = TwoSlopeNorm(vmin=-3.0, vcenter=0.0, vmax=3.0)
    # Espai 16:9 (ample): amb un subplot horitzontal, les caselles surten amples
    # i hi caben etiquetes més grans.
    W, H = 160.0, 90.0
    sizes = squarify.normalize_sizes([s.market_cap for s in stocks], W, H)
    rects = squarify.squarify(sizes, 0, 0, W, H)

    ax.set_xlim(0, W)
    ax.set_ylim(0, H)
    ax.invert_yaxis()  # de més gran (dalt-esq) cap avall, com Finviz
    ax.axis("off")
    ax.set_title("Principals valors", fontsize=14, weight="bold", loc="left", pad=6)

    for st, rc in zip(stocks, rects):
        x, y, dx, dy = rc["x"], rc["y"], rc["dx"], rc["dy"]
        color = cmap(norm(max(-3.0, min(3.0, st.pct))))
        ax.add_patch(Rectangle((x, y), dx, dy, facecolor=color,
                               edgecolor="white", linewidth=1.5))
        tc = _text_color(color)
        # Mida de lletra proporcional a la caixa (més gran que abans), amb topall
        # perquè als quadres gegants no quedi desproporcionada.
        fs = max(8.0, min(min(dx, dy) * 0.62, 22.0))
        if min(dx, dy) < 4:
            continue  # caixa massa petita per a cap etiqueta
        two_lines = dy > 11 and dx > 14
        ax.text(x + dx / 2, y + dy / 2 - (fs * 0.4 if two_lines else 0),
                st.ticker, ha="center", va="center", fontsize=fs,
                weight="bold", color=tc)
        if two_lines:
            ax.text(x + dx / 2, y + dy / 2 + fs * 0.6, fmt_pct(st.pct),
                    ha="center", va="center", fontsize=fs * 0.72, color=tc)


def render_image(session: date, rows: list[SectorRow], spy_pct: float | None,
                 stocks: list[Stock] | None = None) -> bytes:
    """Munta la imatge (sectors + treemap si hi ha valors) i retorna el PNG."""
    import io

    import matplotlib
    matplotlib.use("Agg")  # backend headless (CI sense pantalla)
    import matplotlib.pyplot as plt

    cmap = matplotlib.colormaps["RdYlGn"]  # API estable (matplotlib ≥3.6)

    if stocks:
        # Format horitzontal: sectors a dalt (franja compacta) + treemap a sota
        # (protagonista, ample). Aprofita l'amplada quan Reddit l'escala.
        fig = plt.figure(figsize=(16, 10), layout="constrained")
        gs = fig.add_gridspec(2, 1, height_ratios=[2, 4])
        _draw_sector_grid(fig.add_subplot(gs[0]), rows, spy_pct, cmap)
        _draw_treemap(fig.add_subplot(gs[1]), stocks, cmap)
    else:
        fig = plt.figure(figsize=(16, 5.5), layout="constrained")
        _draw_sector_grid(fig.add_subplot(1, 1, 1), rows, spy_pct, cmap)

    fig.suptitle(f"Tancament de l'S&P 500 · {session.strftime('%d/%m/%Y')}",
                 fontsize=15, weight="bold")

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130, bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# Comentari (DeepSeek amb fallback determinista)                              #
# --------------------------------------------------------------------------- #
def _deterministic_comment(rows: list[SectorRow], spy_pct: float | None) -> str:
    millor, pitjor = rows[0], rows[-1]
    cap = f"**S&P 500: {fmt_pct(spy_pct)}**\n\n" if spy_pct is not None else ""
    return (
        f"{cap}"
        f"Sector que més puja: **{millor.label}** ({fmt_pct(millor.pct)}). "
        f"El que més baixa: **{pitjor.label}** ({fmt_pct(pitjor.pct)}).\n\n"
        "Com ho veieu? 👇"
    )


def build_comment(rows: list[SectorRow], spy_pct: float | None,
                  use_llm: bool = True) -> str:
    """Comentari per al primer comentari del post; DeepSeek o determinista."""
    if not use_llm or not rows:
        return _deterministic_comment(rows, spy_pct) if rows else \
            "Avui no hi ha dades de sessió."

    try:
        from processor import _deepseek_chat
    except Exception as exc:  # pragma: no cover
        log.warning("No es pot importar el client DeepSeek: %s", exc)
        return _deterministic_comment(rows, spy_pct)

    taula = "\n".join(f"- {r.label}: {fmt_pct(r.pct)}" for r in rows)
    spy_line = f"S&P 500 global: {fmt_pct(spy_pct)}\n" if spy_pct is not None else ""
    user = (
        "Aquestes són les variacions REALS del tancament d'avui a Wall Street "
        "(no n'inventis cap altra ni n'afegeixis de noves):\n\n"
        f"{spy_line}{taula}\n\n"
        "Escriu un comentari en català de DOS paràgrafs curts: el primer resumeix "
        "el to general del dia i destaca els sectors que més s'han mogut (amunt i "
        "avall) i, si ho saps amb certesa general, per què; el segon, una mica de "
        "context o què mirar. Acaba amb UNA pregunta perquè la gent comenti. Fes "
        "servir Markdown amb mesura (alguna negreta). No posis títol ni encapçalament."
    )
    out = _deepseek_chat(
        [{"role": "system", "content": SYSTEM_PROMPT},
         {"role": "user", "content": user}],
        temperature=0.7,
        max_tokens=600,
    )
    text = (out or "").strip()
    return text if text else _deterministic_comment(rows, spy_pct)


# --------------------------------------------------------------------------- #
# Històric (idempotència per data de sessió)                                  #
# --------------------------------------------------------------------------- #
def last_session() -> str | None:
    try:
        data = json.loads(config.BORSA_HISTORY_FILE.read_text(encoding="utf-8"))
        return data.get("last_session")
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def save_session(session: date) -> None:
    config.BORSA_HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    config.BORSA_HISTORY_FILE.write_text(
        json.dumps({"last_session": session.isoformat()}, ensure_ascii=False,
                   indent=2),
        encoding="utf-8",
    )


# --------------------------------------------------------------------------- #
# Payload                                                                      #
# --------------------------------------------------------------------------- #
def build_payload(session: date, url: str, rows: list[SectorRow],
                  spy_pct: float | None, comment: str) -> dict:
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "tipus": "imatge",
        "title": build_title(session, spy_pct),
        "subreddit": config.BORSA_SUBREDDIT,
        "url": url,
        "comment_markdown": comment,
        "source": "borsa",
        "source_label": "Borsa",
    }


# --------------------------------------------------------------------------- #
# CLI                                                                          #
# --------------------------------------------------------------------------- #
def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Heatmap diari del tancament de l'S&P 500.")
    p.add_argument("--post", action="store_true",
                   help="Preview: desa el PNG a output/ i mostra el comentari (no encua).")
    p.add_argument("--push", action="store_true",
                   help="Genera, puja a R2 i encua a la cua del Worker.")
    p.add_argument("--no-llm", action="store_true",
                   help="Comentari determinista (sense DeepSeek).")
    p.add_argument("--debug", action="store_true",
                   help="Imprimeix la taula crua de % per sector.")
    p.add_argument("--quiet", action="store_true", help="Menys missatges.")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(message)s",
    )
    use_llm = config.USE_LLM and not args.no_llm

    closes = fetch_closes(_ALL_TICKERS)
    session, changes = compute_changes(closes)
    if session is None or not changes:
        print("❌ Sense dades de mercat (yfinance no ha retornat res).",
              file=sys.stderr)
        return 1

    rows = build_rows(changes)
    spy_pct = changes.get(SPY)

    if args.debug:
        print(f"Sessió: {session.isoformat()}  ·  S&P 500: "
              f"{fmt_pct(spy_pct) if spy_pct is not None else 'n/d'}")
        for r in rows:
            print(f"  {r.label:<22} {r.ticker:<5} {fmt_pct(r.pct)}")
        return 0

    if not rows:
        print("❌ Sense dades de sectors.", file=sys.stderr)
        return 1

    # Idempotència: amb --push, si la sessió no és nova no publiquem res.
    if args.push and last_session() == session.isoformat():
        print(f"⏭️  La sessió {session.isoformat()} ja s'ha publicat "
              "(cap de setmana / festiu / doble execució). No s'encua res.")
        return 0

    # Treemap de valors (fail-soft: si no hi ha capitalitzacions, només sectors).
    caps = fetch_market_caps(_CONSTITUENT_TICKERS)
    stocks = build_stock_rows(changes, caps)
    if not stocks:
        log.warning("Sense capitalitzacions: es publica només el heatmap de sectors.")

    png = render_image(session, rows, spy_pct, stocks)
    comment = build_comment(rows, spy_pct, use_llm=use_llm)
    # Clau ÚNICA per post: si es reutilitza la mateixa URL (un nom per dia),
    # Reddit cacheja la previsualització i segueix mostrant la imatge antiga
    # encara que R2 ja tingui la nova. Un sufix horari força una URL nova.
    key = f"borsa/{session.isoformat()}-{datetime.now():%H%M%S}.png"

    if not args.push:
        out_path = config.OUTPUT_DIR / f"borsa-{session.isoformat()}.png"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(png)
        print(build_title(session, spy_pct))
        print("-" * 70)
        print(f"Heatmap desat a: {out_path}")
        print("-" * 70)
        print(comment)
        return 0

    if not _HAS_QUEUE:
        print("❌ queue_store no disponible: no s'ha encuat.", file=sys.stderr)
        return 1

    import r2_upload
    try:
        url = r2_upload.upload_bytes(png, key)
    except RuntimeError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 1

    payload = build_payload(session, url, rows, spy_pct, comment)
    item_id = queue_store.enqueue(payload)
    save_session(session)
    print(f"✅ Encuat: {payload['title']} → {item_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
