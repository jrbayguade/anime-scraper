"""
scraper.py — Descàrrega i parsing de les fonts.

Cada font té la seva pròpia funció de parsing registrada a PARSERS. Per afegir
una font nova: afegeix-la a SOURCES (config.py) i escriu una funció
parse_<key>(...) que retorni una llista de NewsItem; després registra-la a
PARSERS.

Disseny pensat per ser robust: si una font falla, es registra l'error al log i
es continua amb les altres. Mai peta tot el procés per culpa d'una sola web.
"""

from __future__ import annotations

import hashlib
import logging
import random
import re
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from typing import Optional

import requests
from bs4 import BeautifulSoup

import config

log = logging.getLogger("anime-scraper.scraper")

# Mesos en català (per parsejar i formatar dates si cal)
_MONTHS_CA = [
    "gener", "febrer", "març", "abril", "maig", "juny",
    "juliol", "agost", "setembre", "octubre", "novembre", "desembre",
]


# --------------------------------------------------------------------------- #
# Model de dades                                                               #
# --------------------------------------------------------------------------- #
@dataclass
class NewsItem:
    source: str            # Nom llegible de la font ("El Racó del Manga")
    source_key: str        # Clau interna ("elracodelmanga")
    source_emoji: str      # Emoji de la font
    title: str
    url: str               # Enllaç complet a la notícia (o pàgina font)
    date: datetime         # Data de publicació
    category: str          # Categoria (o "" si no n'hi ha)
    summary: str           # Resum/extracte ORIGINAL (es traduirà després)
    image_url: str = ""    # URL de la imatge (per penjar a Reddit sense pujar-la)
    lang: str = "ca"       # Idioma original ("ca" / "en")
    summary_ca: str = ""   # Resum final en català (l'omple processor.py)

    def dedupe_key(self) -> str:
        """Clau estable per detectar notícies ja publicades en setmanes anteriors.

        Fem servir font + títol + dia, perquè algunes fonts (El Racó) no tenen
        un enllaç únic per notícia.
        """
        raw = f"{self.source_key}|{self.title.strip().lower()}|{self.date:%Y-%m-%d}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict:
        d = asdict(self)
        d["date"] = self.date.strftime("%Y-%m-%d")
        return d


# --------------------------------------------------------------------------- #
# HTTP educat                                                                  #
# --------------------------------------------------------------------------- #
def build_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(config.HTTP_HEADERS)
    return session


def _sleep_polite() -> None:
    delay = random.uniform(config.DELAY_MIN, config.DELAY_MAX)
    log.debug("Esperant %.1fs abans de la següent petició...", delay)
    time.sleep(delay)


def polite_get(session: requests.Session, url: str, *, sleep: bool = True) -> Optional[requests.Response]:
    """GET amb reintents, timeout i delay. Retorna None si tot falla."""
    url = url.strip()
    for attempt in range(1, config.REQUEST_RETRIES + 2):
        try:
            resp = session.get(url, timeout=config.REQUEST_TIMEOUT)
            resp.raise_for_status()
            if sleep:
                _sleep_polite()
            return resp
        except requests.RequestException as exc:
            log.warning("Petició fallida (%d/%d) a %s: %s",
                        attempt, config.REQUEST_RETRIES + 1, url, exc)
            if attempt <= config.REQUEST_RETRIES:
                time.sleep(2.0 * attempt)
    log.error("No s'ha pogut descarregar %s després de %d intents.",
              url, config.REQUEST_RETRIES + 1)
    return None


# --------------------------------------------------------------------------- #
# Utilitats de parsing                                                         #
# --------------------------------------------------------------------------- #
def clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def truncate(text: str, limit: int = config.SUMMARY_MAX_CHARS) -> str:
    text = clean_text(text)
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0].rstrip(",.;:")
    return cut + "…"


def parse_dmy(text: str) -> Optional[datetime]:
    """Extreu una data DD/MM/YYYY d'un text lliure."""
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", text or "")
    if not m:
        return None
    day, month, year = (int(g) for g in m.groups())
    try:
        return datetime(year, month, day)
    except ValueError:
        return None


def absolutize(url: str, base: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("http"):
        return url
    return requests.compat.urljoin(base, url)


def fetch_og_meta(session: requests.Session, url: str) -> dict:
    """Obre una pàgina i retorna {'image': ..., 'description': ...} dels meta og."""
    out = {"image": "", "description": ""}
    resp = polite_get(session, url)
    if not resp:
        return out
    soup = BeautifulSoup(resp.content, "html.parser")

    def meta(prop: str, attr: str = "property") -> str:
        tag = soup.find("meta", {attr: prop})
        return clean_text(tag.get("content", "")) if tag and tag.get("content") else ""

    out["image"] = meta("og:image") or ""
    out["description"] = meta("og:description") or meta("description", "name") or ""
    return out


# --------------------------------------------------------------------------- #
# Parsers per font                                                             #
# --------------------------------------------------------------------------- #
def parse_elracodelmanga(html: str, source: dict, session: requests.Session) -> list[NewsItem]:
    """El Racó del Manga: blocs .news-article a la pàgina /noticies/.

    No tenen permalink propi, així que l'enllaç és la pàgina de notícies.
    """
    soup = BeautifulSoup(html, "html.parser")
    items: list[NewsItem] = []
    for art in soup.select(".news-article"):
        h3 = art.find("h3")
        if not h3:
            continue
        title = clean_text(h3.get_text())
        info = art.select_one(".autho-info")
        date = parse_dmy(info.get_text() if info else "")
        if not date:
            continue
        category = clean_text(art.get("data-category") or "")
        if not category:
            span = art.select_one(".category")
            category = clean_text(span.get_text()) if span else ""
        p = art.find("p")
        summary = truncate(p.get_text(" ") if p else "")
        img = art.find("img")
        image_url = absolutize(img.get("src", ""), source["url"]) if img else ""
        items.append(NewsItem(
            source=source["name"], source_key=source["key"], source_emoji=source["emoji"],
            title=title, url=source["url"], date=date, category=category,
            summary=summary, image_url=image_url, lang=source["lang"],
        ))
    return items


def parse_fansubs(html: str, source: dict, session: requests.Session) -> list[NewsItem]:
    """Fansubs.cat: blocs .news-article amb .news-title/.news-date/.news-text."""
    soup = BeautifulSoup(html, "html.parser")
    items: list[NewsItem] = []
    for art in soup.select(".news-article"):
        title_a = art.select_one(".news-title a")
        title_el = title_a or art.select_one(".news-title")
        if not title_el:
            continue
        title = clean_text(title_el.get_text())
        url = (title_a.get("href", "").strip() if title_a else "") or source["url"]
        date_el = art.select_one(".news-date")
        date = parse_dmy(date_el.get_text() if date_el else "")
        if not date:
            continue
        text_el = art.select_one(".news-text")
        summary = truncate(text_el.get_text(" ") if text_el else "")
        fansub_el = art.select_one(".news-fansub")
        fansub = clean_text(fansub_el.get_text()) if fansub_el else ""
        # Categoria: prefix [Anime]/[Manga] al títol si existeix, si no el fansub.
        m = re.match(r"\s*\[([^\]]+)\]", title)
        category = m.group(1) if m else (fansub or "Fansub")
        img = art.select_one(".news-image img") or art.select_one(".news-image-mobile")
        image_url = absolutize(img.get("src", ""), source["url"]) if img else ""
        item = NewsItem(
            source=source["name"], source_key=source["key"], source_emoji=source["emoji"],
            title=title, url=url, date=date, category=category,
            summary=summary, image_url=image_url, lang=source["lang"],
        )
        items.append(item)
    return items


def parse_animecorner(feed_url: str, source: dict, session: requests.Session) -> list[NewsItem]:
    """Anime Corner: feed RSS. Sense imatge ni resum al feed → enriquim obrint
    l'article (og:image + og:description). El contingut és en anglès."""
    try:
        import feedparser  # import diferit: només cal per a fonts RSS
    except ImportError:
        log.error("feedparser no està instal·lat; s'omet la font %s.", source["name"])
        return []

    resp = polite_get(session, feed_url)
    if not resp:
        return []
    feed = feedparser.parse(resp.content)
    items: list[NewsItem] = []
    cutoff = datetime.now() - timedelta(days=config.DAYS_BACK)

    for entry in feed.entries:
        # Data
        date = None
        if getattr(entry, "published_parsed", None):
            date = datetime(*entry.published_parsed[:6])
        if not date:
            continue
        # Filtrem aviat per estalviar peticions d'enriquiment
        if date < cutoff:
            continue
        title = clean_text(entry.get("title", ""))
        url = entry.get("link", "").strip()
        if not title or not url:
            continue
        tags = [clean_text(t.get("term", "")) for t in entry.get("tags", []) if t.get("term")]
        # Evitem la categoria genèrica "News" si n'hi ha de més específiques
        specific = [t for t in tags if t.lower() != "news"]
        category = (specific or tags or ["News"])[0]

        # Enriquim amb og:image + og:description (article en anglès)
        meta = fetch_og_meta(session, url)
        summary = truncate(meta["description"] or entry.get("summary", ""))
        items.append(NewsItem(
            source=source["name"], source_key=source["key"], source_emoji=source["emoji"],
            title=title, url=url, date=date, category=category,
            summary=summary, image_url=meta["image"], lang=source["lang"],
        ))
        if len(items) >= config.MAX_ITEMS_PER_SOURCE:
            break
    return items


# Registre clau -> funció de parsing
PARSERS = {
    "elracodelmanga": parse_elracodelmanga,
    "fansubs": parse_fansubs,
    "animecorner": parse_animecorner,
}


# --------------------------------------------------------------------------- #
# Orquestració                                                                 #
# --------------------------------------------------------------------------- #
def _is_blocked(item: NewsItem) -> bool:
    haystack = f"{item.title} {item.summary} {item.category}".lower()
    return any(kw.lower() in haystack for kw in config.SKIP_KEYWORDS if kw)


def scrape_source(session: requests.Session, source: dict) -> list[NewsItem]:
    """Scrapeja una font concreta, amb gestió d'errors aïllada."""
    parser = PARSERS.get(source["key"])
    if not parser:
        log.error("No hi ha parser registrat per a la font '%s'.", source["key"])
        return []
    log.info("Scrapejant %s (%s)...", source["name"], source["url"])
    try:
        if source["type"] == "rss":
            items = parser(source["url"], source, session)
        else:
            resp = polite_get(session, source["url"])
            if not resp:
                return []
            # Passem bytes perquè BeautifulSoup detecti l'encoding pel <meta charset>
            # (algunes webs no declaren charset a les capçaleres i requests l'encerta malament).
            items = parser(resp.content, source, session)
    except Exception as exc:  # noqa: BLE001 — volem que una font no tombi la resta
        log.exception("Error inesperat scrapejant %s: %s", source["name"], exc)
        return []
    log.info("  → %d notícies brutes de %s", len(items), source["name"])
    return items[: config.MAX_ITEMS_PER_SOURCE]


def filter_recent(items: list[NewsItem], days: int = config.DAYS_BACK) -> list[NewsItem]:
    cutoff = (datetime.now() - timedelta(days=days)).date()
    return [it for it in items if it.date.date() >= cutoff]


def scrape_all() -> list[NewsItem]:
    """Scrapeja totes les fonts actives, filtra per data i NSFW, i deduplica
    dins de la mateixa execució. Retorna ordenat de més nou a més antic."""
    session = build_session()
    all_items: list[NewsItem] = []
    for source in config.SOURCES:
        if not source.get("enabled", True):
            continue
        all_items.extend(scrape_source(session, source))

    recent = filter_recent(all_items)
    log.info("%d notícies dins dels últims %d dies.", len(recent), config.DAYS_BACK)

    # Filtre NSFW / paraules vetades
    kept = [it for it in recent if not _is_blocked(it)]
    if len(kept) != len(recent):
        log.info("S'han omès %d notícies pel filtre de contingut.", len(recent) - len(kept))

    # Dedup dins de l'execució
    seen: set[str] = set()
    unique: list[NewsItem] = []
    for it in kept:
        key = it.dedupe_key()
        if key in seen:
            continue
        seen.add(key)
        unique.append(it)

    unique.sort(key=lambda it: it.date, reverse=True)
    return unique
