"""
divulgacio.py — Pack «Divulgació» per a r/divulgacio. Dues fonts:

  DIJOUS 18:00 UTC → cienciaoberta.cat
    wp-json → darrer article → DeepSeek (títol enginyós + post engaging en català)
    → post d'imatge + comentari

  DILLUNS 15:30 UTC → 7ciencies.cat
    wp-json → darrer article → títol original + DeepSeek (resum breu)
    → post d'imatge + comentari amb resum i enllaç original

Ús:
  python divulgacio.py --post                        # preview del que toca avui
  python divulgacio.py --push                        # encua al Worker
  python divulgacio.py --source cienciaoberta --post # força una font concreta
  python divulgacio.py --source set7ciencies --post
  python divulgacio.py --no-llm --post               # sense DeepSeek
  python divulgacio.py --diagnose                    # diagnòstic fonts
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

import config
import queue_store
import r2_upload
from processor import _deepseek_chat

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_HISTORY_FILE: Path = config.OUTPUT_DIR / "divulgacio_history.json"
_QUEUE_SOURCE = "divulgacio"
_QUEUE_SOURCE_LABEL = "Divulgació"
_SUBREDDIT = config.DIVULGACIO_SUBREDDIT

_HEADERS = {"User-Agent": "Mozilla/5.0"}

# ---------------------------------------------------------------------------
# Calendari de fonts
# ---------------------------------------------------------------------------

def sources_due(d: date) -> list[str]:
    """Retorna les fonts que toquen avui."""
    dow = d.weekday()   # 0=dl, 3=dj, 4=dv
    due = []
    if dow == 0:
        due.append("set7ciencies")    # dilluns
    if dow == 3:
        due.append("cienciaoberta")   # dijous
    return due


# ---------------------------------------------------------------------------
# Historial
# ---------------------------------------------------------------------------

def _load_history() -> set[str]:
    if _HISTORY_FILE.exists():
        data = json.loads(_HISTORY_FILE.read_text(encoding="utf-8"))
        return set(data.get("urls", []))
    return set()


def _save_history(urls: set[str]) -> None:
    config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    _HISTORY_FILE.write_text(
        json.dumps({"urls": sorted(urls)}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Parsers de les fonts
# ---------------------------------------------------------------------------

def _parse_wp_posts(base_url: str) -> list[dict]:
    """Agafa els darrers posts via wp-json (genèric per als dos llocs)."""
    api = (
        f"{base_url.rstrip('/')}/wp-json/wp/v2/posts"
        "?per_page=10&_embed=1&orderby=date&order=desc"
    )
    r = requests.get(api, headers=_HEADERS, timeout=15)
    r.raise_for_status()
    result = []
    for p in r.json():
        title = BeautifulSoup(p["title"]["rendered"], "html.parser").get_text().strip()
        excerpt = BeautifulSoup(
            p["excerpt"]["rendered"], "html.parser"
        ).get_text(" ", strip=True)
        link = p["link"]
        img = ""
        media_list = p.get("_embedded", {}).get("wp:featuredmedia", [])
        if media_list and isinstance(media_list, list) and media_list[0]:
            img = media_list[0].get("source_url", "")
        result.append({"title": title, "url": link, "excerpt": excerpt, "image_url": img})
    return result


def _fetch_article_text(url: str) -> str:
    """Baixa el text principal d'un article WordPress."""
    try:
        r = requests.get(url, headers=_HEADERS, timeout=20)
        r.raise_for_status()
    except Exception as e:
        log.warning("No s'ha pogut baixar l'article (%s): %s", url, e)
        return ""
    soup = BeautifulSoup(r.text, "html.parser")
    container = (
        soup.find("article")
        or soup.find(class_="entry-content")
        or soup.find(class_="post-content")
        or soup
    )
    paragraphs = [
        p.get_text(" ", strip=True)
        for p in container.find_all("p")
        if len(p.get_text(strip=True)) > 60
    ]
    # Descarta primer paràgraf si és metadata (autor/data)
    if paragraphs and re.match(r"^Publicat per|^Per ", paragraphs[0]):
        paragraphs = paragraphs[1:]
    return " ".join(paragraphs[:10])


# ---------------------------------------------------------------------------
# DeepSeek: dos modes
# ---------------------------------------------------------------------------

_PROMPT_ENGAGING = """Tens un article de ciència en català de Ciència Oberta (web de divulgació científica).
Converteix-lo en un post engaging per a Reddit r/divulgacio: ha de despertar curiositat,
fer que la gent vulgui llegir més — comença amb una pregunta retòrica o una dada sorprenent.
Estil: directe, apassionat per la ciència, accessible però rigorós. Català de Catalunya.
NO facis servir clickbait buit ni expressions com "no et perdis" o "llegeix ara".

Retorna EXACTAMENT en aquest format (sense cap altre text):
TITOL: <títol nou en català — enginyós, 60-100 caràcters, que cridi l'atenció>
TEXT: <post en markdown per a Reddit: 3-5 paràgrafs, usa ** per ressaltar conceptes clau, comença amb ganxo fort>

Article original:
Títol: {title}
Text: {body}"""

_PROMPT_RESUM = """Tens un article de divulgació científica en català de 7ciències.
Escriu un resum breu (2-3 frases) per a Reddit r/divulgacio en català de Catalunya.
Ha de ser informatiu i precís; acaba indicant que hi ha més informació a l'article original.
Res de clickbait. L'estil ha de ser directe i clar.

Retorna EXACTAMENT en aquest format (sense cap altre text):
TEXT: <resum en 2-3 frases + "Més informació a l'article original.">

Article original:
Títol: {title}
Text: {body}"""


def _rewrite_engaging(title: str, body: str) -> tuple[str, str]:
    """cienciaoberta: retorna (nou_titol, nou_text)."""
    try:
        resposta = _deepseek_chat(
            [{"role": "user", "content": _PROMPT_ENGAGING.format(title=title, body=body[:2000])}],
            max_tokens=700,
        )
    except Exception as e:
        log.warning("DeepSeek ha fallat: %s", e)
        return title, body[:600]
    titol_m = re.search(r"TITOL:\s*(.+)", resposta)
    text_m = re.search(r"TEXT:\s*([\s\S]+)", resposta)
    return (
        titol_m.group(1).strip() if titol_m else title,
        text_m.group(1).strip() if text_m else body[:600],
    )


def _resum_simple(title: str, body: str, url: str) -> tuple[str, str]:
    """7ciencies: retorna (titol_original, resum_breu)."""
    try:
        resposta = _deepseek_chat(
            [{"role": "user", "content": _PROMPT_RESUM.format(title=title, body=body[:1500])}],
            max_tokens=300,
        )
    except Exception as e:
        log.warning("DeepSeek ha fallat: %s", e)
        excerpt_fallback = body[:300] + f"\n\nMés informació a l'[article original]({url})."
        return title, excerpt_fallback
    text_m = re.search(r"TEXT:\s*([\s\S]+)", resposta)
    return title, text_m.group(1).strip() if text_m else body[:300]


# ---------------------------------------------------------------------------
# Imatge: re-allotja a R2
# ---------------------------------------------------------------------------

def _rehost_image(image_url: str, source_key: str, slug: str) -> str:
    if not image_url:
        return ""
    if not all([config.R2_ACCOUNT_ID, config.R2_BUCKET, config.R2_PUBLIC_BASE]):
        log.warning("R2 no configurat; s'usa la URL original.")
        return image_url
    try:
        r = requests.get(image_url, headers=_HEADERS, timeout=20)
        r.raise_for_status()
        data = r.content
        content_type = r.headers.get("Content-Type", "image/jpeg")
        ext = (
            "jpg" if "jpeg" in content_type or "jpg" in content_type
            else "png" if "png" in content_type
            else "webp" if "webp" in content_type
            else "jpg"
        )
        if ext == "webp":
            from PIL import Image
            import io
            img = Image.open(io.BytesIO(data)).convert("RGB")
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=92)
            data, ext, content_type = buf.getvalue(), "jpg", "image/jpeg"
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        key = f"divulgacio/{source_key}/{slug}/{stamp}.{ext}"
        return r2_upload.upload_bytes(data, key, content_type)
    except Exception as e:
        log.warning("Error re-allotjant imatge: %s", e)
        return image_url


# ---------------------------------------------------------------------------
# Processa una font concreta
# ---------------------------------------------------------------------------

def _process_source(source_key: str, history: set[str], use_llm: bool) -> dict | None:
    """Retorna el payload llest per encuar, o None si no hi ha res nou."""
    if source_key == "cienciaoberta":
        posts = _parse_wp_posts("https://www.cienciaoberta.cat")
        source_label = "Ciència Oberta"
    elif source_key == "set7ciencies":
        posts = _parse_wp_posts("https://7ciencies.cat")
        source_label = "7Ciències"
    else:
        raise ValueError(f"Font desconeguda: {source_key}")

    log.info("%s: %d articles disponibles.", source_label, len(posts))

    article = next((p for p in posts if p["url"] not in history), None)
    if article is None:
        log.info("%s: cap article nou.", source_label)
        return None

    title = article["title"]
    url = article["url"]
    body = _fetch_article_text(url) or article["excerpt"]

    if source_key == "cienciaoberta":
        if use_llm:
            new_title, new_text = _rewrite_engaging(title, body)
        else:
            new_title, new_text = title, article["excerpt"]
        footer = f"\n\n*(via [Ciència Oberta]({url}))*"
    else:  # set7ciencies
        if use_llm:
            new_title, new_text = _resum_simple(title, body, url)
        else:
            new_title = title
            new_text = article["excerpt"][:400]
        footer = f"\n\n*(via [7Ciències]({url}))*"

    slug = re.sub(r"[^a-z0-9]+", "-", new_title.lower())[:40]
    image_url_r2 = _rehost_image(article["image_url"], source_key, slug)

    print(f"\n--- ORIGINAL ({source_label}) ---")
    print(f"Títol: {title}")
    print(f"Text:  {body[:350]}...")
    print(f"\n--- RESULTAT ({'' if use_llm else 'NO '}LLM) ---")
    print(f"Títol: {new_title}")
    print(f"Text:\n{new_text[:500]}")
    print(f"\nImatge: {article['image_url']}")

    return {
        "article_url": url,
        "payload": {
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
            "tipus": "imatge" if image_url_r2 else "text",
            "title": new_title,
            "url": image_url_r2 if image_url_r2 else None,
            "markdown": (new_text + footer) if not image_url_r2 else None,
            "comment_markdown": (new_text + footer) if image_url_r2 else None,
            "subreddit": _SUBREDDIT,
            "source": _QUEUE_SOURCE,
            "source_label": source_label,
        },
    }


# ---------------------------------------------------------------------------
# Punt d'entrada
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Divulgació — ciència en català per a r/divulgacio"
    )
    parser.add_argument("--post", action="store_true", help="Preview (no encua res)")
    parser.add_argument("--push", action="store_true", help="Encua al Worker")
    parser.add_argument("--source", default="", help="Força font: cienciaoberta | set7ciencies")
    parser.add_argument("--no-llm", action="store_true", help="Sense DeepSeek")
    parser.add_argument("--diagnose", action="store_true", help="Diagnòstic fonts")
    args = parser.parse_args()

    if not args.post and not args.push and not args.diagnose:
        parser.print_help()
        sys.exit(0)

    if args.diagnose:
        print("=== Diagnòstic divulgacio.py ===")
        for key, base in [("cienciaoberta", "https://www.cienciaoberta.cat"),
                          ("set7ciencies", "https://7ciencies.cat")]:
            try:
                posts = _parse_wp_posts(base)
                p = posts[0] if posts else None
                print(f"{key}: {len(posts)} articles{'  | darrer: ' + p['title'][:60] if p else ''}")
                if p:
                    print(f"  Imatge: {p['image_url'] or 'cap'}")
            except Exception as e:
                print(f"{key}: ERROR — {e}")
        return

    history = _load_history()
    use_llm = config.USE_LLM and not args.no_llm

    # Determina quines fonts toquen avui (o la forçada per --source)
    if args.source:
        due = [args.source]
    else:
        due = sources_due(date.today())

    if not due:
        log.info("Avui no toca cap font de divulgacio. Res a encuar.")
        return

    for source_key in due:
        try:
            result = _process_source(source_key, history, use_llm)
        except Exception as exc:  # noqa: BLE001
            # Robustesa: una font que falla (p.ex. timeout de xarxa) no ha de
            # tombar tot el procés; es registra i es continua amb la resta.
            log.warning("Font «%s» ha fallat: %s", source_key, exc)
            continue
        if result is None:
            continue

        if not args.push:
            print("\n[--post: no s'encua res]")
            continue

        # Neteja camps buits del payload (None)
        payload = {k: v for k, v in result["payload"].items() if v is not None}
        queue_store.enqueue(payload)
        print(
            f"✅ Encuat [{payload['source_label']}]: {payload['title'][:60]}"
            f" → {payload['tipus']}-{payload['generated_at'][:10]}"
        )
        history.add(result["article_url"])

    if args.push:
        _save_history(history)


if __name__ == "__main__":
    main()
