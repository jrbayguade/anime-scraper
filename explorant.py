"""
explorant.py — Pack «Explorant Catalunya»: activitats i escapades en família.

Sisena sortida (autònoma). Scrapeja diverses webs catalanes d'activitats —cadascuna
amb el seu propi calendari— en tria UNA fitxa encara no publicada, la resumeix amb
DeepSeek, RE-ALLOTJA la foto a Cloudflare R2 (perquè Reddit la serveixi de forma
fiable, com vam aprendre amb la borsa) i la publica a r/ExplorantCatalunya com a
**post d'imatge + primer comentari** (el resum, amb l'enllaç a la font original al
peu en cursiva).

Arquitectura (pensada per créixer font a font):
- Cada font té una funció `parse_<key>()` que retorna `list[Fitxa]`.
- `SOURCES` registra (nom, web, parser) per clau.
- `sources_due(date)` decideix quines fonts «toquen» avui (dia de setmana /
  setmana del mes), perquè n'hi hagi prou amb UN sol cron diari.
- Si una font encara no té parser (stub) o falla, es registra i es continua: mai
  tomba el procés (convenció de robustesa del projecte).

Calendari (segons l'encàrrec):
    dijous            → surtdecasa (agenda cap de setmana)
    divendres         → femturisme (què fer aquest cap de setmana)
    dimarts           → elmonensespera (escapades a 1h de Barcelona)
    dimecres          → sortirambnens (excursions amb nens)
    dissabte          → barcelona.cat (cap de setmana, nens i nenes)
    1r dilluns de mes → senders.feec (un sender)
    2n dilluns de mes → dexcursio
    3r dilluns de mes → timeout BCN (què fer)
    dia 1 de mes      → escapadaambnens (festes i fires del mes que ve)

Ús:
    python explorant.py --post                  # preview de les fonts que toquen avui
    python explorant.py --source escapadaambnens --post   # prova una font concreta
    python explorant.py --push                  # publica (encua) el que toqui avui
    python explorant.py --no-llm --post         # sense DeepSeek (resum cru)
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime

from urllib.parse import urljoin

import feedparser
import requests
from bs4 import BeautifulSoup

import config

log = logging.getLogger("anime-scraper.explorant")

try:
    import queue_store
    _HAS_QUEUE = True
except Exception:  # pragma: no cover
    _HAS_QUEUE = False

_MONTHS_CA_FULL = [
    "gener", "febrer", "març", "abril", "maig", "juny",
    "juliol", "agost", "setembre", "octubre", "novembre", "desembre",
]
# Abreviatures que fa servir escapadaambnens ("jun", "jul"...). Map a número de mes.
_MONTHS_CA_ABBR = {
    "gen": 1, "febr": 2, "feb": 2, "març": 3, "mar": 3, "abr": 4, "mai": 5,
    "jun": 6, "jul": 7, "ag": 8, "ago": 8, "set": 9, "oct": 10, "nov": 11,
    "des": 12, "dec": 12,
}

SYSTEM_PROMPT = (
    "Ets qui dinamitza r/ExplorantCatalunya, una comunitat catalana per descobrir "
    "activitats, excursions i escapades en família. Escrius en català natural, "
    "proper i engrescador, sense floritures ni farciment. Vas al gra, fas servir "
    "Markdown amb mesura i convides la gent a fer plans. No t'inventis dades que no "
    "tinguis. No facis servir mai el guió llarg (—)."
)


@dataclass
class Fitxa:
    """Una activitat/escapada trobada en una font."""
    source_key: str
    source_name: str
    source_web: str        # web de la font (per al peu)
    title: str
    url: str               # enllaç a l'article/fitxa original
    summary: str           # text original (DeepSeek el reescriu després)
    image_url: str         # foto (es re-allotja a R2)
    when: str = ""         # data/quan en text (opcional)
    where: str = ""        # població/lloc (opcional)

    def key(self) -> str:
        return f"{self.source_key}|{self.url}"


# --------------------------------------------------------------------------- #
# Utilitats de xarxa                                                           #
# --------------------------------------------------------------------------- #
def _get_soup(url: str) -> BeautifulSoup | None:
    try:
        r = requests.get(url, headers=config.HTTP_HEADERS, timeout=config.REQUEST_TIMEOUT)
        r.raise_for_status()
        return BeautifulSoup(r.text, "lxml")
    except Exception as exc:  # noqa: BLE001
        log.warning("No s'ha pogut descarregar %s: %s", url, exc)
        return None


def _clean_summary(text: str) -> str:
    """Talla les cues de WordPress («Read More», «L'entrada … ha aparegut primer a …»)."""
    for marker in ("Read More", " La entrada ", " L'entrada ", " The post ",
                   "aparece primero", "ha aparegut primer", "appeared first"):
        i = text.find(marker)
        if i != -1:
            text = text[:i]
    return text.strip(" …·-").strip()


def _og_image(url: str) -> str:
    soup = _get_soup(url)
    if not soup:
        return ""
    m = soup.find("meta", property="og:image") or soup.find("meta", attrs={"name": "og:image"})
    return (m.get("content") or "").strip() if m else ""


def _og_description(url: str) -> str:
    """Descripció de l'article (og:description o meta description) per enriquir."""
    soup = _get_soup(url)
    if not soup:
        return ""
    for attrs in (("property", "og:description"), ("name", "description")):
        m = soup.find("meta", attrs={attrs[0]: attrs[1]})
        if m and m.get("content"):
            return m["content"].strip()
    return ""


def _entry_image(entry, og_fallback: bool) -> str:
    """Treu la millor imatge d'una entrada RSS (evita emojis/ploma de WordPress)."""
    def _ok(u: str) -> bool:
        return bool(u) and "s.w.org" not in u and "/emoji/" not in u
    for m in (entry.get("media_content") or []) + (entry.get("media_thumbnail") or []):
        if _ok(m.get("url", "")):
            return m["url"]
    for enc in entry.get("enclosures") or []:
        if "image" in enc.get("type", "") and _ok(enc.get("href", "")):
            return enc["href"]
    html = ""
    if entry.get("content"):
        html = entry["content"][0].get("value", "")
    html = html or entry.get("summary", "")
    img = BeautifulSoup(html, "lxml").find("img")
    if img and _ok(img.get("src", "")):
        return img["src"]
    if og_fallback and entry.get("link"):
        return _og_image(entry["link"])
    return ""


def _parse_rss(feed_url: str, key: str, name: str, web: str,
               *, og_image: bool = False, limit: int = 8) -> list[Fitxa]:
    """Parser genèric d'un feed RSS de WordPress → list[Fitxa]."""
    try:
        raw = requests.get(feed_url, headers=config.HTTP_HEADERS,
                           timeout=config.REQUEST_TIMEOUT)
        d = feedparser.parse(raw.content)
    except Exception as exc:  # noqa: BLE001
        log.warning("RSS %s ha fallat: %s", feed_url, exc)
        return []
    out: list[Fitxa] = []
    for e in d.entries[:limit]:
        summary = BeautifulSoup(e.get("summary", ""), "lxml").get_text(" ", strip=True)
        summary = _clean_summary(summary)
        out.append(Fitxa(
            source_key=key, source_name=name, source_web=web,
            title=(e.get("title") or "").strip(),
            url=(e.get("link") or "").strip(),
            summary=summary,
            image_url=_entry_image(e, og_image),
        ))
    log.info("%s: %d entrades del feed.", key, len(out))
    return out


# --------------------------------------------------------------------------- #
# Fonts — parsers (un per web). Implementades vs stubs (pendents).             #
# --------------------------------------------------------------------------- #
def parse_escapadaambnens(_today: date) -> list[Fitxa]:
    """Festes i fires familiars del MES QUE VE (escapadaambnens.com)."""
    web = "https://www.escapadaambnens.com"
    soup = _get_soup(f"{web}/festivals-familiars/")
    if not soup:
        return []

    # Mes objectiu = el mes vinent respecte d'avui.
    nm_year = _today.year + (_today.month // 12)
    nm_month = (_today.month % 12) + 1

    fitxes: list[Fitxa] = []
    for a in soup.select("a.item_festival"):
        href = a.get("href", "")
        if "/festival/" not in href:
            continue
        info = a.select_one(".info")
        parts = [t.strip() for t in info.stripped_strings] if info else []
        # parts ≈ [població, títol, "Del 19 al 23 de jun de 2026"]
        where = parts[0] if len(parts) >= 3 else ""
        title = (a.get("title") or (parts[1] if len(parts) >= 2 else "")).strip()
        when = parts[-1] if parts else ""
        m = re.search(r"de\s+([a-zç]+)\s+de\s+(\d{4})", when.lower())
        if not m:
            continue
        month = _MONTHS_CA_ABBR.get(m.group(1))
        year = int(m.group(2))
        if month != nm_month or year != nm_year:
            continue
        img = a.select_one("img")
        fitxes.append(Fitxa(
            source_key="escapadaambnens",
            source_name="Escapada amb nens",
            source_web=web,
            title=title.replace(" en familia", "").strip(),
            url=href if href.startswith("http") else web + href,
            summary=f"{title}. {where}. {when}.",
            image_url=img.get("src", "") if img else "",
            when=when,
            where=where,
        ))
    log.info("escapadaambnens: %d festes del mes que ve (%02d/%d).",
             len(fitxes), nm_month, nm_year)
    return fitxes


def _parse_wpjson(base: str, key: str, name: str, web: str, limit: int = 6) -> list[Fitxa]:
    """Parser genèric via l'API REST de WordPress (wp-json), amb imatge destacada."""
    headers = {**config.HTTP_HEADERS, "Accept": "application/json"}
    url = f"{base}/wp-json/wp/v2/posts?per_page={limit}&_embed=1"
    try:
        r = requests.get(url, headers=headers, timeout=config.REQUEST_TIMEOUT)
        posts = r.json() if "json" in r.headers.get("Content-Type", "") else []
    except Exception as exc:  # noqa: BLE001
        log.warning("wp-json %s ha fallat: %s", base, exc)
        return []
    out: list[Fitxa] = []
    for p in posts:
        title = BeautifulSoup(p.get("title", {}).get("rendered", ""), "lxml").get_text(strip=True)
        excerpt = _clean_summary(
            BeautifulSoup(p.get("excerpt", {}).get("rendered", ""), "lxml").get_text(" ", strip=True))
        media = (p.get("_embedded", {}) or {}).get("wp:featuredmedia", [])
        img = media[0].get("source_url", "") if media and isinstance(media, list) else ""
        if title and img:
            out.append(Fitxa(key, name, web, title, p.get("link", ""), excerpt, img))
    log.info("%s: %d posts (wp-json).", key, len(out))
    return out


def parse_elmonensespera(_today: date) -> list[Fitxa]:
    """Escapades/viatges en família (API REST de WordPress; el feed RSS està bloquejat)."""
    return _parse_wpjson("https://elmonensespera.com", "elmonensespera",
                         "El món ens espera", "https://elmonensespera.com")


def parse_sortirambnens(_today: date) -> list[Fitxa]:
    """Excursions amb nens (feed de la categoria, amb imatge al feed)."""
    return _parse_rss(
        "https://www.sortirambnens.com/excursions-amb-nens/feed/",
        "sortirambnens", "Sortir amb nens", "https://www.sortirambnens.com")


def parse_dexcursio(_today: date) -> list[Fitxa]:
    """Excursions (feed general; imatge real via og:image de l'article)."""
    return _parse_rss(
        "https://dexcursio.net/feed/",
        "dexcursio", "D'excursió", "https://dexcursio.net", og_image=True)


def _img_src(node) -> str:
    img = node.find("img") if node else None
    if not img:
        return ""
    src = (img.get("src") or img.get("data-src") or "").strip()
    return src if src.startswith("http") else ""


def parse_surtdecasa(_today: date) -> list[Fitxa]:
    """Agenda del cap de setmana (HTML; cada `.views-row` és un esdeveniment)."""
    web = "https://surtdecasa.cat"
    soup = _get_soup(f"{web}/agenda/cap-de-setmana")
    if not soup:
        return []
    out: list[Fitxa] = []
    for row in soup.select(".views-row"):
        a = row.find("a", href=True)
        title = ""
        for h in row.find_all(["h3", "h2"]):
            t = h.get_text(" ", strip=True)
            if t and not t.lower().startswith("foto"):
                title = t
                break
        src = _img_src(row)
        if a and title and src:
            out.append(Fitxa("surtdecasa", "Surt de casa", web, title,
                             urljoin(web, a["href"]), title, src))
    log.info("surtdecasa: %d esdeveniments.", len(out))
    return out


def parse_timeout(_today: date) -> list[Fitxa]:
    """Què fer a Barcelona (HTML; targetes `article`)."""
    web = "https://www.timeout.cat"
    soup = _get_soup(f"{web}/barcelona/ca/que-fer")
    if not soup:
        return []
    out: list[Fitxa] = []
    for art in soup.select("article"):
        h = art.find(["h3", "h2"])
        a = art.find("a", href=True)
        src = _img_src(art)
        if h and a and src:
            title = h.get_text(" ", strip=True)
            if title:
                out.append(Fitxa("timeout", "Time Out Barcelona", web, title,
                                 urljoin(web, a["href"]), title, src))
    log.info("timeout: %d propostes.", len(out))
    return out


def parse_barcelona_nens(_today: date) -> list[Fitxa]:
    """Cap de setmana amb nens (HTML; targetes `article`)."""
    web = "https://www.barcelona.cat"
    soup = _get_soup(f"{web}/capdesetmana/ca/nens-i-nenes")
    if not soup:
        return []
    out: list[Fitxa] = []
    for art in soup.select("article"):
        a = art.find("a", href=True)
        src = _img_src(art)
        if a and src:
            title = a.get_text(" ", strip=True)
            if title:
                out.append(Fitxa("barcelona_nens", "Barcelona.cat", web, title,
                                 urljoin(web, a["href"]), title, src))
    log.info("barcelona_nens: %d activitats.", len(out))
    return out


def _parser_pendent(key: str):
    """Crea un stub de parser que registra que la font encara no està feta."""
    def _stub(_today: date) -> list[Fitxa]:
        log.warning("Font «%s»: parser encara PENDENT (stub).", key)
        return []
    return _stub


# Registre de fonts: clau → (nom, web, parser).
SOURCES: dict[str, dict] = {
    "escapadaambnens": {
        "name": "Escapada amb nens", "web": "https://www.escapadaambnens.com",
        "parse": parse_escapadaambnens,
    },
    "elmonensespera": {
        "name": "El món ens espera", "web": "https://elmonensespera.com",
        "parse": parse_elmonensespera,
    },
    "sortirambnens": {
        "name": "Sortir amb nens", "web": "https://www.sortirambnens.com",
        "parse": parse_sortirambnens,
    },
    "senders_feec": {
        "name": "Senders FEEC", "web": "https://senders.feec.cat",
        "parse": _parser_pendent("senders_feec"),
    },
    "dexcursio": {
        "name": "D'excursió", "web": "https://dexcursio.net",
        "parse": parse_dexcursio,
    },
    "timeout": {
        "name": "Time Out Barcelona", "web": "https://www.timeout.cat",
        "parse": parse_timeout,
    },
    "surtdecasa": {
        "name": "Surt de casa", "web": "https://surtdecasa.cat",
        "parse": parse_surtdecasa,
    },
    "barcelona_nens": {
        "name": "Barcelona.cat (cap de setmana)", "web": "https://www.barcelona.cat",
        "parse": parse_barcelona_nens,
    },
    "femturisme": {
        "name": "Fem Turisme", "web": "https://femturisme.cat",
        "parse": _parser_pendent("femturisme"),
    },
}


# --------------------------------------------------------------------------- #
# Calendari: quines fonts toquen avui                                          #
# --------------------------------------------------------------------------- #
def sources_due(d: date) -> list[str]:
    """Claus de les fonts que toca publicar avui segons el calendari."""
    dow = d.weekday()              # 0=dilluns ... 6=diumenge
    week_of_month = (d.day - 1) // 7 + 1
    due: list[str] = []
    if dow == 1:
        due.append("elmonensespera")     # dimarts
    if dow == 2:
        due.append("sortirambnens")      # dimecres
    if dow == 3:
        due.append("surtdecasa")         # dijous
    if dow == 4:
        due.append("femturisme")         # divendres
    if dow == 5:
        due.append("barcelona_nens")     # dissabte
    if dow == 0 and week_of_month == 1:
        due.append("senders_feec")       # 1r dilluns
    if dow == 0 and week_of_month == 2:
        due.append("dexcursio")          # 2n dilluns
    if dow == 0 and week_of_month == 3:
        due.append("timeout")            # 3r dilluns
    if d.day == 1:
        due.append("escapadaambnens")    # dia 1: festes del mes que ve
    return due


# --------------------------------------------------------------------------- #
# Històric (dedup)                                                             #
# --------------------------------------------------------------------------- #
def _load_history() -> set[str]:
    try:
        data = json.loads(config.EXPLORANT_HISTORY_FILE.read_text(encoding="utf-8"))
        return set(data.get("posted", []))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return set()


def _save_history(posted: set[str]) -> None:
    config.EXPLORANT_HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    config.EXPLORANT_HISTORY_FILE.write_text(
        json.dumps({"posted": sorted(posted)}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# --------------------------------------------------------------------------- #
# Resum (DeepSeek) + imatge (R2)                                              #
# --------------------------------------------------------------------------- #
def build_comment(f: Fitxa, use_llm: bool = True) -> str:
    """Primer comentari: resum en català + enllaç a la font al peu (cursiva)."""
    peu = f"\n\n*Font: [{f.source_name}]({f.url})*"
    cos = f.summary
    if use_llm:
        try:
            from processor import _deepseek_chat
            ctx = f"Títol: {f.title}\n"
            if f.where:
                ctx += f"Lloc: {f.where}\n"
            if f.when:
                ctx += f"Quan: {f.when}\n"
            user = (
                "Escriu una descripció breu i engrescadora en català (2-3 frases, "
                "pots fer servir 1-2 emojis i alguna negreta) per convidar famílies "
                "a aquesta activitat/escapada. No inventis detalls que no et dono; "
                "si en falten, queda't en el to i convida a mirar la font. No posis "
                f"títol ni encapçalament.\n\n{ctx}"
            )
            out = _deepseek_chat(
                [{"role": "system", "content": SYSTEM_PROMPT},
                 {"role": "user", "content": user}],
                temperature=0.7, max_tokens=350,
            )
            if out and out.strip():
                cos = out.strip()
        except Exception as exc:  # noqa: BLE001
            log.warning("DeepSeek ha fallat, resum cru: %s", exc)
    return cos + peu


def rehost_image(f: Fitxa) -> str:
    """Baixa la foto de la font i la re-allotja a R2; retorna la URL pública."""
    import r2_upload
    r = requests.get(f.image_url, headers=config.HTTP_HEADERS, timeout=config.REQUEST_TIMEOUT)
    r.raise_for_status()
    data = r.content
    ctype = r.headers.get("Content-Type", "image/jpeg").split(";")[0].strip()
    ext = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp",
           "image/gif": "gif"}.get(ctype, "jpg")
    # Reddit pot rebutjar WebP en posts d'imatge: converteix a JPG (Pillow ve amb
    # matplotlib). Si falla, es manté el WebP original.
    if ext == "webp":
        try:
            import io
            from PIL import Image
            im = Image.open(io.BytesIO(data)).convert("RGB")
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=88)
            data, ext, ctype = buf.getvalue(), "jpg", "image/jpeg"
        except Exception as exc:  # noqa: BLE001
            log.warning("No s'ha pogut convertir WebP a JPG: %s", exc)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    key = f"explorant/{f.source_key}/{stamp}.{ext}"
    return r2_upload.upload_bytes(data, key, content_type=ctype)


# --------------------------------------------------------------------------- #
# Selecció + payload                                                           #
# --------------------------------------------------------------------------- #
def pick_fitxa(source_key: str, today: date, posted: set[str]) -> Fitxa | None:
    """Primera fitxa d'aquesta font que encara no s'ha publicat (amb imatge)."""
    src = SOURCES.get(source_key)
    if not src:
        log.warning("Font desconeguda: %s", source_key)
        return None
    try:
        fitxes = src["parse"](today)
    except Exception as exc:  # noqa: BLE001
        log.warning("Font «%s» ha fallat en parsejar: %s", source_key, exc)
        return None
    for f in fitxes:
        if f.image_url and f.title and f.key() not in posted:
            return f
    return None


def build_payload(f: Fitxa, image_url: str, comment: str) -> dict:
    emoji = "🗺️"
    title = f"{emoji} {f.title}"
    if f.where and f.where.lower() not in f.title.lower():
        title += f" ({f.where})"
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "tipus": "imatge",
        "title": title[:300],
        "subreddit": config.EXPLORANT_SUBREDDIT,
        "url": image_url,
        "comment_markdown": comment,
        "source": "explorant",
        "source_label": "Explorant Catalunya",
    }


# --------------------------------------------------------------------------- #
# CLI                                                                          #
# --------------------------------------------------------------------------- #
def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Pack «Explorant Catalunya».")
    p.add_argument("--post", action="store_true", help="Preview (no encua res).")
    p.add_argument("--push", action="store_true", help="Publica (re-allotja imatge + encua).")
    p.add_argument("--source", help="Força una font concreta (clau de SOURCES).")
    p.add_argument("--no-llm", action="store_true", help="Sense DeepSeek (resum cru).")
    p.add_argument("--quiet", action="store_true", help="Menys missatges.")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=logging.WARNING if args.quiet else logging.INFO,
                        format="%(message)s")
    use_llm = config.USE_LLM and not args.no_llm
    today = date.today()

    keys = [args.source] if args.source else sources_due(today)
    if not keys:
        print("Avui no toca cap font. (Calendari a sources_due.)")
        return 0

    posted = _load_history()
    any_done = False
    for key in keys:
        f = pick_fitxa(key, today, posted)
        if not f:
            log.info("Font «%s»: res de nou per publicar.", key)
            continue
        # Resum prim (només el títol)? Enriqueix amb l'og:description de l'article.
        if len(f.summary) < len(f.title) + 25:
            desc = _og_description(f.url)
            if desc:
                f.summary = desc
        comment = build_comment(f, use_llm=use_llm)

        if not args.push:
            print("=" * 70)
            print(f"[{f.source_name}] {f.title}")
            print(f"IMATGE: {f.image_url}")
            print("-" * 70)
            print(comment)
            any_done = True
            continue

        if not _HAS_QUEUE:
            print("❌ queue_store no disponible.", file=sys.stderr)
            return 1
        try:
            image_url = rehost_image(f)
        except Exception as exc:  # noqa: BLE001
            log.warning("Font «%s»: no s'ha pogut re-allotjar la imatge: %s", key, exc)
            continue
        payload = build_payload(f, image_url, comment)
        item_id = queue_store.enqueue(payload)
        posted.add(f.key())
        print(f"✅ Encuat [{f.source_name}]: {f.title} → {item_id}")
        any_done = True

    if args.push:
        _save_history(posted)
    if not any_done:
        print("Res a publicar avui.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
