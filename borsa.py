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
_ALL_TICKERS = [SPY] + [t for t, _ in SECTORS]

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


def build_title(session: date, spy_pct: float | None) -> str:
    cap = f" — S&P 500 {fmt_pct(spy_pct)}" if spy_pct is not None else ""
    return f"📊 Tancament de Wall Street · {session.strftime('%d/%m/%Y')}{cap}"


# --------------------------------------------------------------------------- #
# Heatmap (matplotlib → PNG bytes)                                            #
# --------------------------------------------------------------------------- #
def render_heatmap(session: date, rows: list[SectorRow],
                   spy_pct: float | None) -> bytes:
    """Pinta el heatmap (caselles per sector + resum S&P) i retorna el PNG."""
    import io

    import matplotlib
    matplotlib.use("Agg")  # backend headless (CI sense pantalla)
    import matplotlib.pyplot as plt
    from matplotlib.colors import TwoSlopeNorm

    norm = TwoSlopeNorm(vmin=-2.0, vcenter=0.0, vmax=2.0)
    cmap = matplotlib.colormaps["RdYlGn"]  # API estable (matplotlib ≥3.6)

    # Caselles: resum S&P primer (si hi és) + sectors, en graella de 4 columnes.
    cells: list[tuple[str, float, bool]] = []
    if spy_pct is not None:
        cells.append(("S&P 500", spy_pct, True))
    cells.extend((r.label, r.pct, False) for r in rows)

    ncols = 4
    nrows = -(-len(cells) // ncols)  # ceil
    fig, ax = plt.subplots(figsize=(ncols * 2.4, nrows * 1.5 + 0.6))
    ax.set_xlim(0, ncols)
    ax.set_ylim(0, nrows)
    ax.axis("off")

    for i, (label, pct, is_index) in enumerate(cells):
        col = i % ncols
        row = i // ncols
        y = nrows - 1 - row  # de dalt a baix
        color = cmap(norm(max(-2.0, min(2.0, pct))))
        ax.add_patch(plt.Rectangle((col, y), 1, 1, facecolor=color,
                                   edgecolor="white", linewidth=2))
        # Color del text segons la lluminositat del fons (llegibilitat).
        r_, g_, b_, _ = color
        text_color = "white" if (0.299 * r_ + 0.587 * g_ + 0.114 * b_) < 0.55 else "black"
        weight = "bold" if is_index else "normal"
        ax.text(col + 0.5, y + 0.60, label, ha="center", va="center",
                fontsize=10, weight=weight, color=text_color)
        ax.text(col + 0.5, y + 0.32, fmt_pct(pct), ha="center", va="center",
                fontsize=13, weight="bold", color=text_color)

    fig.suptitle(f"Tancament S&P 500 per sectors · {session.strftime('%d/%m/%Y')}",
                 fontsize=14, weight="bold", y=0.99)
    fig.tight_layout(rect=(0, 0, 1, 0.96))

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

    png = render_heatmap(session, rows, spy_pct)
    comment = build_comment(rows, spy_pct, use_llm=use_llm)
    key = f"borsa/{session.isoformat()}.png"

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
