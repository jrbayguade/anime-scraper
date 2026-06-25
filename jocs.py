"""
jocs.py — Pack «Jocs en català»: notícies i videojocs en català per a r/jocs.

Setena sortida (autònoma). Agrupa quatre fonts de contingut sobre videojocs en
català, cadascuna amb el seu calendari (decidit per `sources_due`). Un sol cron
diari (dl + dt) n'hi ha prou.

Calendari:
    1r dilluns de mes  → noujoc.com (darrer article de notícies de videojocs)
    2n dilluns de mes  → 3dnassos.cat (darrera notícia)
    3r dilluns de mes  → podcast Generació Digital (3cat.cat) — post d'imatge
    dimarts            → videojoc aleatori de la llista VDJOC (llengua.gencat.cat) — post d'imatge

Posts de text (noujoc, 3dnassos): resum adaptat per Reddit + peu «via font.cat».
Posts d'imatge (generacio_digital, videojoc_setmana): foto re-allotjada a R2 +
primer comentari amb descripció i enlaces.

Ús:
    python jocs.py --post                      # preview del que toca avui
    python jocs.py --source videojoc_setmana   # força una font concreta
    python jocs.py --push                      # publica (encua al Worker)
    python jocs.py --no-llm --post             # sense DeepSeek (text cru)
    python jocs.py --diagnose                  # diagnòstic de les fonts
"""
from __future__ import annotations

import argparse
import json
import logging
import random
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime

import feedparser
import requests
from bs4 import BeautifulSoup

import config

log = logging.getLogger("anime-scraper.jocs")

try:
    import queue_store
    _HAS_QUEUE = True
except Exception:  # pragma: no cover
    _HAS_QUEUE = False

SYSTEM_PROMPT = (
    "Ets qui dinamitza r/jocs, una comunitat catalana de videojocs. Escrius en "
    "català natural, directe i engrescador, adequat per a Reddit. Vas al gra, "
    "fas servir Markdown amb mesura i t'adaptes al to del contingut (notícies, "
    "podcast, jocs). No t'inventis dades que no et dono. "
    "No facis servir mai el guió llarg (—)."
)

_SPOTIFY_GENERACIO = "https://open.spotify.com/show/2FlKeeRVemGs6YU3HAXiRW"
_GENERACIO_3CAT_URL = "https://www.3cat.cat/3cat/generacio-digital/"
_VDJOC_URL = "https://llengua.gencat.cat/ca/serveis/videojocs/"
_GENCAT_BASE = "https://llengua.gencat.cat"

# Mapa d'icones de botiga (nom del fitxer png → etiqueta visible).
_STORE_LABELS: dict[str, str] = {
    "steam": "Steam",
    "ps": "PlayStation Store",
    "nintendoeshop": "Nintendo eShop",
    "microsoftstore": "Microsoft Store",
    "itchio": "itch.io",
    "googleplay": "Google Play",
    "appstore": "App Store",
    "gog": "GOG",
    "epic": "Epic Games Store",
    "amazon": "Amazon",
}


# ---------------------------------------------------------------------------
# Fitxa: unitat de contingut
# ---------------------------------------------------------------------------
@dataclass
class Fitxa:
    """Un contingut (article, episodi o joc) a publicar a r/jocs."""
    source_key: str
    source_name: str
    source_web: str
    title: str
    url: str          # URL de l'article/joc/episodi original
    summary: str      # text pla (DeepSeek el pot reescriure)
    tipus: str        # "text" | "imatge"
    image_url: str = ""         # URL de la imatge (posts d'imatge; es re-allotja a R2)
    via_label: str = ""         # peu per a posts de text («via noujoc.com»)
    platforms: str = ""         # plataformes del joc (videojoc_setmana)
    store_links: list = field(default_factory=list)  # list[tuple[str,str]]

    def key(self) -> str:
        return f"{self.source_key}|{self.url}"


# ---------------------------------------------------------------------------
# Utilitats de xarxa
# ---------------------------------------------------------------------------
def _get_soup(url: str) -> BeautifulSoup | None:
    try:
        r = requests.get(url, headers=config.HTTP_HEADERS, timeout=config.REQUEST_TIMEOUT)
        r.raise_for_status()
        return BeautifulSoup(r.text, "lxml")
    except Exception as exc:  # noqa: BLE001
        log.warning("No s'ha pogut descarregar %s: %s", url, exc)
        return None


def _clean_summary(text: str) -> str:
    """Talla cues de WordPress («Read More», peu de post…)."""
    for marker in ("Read More", " La entrada ", " L'entrada ", " The post ",
                   "aparece primero", "ha aparegut primer", "appeared first"):
        i = text.find(marker)
        if i != -1:
            text = text[:i]
    return text.strip(" …·-").strip()


def _og_description(url: str) -> str:
    """og:description o meta description de l'article, per enriquir el resum."""
    soup = _get_soup(url)
    if not soup:
        return ""
    for attrs in (("property", "og:description"), ("name", "description")):
        m = soup.find("meta", attrs={attrs[0]: attrs[1]})
        if m and m.get("content"):
            return m["content"].strip()
    return ""


# ---------------------------------------------------------------------------
# Parsers — un per font
# ---------------------------------------------------------------------------
def parse_noujoc(_today: date) -> list[Fitxa]:
    """Darrers articles de noujoc.com (feed RSS de WordPress)."""
    try:
        raw = requests.get("https://noujoc.com/feed/",
                           headers=config.HTTP_HEADERS, timeout=config.REQUEST_TIMEOUT)
        d = feedparser.parse(raw.content)
    except Exception as exc:  # noqa: BLE001
        log.warning("noujoc RSS ha fallat: %s", exc)
        return []
    out: list[Fitxa] = []
    for e in d.entries[:8]:
        summary = BeautifulSoup(e.get("summary", ""), "lxml").get_text(" ", strip=True)
        summary = _clean_summary(summary)
        out.append(Fitxa(
            source_key="noujoc",
            source_name="Nou Joc",
            source_web="https://noujoc.com",
            title=(e.get("title") or "").strip(),
            url=(e.get("link") or "").strip(),
            summary=summary,
            tipus="text",
            via_label="noujoc.com",
        ))
    log.info("noujoc: %d articles del feed.", len(out))
    return out


def parse_3dnassos(_today: date) -> list[Fitxa]:
    """Darreres notícies de 3dnassos.cat (API REST de WordPress; el feed RSS dóna 403).

    NOTA: el Chrome UA retorna 403; es fa servir un UA mínim.
    """
    # El User-Agent de Chrome és bloquejat per 3dnassos.cat; cal un UA mínim.
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
    try:
        r = requests.get(
            "https://3dnassos.cat/wp-json/wp/v2/posts?per_page=8&_embed=1",
            headers=headers, timeout=config.REQUEST_TIMEOUT)
        posts = r.json() if "json" in r.headers.get("Content-Type", "") else []
    except Exception as exc:  # noqa: BLE001
        log.warning("3dnassos wp-json ha fallat: %s", exc)
        return []
    out: list[Fitxa] = []
    for p in posts:
        if not isinstance(p, dict):
            continue
        title = BeautifulSoup(
            p.get("title", {}).get("rendered", ""), "lxml").get_text(strip=True)
        excerpt = _clean_summary(BeautifulSoup(
            p.get("excerpt", {}).get("rendered", ""), "lxml").get_text(" ", strip=True))
        if title:
            out.append(Fitxa(
                source_key="3dnassos",
                source_name="3Dnassos",
                source_web="https://3dnassos.cat",
                title=title,
                url=p.get("link", "").strip(),
                summary=excerpt,
                tipus="text",
                via_label="3dnassos.cat",
            ))
    log.info("3dnassos: %d articles (wp-json).", len(out))
    return out


def parse_generacio_digital(_today: date) -> list[Fitxa]:
    """Darrers episodis del podcast Generació Digital (API pública de 3cat.cat)."""
    api_url = (
        "https://api.3cat.cat/audios"
        "?_format=json&programaradio_id=925&pagina=1&items_pagina=8"
        "&sdom=img&version=2.0&cache=90&master=yes"
    )
    try:
        r = requests.get(api_url, headers=config.HTTP_HEADERS, timeout=config.REQUEST_TIMEOUT)
        r.raise_for_status()
        items = r.json()["resposta"]["items"].get("item", [])
    except Exception as exc:  # noqa: BLE001
        log.warning("Generació Digital API ha fallat: %s", exc)
        return []
    out: list[Fitxa] = []
    for ep in items:
        nom = ep.get("nom_friendly", "")
        audio_id = str(ep.get("audios", [{}])[0].get("id", "")) if ep.get("audios") else ""
        ep_url = (f"{_GENERACIO_3CAT_URL}{nom}/audio/{audio_id}/"
                  if nom and audio_id else _GENERACIO_3CAT_URL)
        # Imatge de l'episodi: prioritat 670x378, fallback MASTER
        img_url = ""
        for img in ep.get("imatges", []):
            if img.get("mida") == "670x378" and img.get("text"):
                img_url = img["text"]
                break
        if not img_url:
            for img in ep.get("imatges", []):
                if img.get("mida") == "MASTER" and img.get("text"):
                    img_url = img["text"]
                    break
        title = ep.get("permatitle") or ep.get("titol") or ""
        out.append(Fitxa(
            source_key="generacio_digital",
            source_name="Generació Digital",
            source_web=_GENERACIO_3CAT_URL,
            title=title.strip(),
            url=ep_url,
            summary=ep.get("entradeta", "").strip(),
            tipus="imatge",
            image_url=img_url,
        ))
    log.info("generacio_digital: %d episodis.", len(out))
    return out


def parse_videojoc_setmana(_today: date) -> list[Fitxa]:
    """Llista completa de videojocs en català de la base de dades VDJOC."""
    soup = _get_soup(_VDJOC_URL)
    if not soup:
        return []
    games: list[Fitxa] = []
    for div in soup.find_all("div", class_="grafic_destacat_cont"):
        a = div.find("a", href=True)
        if not a:
            continue
        href = a.get("href", "")
        if "vdjoc" not in href.lower() and "detalls/article" not in href.lower():
            continue
        title = (a.get("title") or "").strip()
        if not title:
            p_txt = div.find("p", class_="imatge_text")
            title = p_txt.get_text(" ", strip=True) if p_txt else ""
        url = href if href.startswith("http") else f"{_GENCAT_BASE}{href}"
        # Miniatura del llistat (fallback si la pàgina de detall no té allargat)
        thumb_src = ""
        img_el = div.find("img")
        if img_el:
            s = img_el.get("src", "")
            thumb_src = (f"{_GENCAT_BASE}{s}" if s.startswith("/") else s)
        games.append(Fitxa(
            source_key="videojoc_setmana",
            source_name="VDJOC – Llengua Catalana",
            source_web=_VDJOC_URL,
            title=title,
            url=url,
            summary="",
            tipus="imatge",
            image_url=thumb_src,
        ))
    log.info("videojoc_setmana: %d jocs a la llista.", len(games))
    return games


def _fetch_videojoc_detail(f: Fitxa) -> Fitxa:
    """Visita la pàgina de detall d'un joc i n'extreu descripció, plataformes, botigues i foto."""
    soup = _get_soup(f.url)
    if not soup:
        return f
    # Descripció: busquem dins <article> per evitar el nav i els modals de la capçalera.
    container = soup.find("article") or soup
    for p in container.find_all("p"):
        cl = p.get("class") or []
        if any(c in cl for c in ("films_data", "modal-title")):
            continue
        t = p.get_text(" ", strip=True)
        if len(t) > 40:
            f.summary = t
            break
    # Camps estructurats (p.films_data amb strong label)
    for p in soup.find_all("p", class_="films_data"):
        strong = p.find("strong")
        if not strong:
            continue
        lbl = strong.get_text(strip=True)
        if lbl == "Plataformes":
            full = p.get_text(" ", strip=True)
            if ":" in full:
                f.platforms = full.split(":", 1)[1].strip().rstrip(".")
        elif lbl == "Botigues":
            for a in p.find_all("a", href=True):
                href = a.get("href", "")
                if not href:
                    continue
                img_tag = a.find("img")
                icon = ""
                if img_tag:
                    m = re.search(r"/icones/(\w+)\.png", img_tag.get("src", ""))
                    icon = m.group(1).lower() if m else ""
                lbl_store = _STORE_LABELS.get(icon, icon.capitalize() or "Botiga")
                f.store_links.append((lbl_store, href))
    # Foto de portada (-allargat.jpg, sense hash; millor que la miniatura del llistat)
    for img in soup.find_all("img"):
        src = img.get("src", "")
        if "videojocs/imatges" in src and "allargat" in src and not src.endswith(".svg"):
            f.image_url = f"{_GENCAT_BASE}{src}" if src.startswith("/") else src
            break
    return f


# ---------------------------------------------------------------------------
# Calendari: quines fonts toquen avui
# ---------------------------------------------------------------------------
SOURCES: dict[str, dict] = {
    "noujoc": {
        "name": "Nou Joc", "web": "https://noujoc.com", "parse": parse_noujoc,
    },
    "3dnassos": {
        "name": "3Dnassos", "web": "https://3dnassos.cat", "parse": parse_3dnassos,
    },
    "generacio_digital": {
        "name": "Generació Digital", "web": _GENERACIO_3CAT_URL,
        "parse": parse_generacio_digital,
    },
    "videojoc_setmana": {
        "name": "VDJOC – Llengua Catalana", "web": _VDJOC_URL,
        "parse": parse_videojoc_setmana,
    },
}


def sources_due(d: date) -> list[str]:
    """Claus de les fonts que toca publicar avui."""
    dow = d.weekday()             # 0=dilluns … 6=diumenge
    week = (d.day - 1) // 7 + 1  # setmana del mes (1–5)
    due: list[str] = []
    if dow == 0 and week == 1:
        due.append("noujoc")
    if dow == 0 and week == 2:
        due.append("3dnassos")
    if dow == 0 and week == 3:
        due.append("generacio_digital")
    if dow == 1:
        due.append("videojoc_setmana")
    return due


# ---------------------------------------------------------------------------
# Històric (dedup)
# ---------------------------------------------------------------------------
def _load_history() -> set[str]:
    try:
        data = json.loads(config.JOCS_HISTORY_FILE.read_text(encoding="utf-8"))
        return set(data.get("posted", []))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return set()


def _save_history(posted: set[str]) -> None:
    config.JOCS_HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    config.JOCS_HISTORY_FILE.write_text(
        json.dumps({"posted": sorted(posted)}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Construcció del contingut (DeepSeek + imatge a R2)
# ---------------------------------------------------------------------------
def _deepseek(system: str, user: str, *, temperature: float = 0.6,
              max_tokens: int = 500) -> str | None:
    try:
        from processor import _deepseek_chat
        out = _deepseek_chat(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            temperature=temperature, max_tokens=max_tokens,
        )
        return out.strip() if out and out.strip() else None
    except Exception as exc:  # noqa: BLE001
        log.warning("DeepSeek ha fallat: %s", exc)
        return None


def build_text_body(f: Fitxa, use_llm: bool) -> str:
    """Cos del post de text: resum adaptat per Reddit + peu discret «via …»."""
    via = f"\n\n*(via [{f.via_label}]({f.url}))*"
    cos = f.summary
    if use_llm and f.summary:
        out = _deepseek(
            SYSTEM_PROMPT,
            "Escriu un post de Reddit en català (2-3 paràgrafs, to directe i engrescador) "
            "que resumeixi/adapti aquest contingut per a la comunitat r/jocs. "
            "No posis títol ni encapçalament. No inventis dades que no et dono.\n\n"
            f"Títol: {f.title}\n\n{f.summary}",
        )
        if out:
            cos = out
    return cos + via


def build_comment(f: Fitxa, use_llm: bool) -> str:
    """Primer comentari per als posts d'imatge (generacio_digital, videojoc_setmana)."""
    if f.source_key == "generacio_digital":
        return _comment_generacio(f, use_llm)
    if f.source_key == "videojoc_setmana":
        return _comment_videojoc(f, use_llm)
    return f.summary


def _comment_generacio(f: Fitxa, use_llm: bool) -> str:
    desc = f.summary
    if use_llm and f.summary:
        out = _deepseek(
            SYSTEM_PROMPT,
            "Escriu una breu presentació (2-3 frases) en català per a Reddit "
            "que desperti les ganes d'escoltar aquest episodi de podcast. "
            "No poses títol. No inventis dades.\n\n"
            f"Títol: {f.title}\n\n{f.summary}",
            temperature=0.7, max_tokens=300,
        )
        if out:
            desc = out
    links = f"[Escolta a 3Cat]({f.url}) · [Spotify]({_SPOTIFY_GENERACIO})"
    return f"{desc}\n\n{links}"


def _comment_videojoc(f: Fitxa, use_llm: bool) -> str:
    desc = f.summary
    if use_llm and f.summary:
        ctx = f"Títol: {f.title}\n"
        if f.platforms:
            ctx += f"Plataformes: {f.platforms}\n"
        ctx += f"\n{f.summary}"
        out = _deepseek(
            SYSTEM_PROMPT,
            "Escriu una descripció breu i engrescadora en català (2-3 frases) "
            "d'aquest videojoc per a Reddit. No poses títol. "
            "No inventis dades que no et dono.\n\n" + ctx,
            temperature=0.7, max_tokens=300,
        )
        if out:
            desc = out
    parts: list[str] = []
    if f.platforms:
        parts.append(f"**Plataformes:** {f.platforms}")
    if f.store_links:
        store_str = " · ".join(f"[{lbl}]({url})" for lbl, url in f.store_links)
        parts.append(f"**On trobar-lo:** {store_str}")
    parts.append(f"*(via [VDJOC – Llengua Catalana]({f.url}))*")
    return desc + "\n\n" + "\n\n".join(parts)


def rehost_image(f: Fitxa) -> str:
    """Baixa la foto i la re-allotja a Cloudflare R2; retorna la URL pública."""
    import io
    import r2_upload
    r = requests.get(f.image_url, headers=config.HTTP_HEADERS, timeout=config.REQUEST_TIMEOUT)
    r.raise_for_status()
    data = r.content
    ctype = r.headers.get("Content-Type", "image/jpeg").split(";")[0].strip()
    ext = {"image/jpeg": "jpg", "image/png": "png",
           "image/webp": "webp", "image/gif": "gif"}.get(ctype, "jpg")
    # Reddit pot rebutjar WebP: converteix a JPG si cal.
    if ext == "webp":
        try:
            from PIL import Image
            im = Image.open(io.BytesIO(data)).convert("RGB")
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=88)
            data, ext, ctype = buf.getvalue(), "jpg", "image/jpeg"
        except Exception as exc:  # noqa: BLE001
            log.warning("No s'ha pogut convertir WebP a JPG: %s", exc)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    key = f"jocs/{f.source_key}/{stamp}.{ext}"
    return r2_upload.upload_bytes(data, key, content_type=ctype)


# ---------------------------------------------------------------------------
# Selecció + payload
# ---------------------------------------------------------------------------
def pick_fitxa(source_key: str, today: date, posted: set[str]) -> Fitxa | None:
    """Tria la fitxa a publicar per a una font (primera nova, o aleatòria per a videojoc)."""
    src = SOURCES.get(source_key)
    if not src:
        log.warning("Font desconeguda: %s", source_key)
        return None
    try:
        fitxes = src["parse"](today)
    except Exception as exc:  # noqa: BLE001
        log.warning("Font «%s» ha fallat en parsejar: %s", source_key, exc)
        return None

    if source_key == "videojoc_setmana":
        # Selecció aleatòria entre els no publicats
        candidats = [f for f in fitxes if f.key() not in posted]
        if not candidats:
            log.info("videojoc_setmana: tots els jocs ja s'han publicat!")
            return None
        f = random.choice(candidats)
        return _fetch_videojoc_detail(f)

    # Fonts seqüencials: pren el més recent no publicat
    for f in fitxes:
        if f.key() not in posted:
            # Enriqueix si el resum és molt curt
            if len(f.summary) < len(f.title) + 25:
                desc = _og_description(f.url)
                if desc:
                    f.summary = desc
            return f
    return None


def build_payload(f: Fitxa, use_llm: bool, image_url: str = "") -> dict:
    base: dict = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "subreddit": config.JOCS_SUBREDDIT,
        "source": "jocs",
        "source_label": "Jocs en català",
    }
    if f.tipus == "text":
        base["tipus"] = "text"
        base["title"] = f.title[:300]
        base["markdown"] = build_text_body(f, use_llm)
    else:
        if f.source_key == "videojoc_setmana":
            title = f"El videojoc en català de la setmana: {f.title}"
        elif f.source_key == "generacio_digital":
            title = f"Generació Digital: {f.title}"
        else:
            title = f.title
        base["tipus"] = "imatge"
        base["title"] = title[:300]
        base["url"] = image_url
        base["comment_markdown"] = build_comment(f, use_llm)
    return base


# ---------------------------------------------------------------------------
# Mode diagnòstic
# ---------------------------------------------------------------------------
_PROBE_URL = {
    "noujoc": "https://noujoc.com/feed/",
    "3dnassos": "https://3dnassos.cat/wp-json/wp/v2/posts?per_page=1",
    "generacio_digital": (
        "https://api.3cat.cat/audios?_format=json&programaradio_id=925"
        "&pagina=1&items_pagina=1&sdom=img&version=2.0&cache=90&master=yes"
    ),
    "videojoc_setmana": _VDJOC_URL,
}


def diagnose(keys: list[str]) -> None:
    for key in keys:
        url = _PROBE_URL.get(key, "")
        line = f"[{key}]"
        if url:
            try:
                r = requests.get(url, headers={**config.HTTP_HEADERS, "Accept": "*/*"},
                                 timeout=config.REQUEST_TIMEOUT)
                snippet = r.text[:80].replace("\n", " ").replace("\r", " ")
                line += (f" HTTP {r.status_code} · {r.headers.get('Content-Type','?')[:25]}"
                         f" · {len(r.content)}B · «{snippet}»")
            except Exception as exc:  # noqa: BLE001
                line += f" FETCH ERROR: {exc}"
        try:
            n = len(SOURCES[key]["parse"](date.today()))
            line += f"  →  {n} fitxes"
        except Exception as exc:  # noqa: BLE001
            line += f"  →  PARSER ERROR: {exc}"
        print(line)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Pack «Jocs en català» per a r/jocs.")
    p.add_argument("--post", action="store_true", help="Preview (no encua res).")
    p.add_argument("--push", action="store_true", help="Publica (re-allotja imatge + encua).")
    p.add_argument("--source", help="Força una font concreta (clau de SOURCES).")
    p.add_argument("--all", action="store_true",
                   help="Totes les fonts, ignorant el calendari (per provar).")
    p.add_argument("--diagnose", action="store_true",
                   help="Diagnòstic: resposta de cada font (no encua res).")
    p.add_argument("--no-llm", action="store_true", help="Sense DeepSeek (text cru).")
    p.add_argument("--quiet", action="store_true", help="Menys missatges.")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=logging.WARNING if args.quiet else logging.INFO,
                        format="%(message)s")
    use_llm = config.USE_LLM and not args.no_llm
    today = date.today()

    if args.all or args.diagnose:
        keys = list(SOURCES)
    elif args.source:
        keys = [args.source]
    else:
        keys = sources_due(today)

    if args.diagnose:
        diagnose(keys)
        return 0

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

        if not args.push:
            # Mode preview
            print("=" * 70)
            if f.tipus == "text":
                body = build_text_body(f, use_llm)
                print(f"[{f.source_name}] TÍTOL: {f.title}")
                print("-" * 70)
                print(body)
            else:
                comment = build_comment(f, use_llm)
                if f.source_key == "videojoc_setmana":
                    title = f"El videojoc en català de la setmana: {f.title}"
                elif f.source_key == "generacio_digital":
                    title = f"Generació Digital: {f.title}"
                else:
                    title = f.title
                print(f"[{f.source_name}] TÍTOL: {title}")
                print(f"IMATGE: {f.image_url}")
                print("-" * 70)
                print(comment)
            any_done = True
            continue

        if not _HAS_QUEUE:
            print("❌ queue_store no disponible.", file=sys.stderr)
            return 1

        try:
            image_url = ""
            if f.tipus == "imatge":
                if not f.image_url:
                    log.warning("Font «%s»: sense imatge, es descarta.", key)
                    continue
                image_url = rehost_image(f)
            payload = build_payload(f, use_llm, image_url)
            item_id = queue_store.enqueue(payload)
        except Exception as exc:  # noqa: BLE001
            log.warning("Font «%s»: no s'ha pogut publicar (%s); es continua.", key, exc)
            continue

        posted.add(f.key())
        print(f"✅ Encuat [{f.source_name}]: {payload.get('title','')[:60]} → {item_id}")
        any_done = True

    if args.push:
        _save_history(posted)
    if not any_done:
        print("Res a publicar avui.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
