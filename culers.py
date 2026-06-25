"""
culers.py — Pack «Culers» per a r/Culers.

Cada divendres: agafa la darrera notícia del FC Barcelona de Mundo Deportivo
(RSS https://www.mundodeportivo.com/rss/futbol/fc-barcelona.xml), la reescriu
amb DeepSeek com si fos un post d'un culé de la comunitat (títol i text nous,
sempre en català), i l'encua al Worker com a post d'imatge.

Flux:
  RSS Mundo Deportivo → selecció darrera notícia no publicada
  → scraping article complet → DeepSeek (reescriptura en català)
  → imatge a R2 → queue_store.enqueue (r/Culers, post d'imatge + comentari)

Ús:
  python culers.py --post       # preview (no encua res)
  python culers.py --push       # encua al Worker
  python culers.py --no-llm     # sense DeepSeek (text original, títol traduït)
  python culers.py --diagnose   # diagnòstic: comprova RSS i article
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import defusedxml.ElementTree as ET
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
_RSS_URL = "https://www.mundodeportivo.com/rss/futbol/fc-barcelona.xml"
_HISTORY_FILE: Path = config.OUTPUT_DIR / "culers_history.json"
_QUEUE_SOURCE = "culers"
_QUEUE_SOURCE_LABEL = "Culers"
_SUBREDDIT = config.CULERS_SUBREDDIT

_HEADERS_PLAIN = {"User-Agent": "Mozilla/5.0"}
_HEADERS_CHROME = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}

# ---------------------------------------------------------------------------
# Historial de posts publicats
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
# RSS: agafa articles
# ---------------------------------------------------------------------------

def _fetch_rss() -> list[dict]:
    """Retorna els articles del RSS del Barça de Mundo Deportivo."""
    r = requests.get(_RSS_URL, headers=_HEADERS_PLAIN, timeout=15)
    r.raise_for_status()
    ns = {"media": "http://search.yahoo.com/mrss/"}
    root = ET.fromstring(r.content)
    items = []
    for item in root.findall(".//item"):
        title_el = item.find("title")
        link_el = item.find("link")
        img_el = item.find("media:content", ns)
        desc_el = item.find("description")
        if title_el is None or link_el is None:
            continue
        items.append({
            "title": (title_el.text or "").strip(),
            "url": (link_el.text or "").strip(),
            "image_url": img_el.attrib.get("url", "") if img_el is not None else "",
            "desc_rss": BeautifulSoup(desc_el.text or "", "html.parser").get_text(" ", strip=True) if desc_el is not None else "",
        })
    return items


# ---------------------------------------------------------------------------
# Article complet
# ---------------------------------------------------------------------------

def _fetch_article_text(url: str) -> str:
    """Baixa el text principal d'un article de Mundo Deportivo."""
    try:
        r = requests.get(url, headers=_HEADERS_CHROME, timeout=20, allow_redirects=True)
        r.raise_for_status()
    except Exception as e:
        log.warning("No s'ha pogut baixar l'article: %s", e)
        return ""

    soup = BeautifulSoup(r.text, "html.parser")
    container = soup.find("article") or soup
    paragraphs = [
        p.get_text(" ", strip=True)
        for p in container.find_all("p")
        if len(p.get_text(strip=True)) > 50
    ]
    return " ".join(paragraphs[:8])  # màxim 8 paràgrafs per no inflar el prompt


# ---------------------------------------------------------------------------
# DeepSeek: reescriptura com a post de culé en català
# ---------------------------------------------------------------------------

_PROMPT_TEMPLATE = """Tens una notícia del FC Barcelona en castellà de Mundo Deportivo. \
Reescriu-la com si fos un post d'un culé que ho comparteix a Reddit en català, \
de forma natural i personal (no periodística, sinó com algú de la comunitat que ho explica als altres). \
Adapta el títol perquè sembli redactat per un fan (no literalment traduït). \
El text ha de ser en català de Catalunya.

Retorna EXACTAMENT en aquest format (sense cap altre text addicional):
TITOL: <títol nou en català>
TEXT: <text adaptat en català, 3-5 frases>

Notícia original:
Títol: {title}
Text: {body}"""


def _rewrite_with_deepseek(title: str, body: str) -> tuple[str, str]:
    """Retorna (nou_titol, nou_text) reescrits en català per DeepSeek."""
    prompt = _PROMPT_TEMPLATE.format(title=title, body=body[:1500])
    try:
        resposta = _deepseek_chat(
            [{"role": "user", "content": prompt}],
            max_tokens=500,
        )
    except Exception as e:
        log.warning("DeepSeek ha fallat: %s", e)
        return title, body[:600]

    # Parseig del format TITOL: ... TEXT: ...
    titol_m = re.search(r"TITOL:\s*(.+)", resposta)
    text_m = re.search(r"TEXT:\s*([\s\S]+)", resposta)
    nou_titol = titol_m.group(1).strip() if titol_m else title
    nou_text = text_m.group(1).strip() if text_m else body[:600]
    return nou_titol, nou_text


def _translate_title_fallback(title: str) -> str:
    """Fallback mínim: tradueix paraules clau comunes castellà→català."""
    replacements = [
        (r"\bel\b", "el"), (r"\blos\b", "els"), (r"\blas\b", "les"),
        (r"\bBarça\b", "Barça"), (r"\bBarcelona\b", "Barcelona"),
    ]
    t = title
    for pat, rep in replacements:
        t = re.sub(pat, rep, t, flags=re.IGNORECASE)
    return t


# ---------------------------------------------------------------------------
# Imatge: re-allotja a R2
# ---------------------------------------------------------------------------

def _rehost_image(image_url: str, slug: str) -> str:
    """Baixa la imatge i la puja a R2. Retorna la URL pública o '' si falla."""
    if not image_url:
        return ""
    if not all([config.R2_ACCOUNT_ID, config.R2_BUCKET, config.R2_PUBLIC_BASE]):
        log.warning("R2 no configurat; no es re-allotja la imatge.")
        return image_url  # en --post, usem la URL original

    try:
        r = requests.get(image_url, headers=_HEADERS_PLAIN, timeout=20)
        r.raise_for_status()
        data = r.content
        content_type = r.headers.get("Content-Type", "image/jpeg")
        ext = "jpg" if "jpeg" in content_type or "jpg" in content_type else \
              "png" if "png" in content_type else \
              "webp" if "webp" in content_type else "jpg"

        # Converteix WebP a JPEG per compatibilitat Reddit
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
        key = f"culers/{slug}/{stamp}.{ext}"
        return r2_upload.upload_bytes(data, key, content_type)
    except Exception as e:
        log.warning("Error re-allotjant imatge: %s", e)
        return image_url


# ---------------------------------------------------------------------------
# Punt d'entrada
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Culers — notícies del Barça per a r/Culers")
    parser.add_argument("--post", action="store_true", help="Preview (no encua res)")
    parser.add_argument("--push", action="store_true", help="Encua al Worker")
    parser.add_argument("--no-llm", action="store_true", help="Sense DeepSeek")
    parser.add_argument("--diagnose", action="store_true", help="Diagnòstic RSS/article")
    args = parser.parse_args()

    if not args.post and not args.push and not args.diagnose:
        parser.print_help()
        sys.exit(0)

    # --diagnose
    if args.diagnose:
        print("=== Diagnòstic culers.py ===")
        try:
            items = _fetch_rss()
            print(f"RSS OK: {len(items)} articles")
            if items:
                first = items[0]
                print(f"  Darrer: {first['title']}")
                print(f"  URL: {first['url']}")
                print(f"  Imatge: {first['image_url']}")
                body = _fetch_article_text(first["url"])
                print(f"  Article text ({len(body)} chars): {body[:200]}...")
        except Exception as e:
            print(f"RSS ERROR: {e}")
        return

    # Carrega historial
    history = _load_history()

    # Agafa articles del RSS
    try:
        items = _fetch_rss()
    except Exception as e:
        log.error("Error llegint RSS: %s", e)
        sys.exit(1)

    if not items:
        log.error("El RSS no ha retornat cap article.")
        sys.exit(1)

    log.info("mundodeportivo: %d articles del RSS.", len(items))

    # Tria el primer article no publicat
    article = None
    for item in items:
        if item["url"] not in history:
            article = item
            break

    if article is None:
        log.info("Cap article nou (tots ja publicats). Res a encuar.")
        return

    title_orig = article["title"]
    url_orig = article["url"]
    image_url_orig = article["image_url"]

    # Baixa el text complet de l'article
    body_orig = _fetch_article_text(url_orig)
    if not body_orig:
        body_orig = article["desc_rss"]

    # Reescriptura amb DeepSeek (o fallback)
    use_llm = config.USE_LLM and not args.no_llm
    if use_llm:
        new_title, new_text = _rewrite_with_deepseek(title_orig, body_orig)
    else:
        new_title = _translate_title_fallback(title_orig)
        new_text = body_orig[:600] if body_orig else article["desc_rss"]

    # Peu de post: font
    footer = f"\n\n*(via [Mundo Deportivo]({url_orig}))*"

    # Preview
    print(f"\n--- ORIGINAL ---")
    print(f"Títol: {title_orig}")
    print(f"Text:  {body_orig[:300]}...")
    print(f"\n--- DEEPSEEK ({'' if use_llm else 'NO '}LLM) ---")
    print(f"Títol: {new_title}")
    print(f"Text:  {new_text}")
    print(f"Imatge original: {image_url_orig}")

    if not args.push:
        print("\n[--post: no s'encua res]")
        return

    # Re-allotja imatge a R2
    slug = re.sub(r"[^a-z0-9]+", "-", new_title.lower())[:40]
    image_url_r2 = _rehost_image(image_url_orig, slug)

    if not image_url_r2:
        log.error("No hi ha imatge disponible per al post d'imatge.")
        sys.exit(1)

    # Construeix payload
    comment = f"{new_text}{footer}"
    payload = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
        "tipus": "imatge",
        "title": new_title,
        "url": image_url_r2,
        "comment_markdown": comment,
        "subreddit": _SUBREDDIT,
    }

    # Encua
    queue_store.enqueue(
        payload,
        source=_QUEUE_SOURCE,
        source_label=_QUEUE_SOURCE_LABEL,
    )
    print(f"✅ Encuat [{_QUEUE_SOURCE_LABEL}]: {new_title[:60]} → imatge-{payload['generated_at'][:10]}")

    # Desa historial
    history.add(url_orig)
    _save_history(history)


if __name__ == "__main__":
    main()
