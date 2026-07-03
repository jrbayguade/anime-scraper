"""
mainada.py — Pack «Mainada»: activitats i plans en família per a r/mainada.

Vuitena sortida (autònoma). Mateixa arquitectura que explorant.py, però amb
fonts DIFERENTS (perquè no dupliqui contingut d'Explorant Catalunya) i un
calendari de dimarts a divendres. Cada dia agafa UNA fitxa nova d'una font, en
fa un resum curt en català amb DeepSeek, RE-ALLOTJA la foto a Cloudflare R2 i
ho publica com a **post d'imatge + primer comentari** (resum + enllaç a la font
original al peu, en cursiva).

Fonts i calendari:
    dimarts   → criar.cat            (RSS de criança; WordPress)
    dimecres  → surtdecasa (família) (agenda /agenda/familia; HTML)
    dijous    → festacatalunya       (activitats amb nens; HTML)
    divendres → criar.cat            (segona peça de la setmana)

Notes de viabilitat (provat des de les IPs de GitHub Actions):
    - criar.cat: WordPress net, RSS a /feed/ (text/xml). Sense Cloudflare.
    - surtdecasa: Drupal (nginx), mateixa estructura `.views-row` que a explorant.
    - festacatalunya: darrere Cloudflare però ara serveix la pàgina sencera
      (HTTP 200) des de CI. No és WordPress; scraping HTML de targetes. És el
      candidat més fràgil: si CF apuja la seguretat, caldria substituir-la.

Ús:
    python mainada.py --post                     # preview del que toca avui
    python mainada.py --source criar --post       # prova una font concreta
    python mainada.py --all --post                # totes les fonts (ignora calendari)
    python mainada.py --push                       # publica (re-allotja imatge + encua)
    python mainada.py --no-llm --post              # sense DeepSeek (resum cru)
    python mainada.py --diagnose                    # diagnòstic: què rep cada font
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass
from datetime import date, datetime
from urllib.parse import urljoin

import feedparser
import requests
from bs4 import BeautifulSoup

import config

log = logging.getLogger("anime-scraper.mainada")

try:
    import queue_store
    _HAS_QUEUE = True
except Exception:  # pragma: no cover
    _HAS_QUEUE = False

SYSTEM_PROMPT = (
    "Ets qui dinamitza r/mainada, una comunitat catalana de famílies amb canalla "
    "per descobrir plans, activitats i recursos de criança. Escrius en català "
    "natural, proper i engrescador, sense floritures ni farciment. Vas al gra, "
    "fas servir Markdown amb mesura i convides la gent a fer plans en família. No "
    "t'inventis dades que no tinguis. No facis servir mai el guió llarg (—)."
)


@dataclass
class Fitxa:
    """Una activitat/article trobat en una font."""
    source_key: str
    source_name: str
    source_web: str        # web de la font (per al peu)
    title: str
    url: str               # enllaç a l'article/fitxa original
    summary: str           # text original (DeepSeek el reescriu després)
    image_url: str         # foto (es re-allotja a R2)
    where: str = ""        # població/lloc (opcional)

    def key(self) -> str:
        return f"{self.source_key}|{self.url}"


# --------------------------------------------------------------------------- #
# Utilitats de xarxa (genèriques, com a explorant.py)                          #
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


def _img_src(node) -> str:
    img = node.find("img") if node else None
    if not img:
        return ""
    src = (img.get("src") or img.get("data-src") or "").strip()
    return src if src.startswith("http") else ""


# --------------------------------------------------------------------------- #
# Parsers de les fonts                                                         #
# --------------------------------------------------------------------------- #
def parse_criar(_today: date) -> list[Fitxa]:
    """criar.cat — RSS de WordPress (/feed/). Article de criança amb og:image."""
    web = "https://www.criar.cat"
    try:
        raw = requests.get(f"{web}/feed/", headers=config.HTTP_HEADERS,
                           timeout=config.REQUEST_TIMEOUT)
        d = feedparser.parse(raw.content)
    except Exception as exc:  # noqa: BLE001
        log.warning("criar RSS ha fallat: %s", exc)
        return []
    out: list[Fitxa] = []
    for e in d.entries[:8]:
        summary = BeautifulSoup(e.get("summary", ""), "lxml").get_text(" ", strip=True)
        out.append(Fitxa(
            source_key="criar", source_name="Criar.cat", source_web=web,
            title=(e.get("title") or "").strip(),
            url=(e.get("link") or "").strip(),
            summary=_clean_summary(summary),
            image_url=_entry_image(e, og_fallback=True),
        ))
    log.info("criar: %d entrades del feed.", len(out))
    return out


def parse_surtdecasa_familia(_today: date) -> list[Fitxa]:
    """surtdecasa.cat — agenda de família (HTML; cada `.views-row` és un pla)."""
    web = "https://surtdecasa.cat"
    soup = _get_soup(f"{web}/agenda/familia")
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
            out.append(Fitxa("surtdecasa_familia", "Surt de casa", web, title,
                             urljoin(web, a["href"]), title, src))
    log.info("surtdecasa_familia: %d plans.", len(out))
    return out


# Imatges de menú/logo que no volem agafar com a foto de l'activitat.
_BAD_IMG = ("logo", "icon", "avatar", "placeholder", "sprite", "banner")


def parse_festacatalunya(_today: date) -> list[Fitxa]:
    """festacatalunya.cat — activitats amb nens (HTML; targetes amb imatge + títol).

    No és WordPress i no en coneixem el marcatge exacte, així que el parser és
    genèric: recorre contenidors de targeta habituals i es queda amb els que
    tenen enllaç propi, imatge de contingut i un títol raonable. Es valida amb
    `--diagnose` / `--source festacatalunya --post` des de CI.
    """
    web = "https://www.festacatalunya.cat"
    soup = _get_soup(f"{web}/activitats-amb-nens-catalunya")
    if not soup:
        return []
    out: list[Fitxa] = []
    seen: set[str] = set()
    for node in soup.select("article, .item, .card, .activitat, .event, .post, li"):
        a = node.find("a", href=True)
        if not a:
            continue
        href = urljoin(web, a["href"])
        if href in seen or "festacatalunya.cat" not in href:
            continue
        src = _img_src(node)
        if not src or any(b in src.lower() for b in _BAD_IMG):
            continue
        h = node.find(["h1", "h2", "h3", "h4"])
        title = (h.get_text(" ", strip=True) if h
                 else (a.get("title") or a.get_text(" ", strip=True))).strip()
        if len(title) < 8:
            continue
        seen.add(href)
        out.append(Fitxa("festacatalunya", "Festa Catalunya", web, title,
                         href, title, src))
    log.info("festacatalunya: %d activitats.", len(out))
    return out


# Registre de fonts: clau → (nom, web, parser).
SOURCES: dict[str, dict] = {
    "criar": {
        "name": "Criar.cat", "web": "https://www.criar.cat",
        "parse": parse_criar,
    },
    "surtdecasa_familia": {
        "name": "Surt de casa", "web": "https://surtdecasa.cat",
        "parse": parse_surtdecasa_familia,
    },
    "festacatalunya": {
        "name": "Festa Catalunya", "web": "https://www.festacatalunya.cat",
        "parse": parse_festacatalunya,
    },
}


# --------------------------------------------------------------------------- #
# Calendari: quines fonts toquen avui (dt–dv)                                  #
# --------------------------------------------------------------------------- #
def sources_due(d: date) -> list[str]:
    """Claus de les fonts que toca publicar avui (dimarts a divendres)."""
    dow = d.weekday()   # 0=dilluns ... 6=diumenge
    return {
        1: ["criar"],                 # dimarts
        2: ["surtdecasa_familia"],    # dimecres
        3: ["festacatalunya"],        # dijous
        4: ["criar"],                 # divendres
    }.get(dow, [])


# --------------------------------------------------------------------------- #
# Històric (dedup)                                                             #
# --------------------------------------------------------------------------- #
def _load_history() -> set[str]:
    try:
        data = json.loads(config.MAINADA_HISTORY_FILE.read_text(encoding="utf-8"))
        return set(data.get("posted", []))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return set()


def _save_history(posted: set[str]) -> None:
    config.MAINADA_HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    config.MAINADA_HISTORY_FILE.write_text(
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
            if f.summary:
                ctx += f"Descripció: {f.summary}\n"
            user = (
                "Escriu una descripció breu i engrescadora en català (2-3 frases, "
                "pots fer servir 1-2 emojis i alguna negreta) per convidar famílies "
                "amb canalla a aquesta activitat o article. No inventis detalls que "
                "no et dono; si en falten, queda't en el to i convida a mirar la "
                f"font. No posis títol ni encapçalament.\n\n{ctx}"
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
    key = f"mainada/{f.source_key}/{stamp}.{ext}"
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
    emoji = "🎈"
    title = f"{emoji} {f.title}"
    if f.where and f.where.lower() not in f.title.lower():
        title += f" ({f.where})"
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "tipus": "imatge",
        "title": title[:300],
        "subreddit": config.MAINADA_SUBREDDIT,
        "url": image_url,
        "comment_markdown": comment,
        "source": "mainada",
        "source_label": "Mainada",
    }


# --------------------------------------------------------------------------- #
# CLI                                                                          #
# --------------------------------------------------------------------------- #
# URL principal que toca cada parser (per al mode --diagnose).
_PROBE_URL = {
    "criar": "https://www.criar.cat/feed/",
    "surtdecasa_familia": "https://surtdecasa.cat/agenda/familia",
    "festacatalunya": "https://www.festacatalunya.cat/activitats-amb-nens-catalunya",
}


def diagnose(keys: list[str]) -> None:
    """Ensenya, sense encuar, què rep el CI de cada font (resposta crua + nº fitxes)."""
    for key in keys:
        url = _PROBE_URL.get(key, "")
        line = f"[{key}]"
        if url:
            try:
                r = requests.get(url, headers={**config.HTTP_HEADERS, "Accept": "*/*"},
                                 timeout=config.REQUEST_TIMEOUT)
                snippet = r.text[:90].replace("\n", " ").replace("\r", " ")
                line += (f" HTTP {r.status_code} · {r.headers.get('Content-Type','?')[:25]}"
                         f" · {len(r.content)}B · «{snippet}»")
            except Exception as exc:  # noqa: BLE001
                line += f" FETCH ERROR: {exc}"
        try:
            fitxes = SOURCES[key]["parse"](date.today())
            line += f"  →  {len(fitxes)} fitxes"
            if fitxes:
                line += f" · p.ex.: «{fitxes[0].title[:50]}»"
        except Exception as exc:  # noqa: BLE001
            line += f"  →  PARSER ERROR: {exc}"
        print(line)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Pack «Mainada» (r/mainada).")
    p.add_argument("--post", action="store_true", help="Preview (no encua res).")
    p.add_argument("--push", action="store_true", help="Publica (re-allotja imatge + encua).")
    p.add_argument("--source", help="Força una font concreta (clau de SOURCES).")
    p.add_argument("--all", action="store_true",
                   help="Totes les fonts, ignorant el calendari (per provar).")
    p.add_argument("--diagnose", action="store_true",
                   help="Diagnòstic: què rep cada font (no encua res).")
    p.add_argument("--no-llm", action="store_true", help="Sense DeepSeek (resum cru).")
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
        print("Avui no toca cap font. (Calendari a sources_due: dt–dv.)")
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
            payload = build_payload(f, image_url, comment)
            item_id = queue_store.enqueue(payload)
        except Exception as exc:  # noqa: BLE001
            log.warning("Font «%s»: no s'ha pogut publicar (%s); es continua.", key, exc)
            continue
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
