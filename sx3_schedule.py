#!/usr/bin/env python3
"""
sx3_schedule.py — Graella d'anime del canal SX3 per al cap de setmana.

Genera la graella de programació de SX3 (3Cat) per a una finestra de dies i,
per defecte, l'exporta a CSV perquè es pugui VERIFICAR abans de publicar res.
No publica res a Reddit: només llegeix dades i escriu fitxers/consola.

── Font de dades ──────────────────────────────────────────────────────────────
La pàgina https://www.3cat.cat/tv3/programacio/canal-sx3/ és un Next.js que pinta
la graella amb dades d'aquesta API JSON (descoberta al payload SSR, clau
`urlGraella`):

    https://api.3cat.cat/graellatvfutur?_format=json&canal=CAD_SX3
        &data_emissio=AVUI&pagina=1&sdom=img&version=2.0&cache=90&master=yes

`data_emissio` accepta offsets RELATIUS de jornada: AVUI, AVUI+1, … AVUI+9.
Cada jornada va de ~06:00 d'un dia a ~06:00 de l'endemà (per això una jornada
conté dues dates de calendari). Cada ítem porta: `titol`, `capitols[].desc`
(episodi), `entradeta` (sinopsi), `data_emissio` ("DD/MM/YYYY HH:MM:SS") i
`programes[].nom_bonic` (slug per a l'enllaç /tv3/sx3/<slug>/).

── Ús ──────────────────────────────────────────────────────────────────────────
    python sx3_schedule.py                  # 7 dies des d'avui → CSV + preview
    python sx3_schedule.py --from-next-friday  # finestra real divendres→dijous
    python sx3_schedule.py --days 3         # només 3 dies
    python sx3_schedule.py --all            # preview amb TOTS els programes
    python sx3_schedule.py --anime-only     # CSV només amb files d'anime
    python sx3_schedule.py --post           # imprimeix el post (Markdown) sense publicar
    python sx3_schedule.py --push           # envia el post al webhook de make (→ Reddit)
    python sx3_schedule.py --debug          # estadístiques crues de l'API

El cron de divendres executa `--push` (que arrenca a AVUI = divendres). Reutilitza
el webhook MAKE_WEBHOOK_URL del recull setmanal (make limita a 2 escenaris actius),
i DEEPSEEK_API_KEY per a la intro.
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import sys
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from typing import Optional

import requests

# Reaprofitem capçaleres/rutes del projecte si hi són; si no, fallback autònom.
try:
    import config
    _HEADERS = config.HTTP_HEADERS
    _OUTPUT_DIR = config.OUTPUT_DIR
    _SUBREDDIT = getattr(config, "SUBREDDIT", "AnimeCatala")
    # Reutilitzem el MATEIX webhook que el recull setmanal: amb make limitat a 2
    # escenaris, el post de SX3 passa per l'escenari d'anime ja existent.
    _WEBHOOK = (getattr(config, "MAKE_WEBHOOK_URL", "")
                or os.getenv("MAKE_WEBHOOK_URL", "")).strip()
except Exception:  # pragma: no cover - el script ha de funcionar tot sol
    from pathlib import Path
    _HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json,text/plain,*/*",
        "Accept-Language": "ca,es;q=0.9,en;q=0.8",
    }
    _OUTPUT_DIR = Path(__file__).resolve().parent / "output"
    _SUBREDDIT = "AnimeCatala"
    _WEBHOOK = os.getenv("MAKE_WEBHOOK_URL", "").strip()

log = logging.getLogger("anime-scraper.sx3")

# --------------------------------------------------------------------------- #
# Configuració                                                                #
# --------------------------------------------------------------------------- #
API_BASE = "https://api.3cat.cat/graellatvfutur"   # alt: https://api.ccma.cat/...
CANAL = "CAD_SX3"
BASE_URL = "https://www.3cat.cat"
REQUEST_TIMEOUT = 20
REQUEST_RETRIES = 2
MAX_OFFSET = 9                # l'API només serveix AVUI … AVUI+9

# Dies de la setmana en català (Monday=0 … Sunday=6)
_WEEKDAYS_CA = ["dilluns", "dimarts", "dimecres", "dijous",
                "divendres", "dissabte", "diumenge"]

# Paraules clau per marcar un programa com a "anime" (case-insensitive, dins el
# títol o el slug). LLISTA INICIAL — cal calibrar-la veient el CSV de tots els
# programes (columna `anime`). Afegeix/treu títols segons la graella real de SX3.
ANIME_KEYWORDS = [
    "doraemon", "shin-chan", "shin chan", "bola de drac", "dragon ball",
    "detectiu conan", "one piece", "pokémon", "pokemon", "inazuma",
    "beyblade", "yu-gi-oh", "yugioh", "naruto", "capità tsubasa", "campions",
    "oliver i benji", "ranma", "slam dunk", "inuyasha", "sailor moon",
    "dr. slump", "hamtaro", "crayon", "kochikame", "vickie", "vicky el viking",
    "heidi", "marco", " that time i got reincarnated", "spy x family",
    "jujutsu", "demon slayer", "kimetsu", "boku no hero", "my hero",
    "one punch", "death note", "fairy tail", "bleach", "haikyu",
]


# --------------------------------------------------------------------------- #
# Model de dades                                                              #
# --------------------------------------------------------------------------- #
@dataclass
class Programa:
    inici: datetime          # data/hora d'emissió
    titol: str
    episodi: str             # capítol (capitols[].desc)
    entradeta: str           # sinopsi curta
    slug: str                # nom_bonic → enllaç
    anime: bool = False
    durada_min: Optional[int] = None  # minuts fins al següent programa (estimat)

    @property
    def url(self) -> str:
        return f"{BASE_URL}/tv3/sx3/{self.slug}/" if self.slug else ""

    @property
    def data(self) -> str:
        return self.inici.strftime("%Y-%m-%d")

    @property
    def dia_setmana(self) -> str:
        return _WEEKDAYS_CA[self.inici.weekday()]

    @property
    def hora(self) -> str:
        return self.inici.strftime("%H:%M")

    def dedupe_key(self) -> str:
        return f"{self.inici:%Y%m%d%H%M}|{self.titol.strip().lower()}"


# --------------------------------------------------------------------------- #
# Descàrrega i parsing                                                        #
# --------------------------------------------------------------------------- #
def build_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(_HEADERS)
    return s


def fetch_offset(session: requests.Session, offset: int) -> list[dict]:
    """Descarrega la jornada AVUI+offset i retorna la llista d'ítems crus."""
    data_emissio = "AVUI" if offset == 0 else f"AVUI+{offset}"
    params = {
        "_format": "json",
        "canal": CANAL,
        "data_emissio": data_emissio,
        "pagina": "1",
        "sdom": "img",
        "version": "2.0",
        "cache": "90",
        "master": "yes",
    }
    for attempt in range(1, REQUEST_RETRIES + 2):
        try:
            r = session.get(API_BASE, params=params, timeout=REQUEST_TIMEOUT)
            r.raise_for_status()
            payload = r.json()
            items = payload.get("resposta", {}).get("items", {}).get("item", []) or []
            log.info("  AVUI+%d (%s): %d programes", offset, data_emissio, len(items))
            return items
        except (requests.RequestException, ValueError) as exc:
            log.warning("Petició AVUI+%d fallida (%d/%d): %s",
                        offset, attempt, REQUEST_RETRIES + 1, exc)
    log.error("No s'ha pogut descarregar la jornada AVUI+%d.", offset)
    return []


def _parse_dt(text: str) -> Optional[datetime]:
    """'DD/MM/YYYY HH:MM:SS' → datetime."""
    try:
        return datetime.strptime(text.strip(), "%d/%m/%Y %H:%M:%S")
    except (ValueError, AttributeError):
        return None


def _clean(text: str) -> str:
    """Neteja mojibake de la font (p. ex. '¿' on hauria d'anar un apòstrof)."""
    return (text or "").replace("¿", "'").replace(" ", " ").strip()


def is_anime(titol: str, slug: str) -> bool:
    hay = f"{titol} {slug}".lower()
    return any(kw in hay for kw in ANIME_KEYWORDS)


def parse_item(item: dict) -> Optional[Programa]:
    inici = _parse_dt(item.get("data_emissio", ""))
    if not inici:
        return None
    titol = _clean(item.get("titol") or "")
    capitols = item.get("capitols") or []
    episodi = _clean(capitols[0].get("desc") if capitols else "")
    entradeta = _clean(item.get("entradeta") or "")
    programes = item.get("programes") or []
    slug = (programes[0].get("nom_bonic") if programes else "") or ""
    return Programa(
        inici=inici, titol=titol, episodi=episodi,
        entradeta=entradeta, slug=slug,
        anime=is_anime(titol, slug),
    )


# --------------------------------------------------------------------------- #
# Construcció de la graella                                                   #
# --------------------------------------------------------------------------- #
def build_schedule(session: requests.Session, start_offset: int,
                   days: int) -> list[Programa]:
    """Recull `days` jornades a partir d'AVUI+start_offset, deduplica i ordena."""
    seen: set[str] = set()
    progs: list[Programa] = []
    last = min(start_offset + days - 1, MAX_OFFSET)
    if start_offset + days - 1 > MAX_OFFSET:
        log.warning("L'API només arriba fins AVUI+%d; es retalla la finestra.",
                    MAX_OFFSET)
    for offset in range(start_offset, last + 1):
        for item in fetch_offset(session, offset):
            p = parse_item(item)
            if not p:
                continue
            key = p.dedupe_key()
            if key in seen:
                continue
            seen.add(key)
            progs.append(p)

    progs.sort(key=lambda p: p.inici)

    # Finestra exacta: des de l'inici del primer dia fins a +days dies naturals.
    if progs:
        first_day = progs[0].inici.date()
        # El primer offset arrenca ~06:00; considerem el "dia" com la data del
        # primer programa. Tallem a first_day + days (exclòs).
        end = datetime.combine(first_day + timedelta(days=days), datetime.min.time())
        progs = [p for p in progs if p.inici < end]

    # Durada estimada = diferència fins al següent programa (mateix dia natural)
    for i, p in enumerate(progs):
        if i + 1 < len(progs):
            delta = (progs[i + 1].inici - p.inici).total_seconds() / 60
            if 0 < delta <= 600:
                p.durada_min = int(round(delta))
    return progs


def upcoming_friday_offset(today: Optional[datetime] = None) -> int:
    """Nombre de dies (offset AVUI+n) fins al proper divendres (avui inclòs)."""
    today = today or datetime.now()
    # weekday(): divendres = 4
    return (4 - today.weekday()) % 7


# --------------------------------------------------------------------------- #
# Sortides                                                                    #
# --------------------------------------------------------------------------- #
CSV_FIELDS = ["data", "dia_setmana", "hora", "durada_min", "anime",
              "titol", "episodi", "url", "entradeta"]


def write_csv(progs: list[Programa], path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(CSV_FIELDS)
        for p in progs:
            w.writerow([
                p.data, p.dia_setmana, p.hora,
                p.durada_min if p.durada_min is not None else "",
                "Sí" if p.anime else "No",
                p.titol, p.episodi, p.url, p.entradeta,
            ])


def print_preview(progs: list[Programa], anime_only: bool) -> None:
    shown = [p for p in progs if p.anime] if anime_only else progs
    current = None
    for p in shown:
        if p.data != current:
            current = p.data
            print(f"\n📅 {p.dia_setmana.capitalize()} {p.inici:%d/%m/%Y}")
        tag = "🍥 " if p.anime else "   "
        ep = f" — {p.episodi}" if p.episodi else ""
        dur = f" ({p.durada_min}′)" if p.durada_min else ""
        print(f"  {tag}{p.hora}  {p.titol}{ep}{dur}")


_MONTHS_CA = ["gener", "febrer", "març", "abril", "maig", "juny", "juliol",
              "agost", "setembre", "octubre", "novembre", "desembre"]


def _date_ca(d: datetime) -> str:
    return f"{d.day} de {_MONTHS_CA[d.month - 1]}"


def _fmt_blocks(starts: list[datetime]) -> str:
    """Hores d'inici → blocs 'HH:MM–HH:MM' (es talla un bloc si hi ha >45 min de salt).

    Així el bloc de la tarda i el seu repàs de la nit surten com a dos rangs:
    '13:44–15:19 · 21:00–22:23'.
    """
    starts = sorted(starts)
    blocks: list[tuple] = []
    ini = prev = starts[0]
    for t in starts[1:]:
        if (t - prev).total_seconds() > 45 * 60:
            blocks.append((ini, prev))
            ini = t
        prev = t
    blocks.append((ini, prev))
    return " · ".join(f"{a:%H:%M}" if a == b else f"{a:%H:%M}–{b:%H:%M}"
                      for a, b in blocks)


def group_day_by_show(day_progs: list[Programa]) -> list[dict]:
    """Agrupa per sèrie dins d'un dia, ordenat per la 1a emissió del dia."""
    shows: dict[str, dict] = {}
    for p in day_progs:
        g = shows.get(p.titol)
        if not g:
            g = {"titol": p.titol, "slug": p.slug, "starts": []}
            shows[p.titol] = g
        g["starts"].append(p.inici)
    items = list(shows.values())
    items.sort(key=lambda g: min(g["starts"]))
    return items


# Angles per a la pregunta final. Se'n tria un segons el número de setmana, així
# cada setmana el tema és diferent (rotació determinista) i DeepSeek només el
# redacta lligant-lo a les sèries d'aquells dies.
_QUESTION_ANGLES = [
    "el personatge preferit",
    "l'opening o la música que més recordeu",
    "l'arc o la saga favorita",
    "la rivalitat o el duel més èpic",
    "el moment més icònic o emotiu",
    "quina sèrie recomanaríeu a algú que tot just comença",
    "la transformació, poder o tècnica que voldríeu tenir",
    "el record d'infància lligat a aquestes sèries",
    "el vilà o antagonista que més us va marcar",
]

# Preguntes de reserva (rotatives) si DeepSeek no respon.
_FALLBACK_QUESTIONS = [
    "Quin és el vostre personatge d'anime preferit de tots els temps? 👇",
    "Quin opening d'aquestes sèries se us queda al cap tot el dia? 🎵",
    "Quina saga o arc us va enganxar més? 🍿",
    "Quin duel o rivalitat us sembla el més èpic? ⚔️",
    "Quin moment us emociona cada cop que el torneu a veure? 🥹",
    "Quina d'aquestes sèries recomanaríeu per començar amb l'anime? 📺",
    "Quina transformació o tècnica us hauria agradat tenir de petits? 💥",
    "On i amb qui vèieu aquestes sèries quan éreu petits? 🛋️",
    "Quin vilà d'aquestes sèries us va marcar més? 😈",
]


def _week_index(first: datetime) -> int:
    return first.isocalendar()[1]


def _fallback_question(first: datetime) -> str:
    return _FALLBACK_QUESTIONS[_week_index(first) % len(_FALLBACK_QUESTIONS)]


def generate_intro_question(shows: list[str], first: datetime, last: datetime,
                            use_llm: bool = True) -> tuple[str, str]:
    """(intro, pregunta) generats per DeepSeek; fallback estàtic per a cadascun.

    Payload MÍNIM cap a l'LLM: només els títols d'anime distints i el rang de
    dates. La pregunta final és per generar comentaris i canvia de tema cada cop.
    """
    rang = f"{_date_ca(first)} al {_date_ca(last)}"
    destacats = ", ".join(shows[:3]) if shows else "els teus animes preferits"
    intro_fb = (f"🍥 Ja tenim la graella d'anime de **SX3** del {rang}! "
                f"Aquests dies fan {destacats}. 📺")
    quest_fb = _fallback_question(first)
    if not use_llm:
        return intro_fb, quest_fb
    try:
        from processor import _deepseek_chat, _extract_json  # client del projecte
    except Exception:
        return intro_fb, quest_fb
    angle = _QUESTION_ANGLES[_week_index(first) % len(_QUESTION_ANGLES)]
    raw = _deepseek_chat(
        [
            {"role": "system", "content": "Ets el community manager d'una comunitat "
             "catalana d'anime. Escrius en català, to proper i animat."},
            {"role": "user", "content":
                f"Per a un post amb la graella d'anime del canal SX3 del {rang}, "
                f"dona'm DUES coses:\n"
                f"1) 'intro': introducció breu (1-2 frases, 1-2 emojis), sense llistes.\n"
                f"2) 'pregunta': UNA pregunta final curta i engaging per animar els "
                f"comentaris, enfocada concretament en aquest tema: «{angle}», i "
                f"lligada a les sèries d'aquests dies. Acaba amb un emoji.\n"
                f"Sèries d'anime aquests dies: {', '.join(shows)}.\n"
                f'Respon NOMÉS amb un JSON: {{"intro": "...", "pregunta": "..."}}'},
        ],
        temperature=0.8, max_tokens=250,
    )
    data = _extract_json(raw) if raw else None
    if isinstance(data, dict):
        intro = str(data.get("intro", "")).strip() or intro_fb
        quest = str(data.get("pregunta", "")).strip() or quest_fb
        return intro, quest
    return intro_fb, quest_fb


def build_post(progs: list[Programa], use_llm: bool = True) -> dict:
    """Munta el post (títol + Markdown). Taula en Python; intro amb DeepSeek.

    Retorna {'title': ..., 'markdown': ...}. NOMÉS els programes marcats com a
    anime, consolidats per capítol.
    """
    from collections import Counter
    from itertools import groupby

    animes = [p for p in progs if p.anime]
    if not animes:
        return {"title": "", "markdown":
                "_(Cap programa marcat com a anime amb les paraules clau actuals.)_"}

    first, last = animes[0].inici, animes[-1].inici
    shows = [t for t, _ in Counter(p.titol for p in animes).most_common()]
    title = f"🍥 Anime a SX3 · del {_date_ca(first)} al {_date_ca(last)}"

    intro, pregunta = generate_intro_question(shows, first, last, use_llm=use_llm)
    lines = [intro, ""]
    for _, day_iter in groupby(animes, key=lambda p: p.data):
        day_progs = list(day_iter)
        d0 = day_progs[0].inici
        lines.append(f"### {day_progs[0].dia_setmana.capitalize()} {d0:%d/%m}")
        for g in group_day_by_show(day_progs):
            link = (f"[{g['titol']}]({BASE_URL}/tv3/sx3/{g['slug']}/)"
                    if g["slug"] else g["titol"])
            lines.append(f"- **{link}** · {_fmt_blocks(g['starts'])}")
        lines.append("")
    lines.append(f"💬 **{pregunta}**")
    lines.append("")
    lines.append(f"---\n*🤖 Font: [3Cat — Programació SX3]"
                 f"({BASE_URL}/tv3/programacio/canal-sx3/)*")
    return {"title": title, "markdown": "\n".join(lines)}


# --------------------------------------------------------------------------- #
# Publicació (webhook de make.com → Reddit)                                    #
# --------------------------------------------------------------------------- #
def build_structured(post: dict, progs: list[Programa]) -> dict:
    """Dict per al webhook de make (que el penja a Reddit).

    Fa servir les mateixes claus que el recull setmanal (`title`, `subreddit`,
    `markdown`) perquè el MATEIX escenari de make les consumeixi. L'escenari
    només té un pas després del webhook: publicar a r/AnimeCatala. Per tant
    qualsevol post per a aquell canal pot anar per aquest webhook, sense cap
    marcador ni router.
    """
    first = progs[0].inici if progs else datetime.now()
    last = progs[-1].inici if progs else first
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "week_start": first.strftime("%Y-%m-%d"),
        "week_end": last.strftime("%Y-%m-%d"),
        "subreddit": _SUBREDDIT,
        "tipus": "text",
        "title": post["title"],
        "markdown": post["markdown"],
    }


def push_to_make(structured: dict, webhook_url: str) -> bool:
    """Envia el JSON al webhook de make. Retorna True si ha anat bé."""
    if not webhook_url:
        log.error("Sense MAKE_WEBHOOK_URL: no hi ha on enviar el post.")
        return False
    try:
        r = requests.post(webhook_url, json=structured, timeout=30)
        r.raise_for_status()
        log.info("✅ Post enviat al webhook de make (HTTP %s).", r.status_code)
        return True
    except Exception as exc:  # noqa: BLE001
        log.error("No s'ha pogut enviar a make: %s", exc)
        return False


# --------------------------------------------------------------------------- #
# Publicació manual assistida (sense make ni API; el bot prepara, tu postes)    #
# --------------------------------------------------------------------------- #
# Per quan la connexió de Reddit de make no funciona. Copia el cos del post al
# porta-retalls i obre Reddit; tu enganxes el títol i el cos i cliques Post.
# Mateix patró (WSL → Windows) que publish_manual.py i bluesky_manga.py --manual.
def _to_clipboard(text: str) -> bool:
    """Posa `text` al porta-retalls de Windows des de WSL (robust amb accents)."""
    try:
        import subprocess
        tmp = _OUTPUT_DIR / ".clipboard.tmp"
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(text, encoding="utf-8")
        win = subprocess.check_output(["wslpath", "-w", str(tmp)]).decode().strip()
        subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command",
             f"Set-Clipboard -Value (Get-Content -Raw -Encoding UTF8 -LiteralPath '{win}')"],
            check=True, stderr=subprocess.DEVNULL,
        )
        tmp.unlink(missing_ok=True)
        return True
    except Exception:  # noqa: BLE001
        return False


def _open_browser(url: str) -> None:
    try:
        import subprocess
        subprocess.run(["explorer.exe", url], stderr=subprocess.DEVNULL)
    except Exception:  # noqa: BLE001
        print(f"   (obre manualment: {url})")


def run_manual(post: dict) -> int:
    """Prepara el post de TEXT per publicar-lo a mà: copia el cos i obre Reddit.
    No fa servir make ni l'API de Reddit (i sense límit de llargada del 414)."""
    title = post["title"]
    body = post["markdown"]
    ok = _to_clipboard(body)
    _open_browser(f"https://www.reddit.com/r/{_SUBREDDIT}/submit")
    print("\n" + "=" * 66)
    print("📋  TÍTOL (copia'l al camp «Title»):\n")
    print("    " + title + "\n")
    if ok:
        print("✅  El COS del post ja és al porta-retalls → Ctrl+V al quadre de text.")
    else:
        print("⚠️  No s'ha pogut copiar sol; copia el cos del preview de dalt.")
    print("\nPassos a Reddit (s'ha obert al navegador):")
    print("   1) Tria  Type = Text")
    print("   2) Title → enganxa el títol d'aquí dalt")
    print("   3) Body  → Ctrl+V")
    print("   4) Clica  Post")
    print("=" * 66)
    return 0


# --------------------------------------------------------------------------- #
# CLI                                                                         #
# --------------------------------------------------------------------------- #
def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Graella d'anime de SX3: verifica (CSV/preview) o publica (--push/--manual).")
    p.add_argument("--days", type=int, default=7,
                   help="Nombre de dies de la finestra (per defecte 7: dv→dj).")
    p.add_argument("--from-next-friday", action="store_true",
                   help="Comença la finestra al proper divendres (finestra real del post).")
    p.add_argument("--offset", type=int, default=0,
                   help="Offset inicial manual (AVUI+offset). Ignorat amb --from-next-friday.")
    p.add_argument("--all", action="store_true",
                   help="Preview amb TOTS els programes (no només anime).")
    p.add_argument("--anime-only", action="store_true",
                   help="El CSV inclou només files d'anime (per defecte hi són totes).")
    p.add_argument("--post", action="store_true",
                   help="Genera i imprimeix el post en Markdown (sense publicar).")
    p.add_argument("--push", action="store_true",
                   help="Genera el post i l'envia al webhook de make (MAKE_WEBHOOK_URL).")
    p.add_argument("--manual", action="store_true",
                   help="Publicació manual assistida: copia el cos i obre Reddit "
                        "(sense make ni API).")
    p.add_argument("--no-llm", action="store_true",
                   help="Amb --post/--push, no cridis DeepSeek (intro estàtica, 0 tokens).")
    p.add_argument("--csv", default=None, help="Ruta del CSV de sortida.")
    p.add_argument("--no-csv", action="store_true", help="No escriguis CSV.")
    p.add_argument("--debug", action="store_true", help="Estadístiques crues de l'API.")
    p.add_argument("--quiet", action="store_true", help="Menys missatges.")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(levelname)-7s | %(message)s",
    )

    start_offset = upcoming_friday_offset() if args.from_next_friday else args.offset
    if args.from_next_friday:
        log.info("Proper divendres: AVUI+%d", start_offset)

    session = build_session()

    if args.debug:
        items = fetch_offset(session, start_offset)
        print(f"\n[DEBUG] AVUI+{start_offset}: {len(items)} ítems crus")
        if items:
            import json
            print("[DEBUG] Claus del 1r ítem:", list(items[0].keys()))
            print("[DEBUG] Mostra:",
                  json.dumps({k: items[0].get(k) for k in
                              ("titol", "data_emissio", "capitols", "entradeta")},
                             ensure_ascii=False)[:400])
        return 0

    log.info("Recollint %d dies des d'AVUI+%d…", args.days, start_offset)
    progs = build_schedule(session, start_offset, args.days)
    if not progs:
        print("❌ No s'ha obtingut cap programa.")
        return 1

    n_anime = sum(1 for p in progs if p.anime)
    print(f"\n✅ {len(progs)} programes ({n_anime} marcats com a anime) "
          f"del {progs[0].inici:%d/%m} al {progs[-1].inici:%d/%m}.")

    # Mode publicació: genera el post i envia'l a make (sense escriure fitxers).
    if args.push:
        if n_anime == 0:
            print("⚠️  Cap programa d'anime en aquesta finestra; no s'envia res.")
            return 0
        post = build_post(progs, use_llm=not args.no_llm)
        structured = build_structured(post, progs)
        ok = push_to_make(structured, _WEBHOOK)
        print(f"TÍTOL: {post['title']}")
        print("✅ Enviat a make." if ok else
              "❌ No enviat (revisa MAKE_WEBHOOK_URL).")
        return 0 if ok else 1

    # Mode manual: prepara el post de text per publicar-lo a mà (sense make).
    if args.manual:
        if n_anime == 0:
            print("⚠️  Cap programa d'anime en aquesta finestra; res a preparar.")
            return 0
        post = build_post(progs, use_llm=not args.no_llm)
        return run_manual(post)

    # CSV (per defecte: TOTS els programes amb columna `anime` per verificar)
    if not args.no_csv:
        rows = [p for p in progs if p.anime] if args.anime_only else progs
        from pathlib import Path
        csv_path = Path(args.csv) if args.csv else (
            _OUTPUT_DIR / f"sx3_graella_{progs[0].inici:%Y%m%d}_{progs[-1].inici:%Y%m%d}.csv"
        )
        write_csv(rows, csv_path)
        print(f"📄 CSV escrit: {csv_path}  ({len(rows)} files)")

    # Preview a consola
    print_preview(progs, anime_only=not args.all)

    if args.post:
        post = build_post(progs, use_llm=not args.no_llm)
        from pathlib import Path
        md_path = _OUTPUT_DIR / f"sx3_post_{progs[0].inici:%Y%m%d}_{progs[-1].inici:%Y%m%d}.md"
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text(f"# {post['title']}\n\n{post['markdown']}", encoding="utf-8")
        print("\n" + "=" * 64)
        print(f"TÍTOL: {post['title']}")
        print("=" * 64)
        print(post["markdown"])
        print("=" * 64)
        print(f"📝 Post desat a: {md_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
