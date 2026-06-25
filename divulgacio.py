"""
divulgacio.py — Pack «Ciència Oberta» per a r/divulgacio.

Cada dijous: agafa el darrer article de cienciaoberta.cat (via wp-json),
el transforma amb DeepSeek en un post engaging en català i l'encua al
Worker com a post d'imatge + comentari.

Flux:
  wp-json cienciaoberta.cat → darrer article no publicat
  → text complet (HTML scraping) → DeepSeek (títol + post engaging en català)
  → imatge destacada a R2 → queue_store.enqueue (r/divulgacio)

Ús:
  python divulgacio.py --post       # preview (no encua res)
  python divulgacio.py --push       # encua al Worker
  python divulgacio.py --no-llm     # sense DeepSeek (excerpt original)
  python divulgacio.py --diagnose   # diagnòstic: comprova API i article
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from datetime import datetime, timezone
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
_API_URL = (
    "https://www.cienciaoberta.cat/wp-json/wp/v2/posts"
    "?per_page=10&_embed=1&orderby=date&order=desc"
)
_SITE_BASE = "https://www.cienciaoberta.cat"
_HISTORY_FILE: Path = config.OUTPUT_DIR / "divulgacio_history.json"
_QUEUE_SOURCE = "divulgacio"
_QUEUE_SOURCE_LABEL = "Ciència Oberta"
_SUBREDDIT = config.DIVULGACIO_SUBREDDIT

_HEADERS = {"User-Agent": "Mozilla/5.0"}

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
# API: articles
# ---------------------------------------------------------------------------

def _fetch_posts() -> list[dict]:
    """Retorna articles recents de cienciaoberta.cat via wp-json."""
    r = requests.get(_API_URL, headers=_HEADERS, timeout=15)
    r.raise_for_status()
    posts = r.json()
    result = []
    for p in posts:
        title = BeautifulSoup(p["title"]["rendered"], "html.parser").get_text()
        excerpt = BeautifulSoup(
            p["excerpt"]["rendered"], "html.parser"
        ).get_text(" ", strip=True)
        link = p["link"]
        img = ""
        embedded = p.get("_embedded", {})
        media_list = embedded.get("wp:featuredmedia", [])
        if media_list and isinstance(media_list, list):
            img = media_list[0].get("source_url", "")
        result.append({
            "title": title.strip(),
            "url": link,
            "excerpt": excerpt,
            "image_url": img,
        })
    return result


# ---------------------------------------------------------------------------
# Article complet
# ---------------------------------------------------------------------------

def _fetch_article_text(url: str) -> str:
    """Baixa el text principal d'un article de cienciaoberta.cat."""
    try:
        r = requests.get(url, headers=_HEADERS, timeout=20)
        r.raise_for_status()
    except Exception as e:
        log.warning("No s'ha pogut baixar l'article: %s", e)
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
    # Descarta el primer paràgraf si és metadata (autor/data/categoria)
    if paragraphs and re.match(r"^Publicat per", paragraphs[0]):
        paragraphs = paragraphs[1:]
    return " ".join(paragraphs[:10])


# ---------------------------------------------------------------------------
# DeepSeek: reescriptura engaging
# ---------------------------------------------------------------------------

_PROMPT = """Tens un article de ciència en català de Ciència Oberta (web de divulgació científica).
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


def _rewrite_with_deepseek(title: str, body: str) -> tuple[str, str]:
    """Retorna (nou_titol, nou_text) en català per DeepSeek."""
    prompt = _PROMPT.format(title=title, body=body[:2000])
    try:
        resposta = _deepseek_chat(
            [{"role": "user", "content": prompt}],
            max_tokens=700,
        )
    except Exception as e:
        log.warning("DeepSeek ha fallat: %s", e)
        return title, body[:600]

    titol_m = re.search(r"TITOL:\s*(.+)", resposta)
    text_m = re.search(r"TEXT:\s*([\s\S]+)", resposta)
    nou_titol = titol_m.group(1).strip() if titol_m else title
    nou_text = text_m.group(1).strip() if text_m else body[:600]
    return nou_titol, nou_text


# ---------------------------------------------------------------------------
# Imatge: re-allotja a R2
# ---------------------------------------------------------------------------

def _rehost_image(image_url: str, slug: str) -> str:
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
            data = buf.getvalue()
            ext = "jpg"
            content_type = "image/jpeg"

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        key = f"divulgacio/{slug}/{stamp}.{ext}"
        return r2_upload.upload_bytes(data, key, content_type)
    except Exception as e:
        log.warning("Error re-allotjant imatge: %s", e)
        return image_url


# ---------------------------------------------------------------------------
# Punt d'entrada
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Ciència Oberta — divulgació científica per a r/divulgacio"
    )
    parser.add_argument("--post", action="store_true", help="Preview (no encua res)")
    parser.add_argument("--push", action="store_true", help="Encua al Worker")
    parser.add_argument("--no-llm", action="store_true", help="Sense DeepSeek")
    parser.add_argument("--diagnose", action="store_true", help="Diagnòstic API/article")
    args = parser.parse_args()

    if not args.post and not args.push and not args.diagnose:
        parser.print_help()
        sys.exit(0)

    if args.diagnose:
        print("=== Diagnòstic divulgacio.py ===")
        try:
            posts = _fetch_posts()
            print(f"wp-json OK: {len(posts)} articles")
            if posts:
                first = posts[0]
                print(f"  Darrer: {first['title']}")
                print(f"  URL: {first['url']}")
                print(f"  Imatge: {first['image_url']}")
                body = _fetch_article_text(first["url"])
                print(f"  Text article ({len(body)} chars): {body[:200]}...")
        except Exception as e:
            print(f"ERROR: {e}")
        return

    history = _load_history()

    try:
        posts = _fetch_posts()
    except Exception as e:
        log.error("Error llegint l'API de cienciaoberta.cat: %s", e)
        sys.exit(1)

    if not posts:
        log.error("L'API no ha retornat cap article.")
        sys.exit(1)

    log.info("cienciaoberta.cat: %d articles disponibles.", len(posts))

    article = None
    for post in posts:
        if post["url"] not in history:
            article = post
            break

    if article is None:
        log.info("Cap article nou (tots ja publicats). Res a encuar.")
        return

    title_orig = article["title"]
    url_orig = article["url"]
    image_url_orig = article["image_url"]

    body = _fetch_article_text(url_orig)
    if not body:
        body = article["excerpt"]

    use_llm = config.USE_LLM and not args.no_llm
    if use_llm:
        new_title, new_text = _rewrite_with_deepseek(title_orig, body)
    else:
        new_title = title_orig
        new_text = article["excerpt"]

    footer = f"\n\n*(via [Ciència Oberta]({url_orig}))*"

    print(f"\n--- ORIGINAL ---")
    print(f"Títol: {title_orig}")
    print(f"Text:  {body[:350]}...")
    print(f"\n--- DEEPSEEK ({'' if use_llm else 'NO '}LLM) ---")
    print(f"Títol: {new_title}")
    print(f"Text:\n{new_text}")
    print(f"\nImatge: {image_url_orig}")

    if not args.push:
        print("\n[--post: no s'encua res]")
        return

    slug = re.sub(r"[^a-z0-9]+", "-", new_title.lower())[:40]
    image_url_r2 = _rehost_image(image_url_orig, slug)

    if not image_url_r2:
        log.error("No hi ha imatge disponible per al post d'imatge.")
        sys.exit(1)

    comment = f"{new_text}{footer}"
    payload = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
        "tipus": "imatge",
        "title": new_title,
        "url": image_url_r2,
        "comment_markdown": comment,
        "subreddit": _SUBREDDIT,
        "source": _QUEUE_SOURCE,
        "source_label": _QUEUE_SOURCE_LABEL,
    }

    queue_store.enqueue(payload)
    print(
        f"✅ Encuat [{_QUEUE_SOURCE_LABEL}]: {new_title[:60]}"
        f" → imatge-{payload['generated_at'][:10]}"
    )

    history.add(url_orig)
    _save_history(history)


if __name__ == "__main__":
    main()
