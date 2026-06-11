#!/usr/bin/env python3
"""
bluesky_manga.py — Novetats mensuals de manga en català (Bluesky → make → Reddit).

Un cop al mes, el compte @samfainavisual publica un post amb la llista de
llançaments de manga en català del mes, amb una imatge. Aquest script:

  1. Baixa el feed públic de l'autor (API de Bluesky, sense autenticació).
  2. Selecciona el post mensual de forma DETERMINISTA: descarta reposts, busca la
     frase fixa "LLANÇAMENTS MANGA EN CATALÀ" i exigeix que sigui recent.
  3. Si el filtre no troba res, una XARXA DE SEGURETAT amb DeepSeek (acotada:
     només a principi de mes i si el mes no s'ha publicat) tria entre els textos
     recents. Si no hi ha clau o falla, queda determinista pur.
  4. Comprova que no s'hagi publicat ja (històric d'URIs).
  5. Envia un post d'IMATGE a make (mateix webhook del canal) amb
     {subreddit, title, tipus: "imatge", url}. Un router de make encamina segons
     `tipus` ("text"/"imatge") cap al mòdul de Reddit corresponent.

── Font de dades ──────────────────────────────────────────────────────────────
    https://public.api.bsky.app/xrpc/app.bsky.feed.getAuthorFeed
        ?actor=samfainavisual.bsky.social&limit=50&filter=posts_no_replies

Cada element de `feed` és {"post": {...}, "reason"?: {...}}. Si hi ha `reason`,
és una repost (es descarta). Camps del post: `uri` (clau de dedup),
`record.text`, `record.createdAt` (ISO 8601 amb Z), `embed.images[0].fullsize`.

── Ús ──────────────────────────────────────────────────────────────────────────
    python bluesky_manga.py            # preview (no publica)
    python bluesky_manga.py --post     # idem (explícit)
    python bluesky_manga.py --push     # envia a make (→ Reddit) i desa l'històric
    python bluesky_manga.py --no-llm   # desactiva la xarxa de seguretat DeepSeek
    python bluesky_manga.py --debug    # estadístiques crues del feed

El cron de dilluns executa `--push --quiet`. Reutilitza MAKE_WEBHOOK_URL (make
limita a 2 escenaris actius) i, per a la xarxa de seguretat, DEEPSEEK_API_KEY.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

# Reaprofitem config del projecte si hi és; si no, fallback autònom.
try:
    import config
    _HEADERS = config.HTTP_HEADERS
    _OUTPUT_DIR = config.OUTPUT_DIR
    _SUBREDDIT = getattr(config, "SUBREDDIT", "AnimeCatala")
    _ACTOR = getattr(config, "BSKY_ACTOR", "samfainavisual.bsky.social")
    _HISTORY_FILE = getattr(config, "BSKY_HISTORY_FILE",
                            _OUTPUT_DIR / "bsky_history.json")
    _WEBHOOK = (getattr(config, "MAKE_WEBHOOK_URL", "")
                or os.getenv("MAKE_WEBHOOK_URL", "")).strip()
except Exception:  # pragma: no cover - l'script ha de funcionar tot sol
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
    _ACTOR = os.getenv("BSKY_ACTOR", "samfainavisual.bsky.social")
    _HISTORY_FILE = _OUTPUT_DIR / "bsky_history.json"
    _WEBHOOK = os.getenv("MAKE_WEBHOOK_URL", "").strip()

log = logging.getLogger("anime-scraper.bsky")

# --------------------------------------------------------------------------- #
# Configuració                                                                #
# --------------------------------------------------------------------------- #
API_URL = "https://public.api.bsky.app/xrpc/app.bsky.feed.getAuthorFeed"
REQUEST_TIMEOUT = 20
REQUEST_RETRIES = 2
FEED_LIMIT = 50               # posts a demanar (un mes en sobren)
MAX_AGE_DAYS = 35             # finestra de recència del post mensual
LLM_DAY_LIMIT = 14            # la xarxa de seguretat només actua fins al dia 14

# Frase fixa del post mensual (comparació en MAJÚSCULES).
NEEDLE = "LLANÇAMENTS MANGA EN CATALÀ"

# Mesos en català (per al títol, el fallback des de createdAt i el month-key).
_MONTHS_CA = ["gener", "febrer", "març", "abril", "maig", "juny", "juliol",
              "agost", "setembre", "octubre", "novembre", "desembre"]


def _parse_iso(text: str) -> datetime:
    """'2026-06-03T12:33:27.666Z' → datetime amb tzinfo UTC."""
    return datetime.fromisoformat(text.strip().replace("Z", "+00:00"))


# --------------------------------------------------------------------------- #
# Selecció determinista del post mensual                                      #
# --------------------------------------------------------------------------- #
def select_monthly_post(feed: list[dict], now: datetime,
                        max_age_days: int = MAX_AGE_DAYS) -> Optional[dict]:
    """Tria el post mensual de novetats de manga (o None).

    Criteris (tots deterministes, sense LLM):
      - NO és una repost (l'ítem del feed no porta `reason`).
      - El text (en majúscules) conté la frase fixa NEEDLE.
      - `createdAt` és dins dels últims `max_age_days` dies respecte a `now`.
    Si n'hi ha més d'un, retorna el més recent. Retorna el dict del `post`.
    """
    candidates: list[tuple[datetime, dict]] = []
    cutoff = now - timedelta(days=max_age_days)
    for item in feed:
        if "reason" in item:          # repost → fora
            continue
        post = item.get("post") or {}
        record = post.get("record") or {}
        text = (record.get("text") or "").upper()
        if NEEDLE not in text:
            continue
        try:
            created = _parse_iso(record.get("createdAt", ""))
        except (ValueError, AttributeError):
            continue
        if created < cutoff:          # massa antic → fora
            continue
        candidates.append((created, post))
    if not candidates:
        return None
    candidates.sort(key=lambda c: c[0], reverse=True)
    return candidates[0][1]


# --------------------------------------------------------------------------- #
# Extracció de dades del post                                                 #
# --------------------------------------------------------------------------- #
_MONTH_RE = re.compile(r"DEL\s+MES\s+DE\s+([^\s!.,;:\n]+)", re.IGNORECASE)


def extract_month_year(text: str, created: datetime) -> str:
    """'… DEL MES DE JUNY!' + createdAt 2026 → 'juny 2026'.

    El mes ve del text si hi és i és un mes vàlid; si no, del `createdAt`.
    L'any sempre ve del `createdAt` (el post surt a l'inici del mes que cobreix).
    """
    month = _MONTHS_CA[created.month - 1]
    m = _MONTH_RE.search(text or "")
    if m:
        candidate = m.group(1).strip().lower()
        if candidate in _MONTHS_CA:
            month = candidate
    return f"{month} {created.year}"


def month_key(month_year: str) -> str:
    """'juny 2026' → '2026-06' (clau de gating per a la xarxa de seguretat)."""
    name, _, year = month_year.partition(" ")
    num = _MONTHS_CA.index(name) + 1   # name és un mes vàlid (ve d'extract_month_year)
    return f"{year}-{num:02d}"


def extract_image_url(post: dict) -> Optional[str]:
    """URL `fullsize` de la primera imatge del post, o None si no en té."""
    embed = post.get("embed") or {}
    images = embed.get("images") or []
    if images:
        return images[0].get("fullsize") or None
    return None


def extract_post_uri(post: dict) -> str:
    """URI at:// del post (clau de dedup)."""
    return post.get("uri", "")


# --------------------------------------------------------------------------- #
# Construcció del post i payload per a make                                    #
# --------------------------------------------------------------------------- #
def build_title(month_year: str) -> str:
    """Títol determinista del post de Reddit."""
    return (f"📚 Llançaments de manga en català — {month_year} "
            f"(via Samfaina Visual)")


def build_structured(post_uri: str, month_year: str, image_url: str,
                     now: Optional[datetime] = None) -> dict:
    """Dict per al webhook de make (post d'IMATGE → r/AnimeCatala).

    Reutilitza el webhook MAKE_WEBHOOK_URL del canal. `tipus: "imatge"` fa que el
    router de make encamini cap al mòdul de post d'imatge (a partir de `url`), en
    comptes del de text (`tipus: "text"`, que mapeja `markdown`). Les claus extra
    (source_*, month) les ignora make; serveixen per a traçabilitat.
    """
    now = now or datetime.now(timezone.utc)
    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "subreddit": _SUBREDDIT,
        "title": build_title(month_year),
        "tipus": "imatge",
        "url": image_url,
        "source": "bluesky/samfainavisual",
        "source_uri": post_uri,
        "month": month_year,
    }


# --------------------------------------------------------------------------- #
# Xarxa de seguretat amb DeepSeek (només si el filtre determinista falla)      #
# --------------------------------------------------------------------------- #
_LLM_SYSTEM_PROMPT = (
    "Analitza la següent llista de posts (cadascun amb 'id' i 'text'). "
    "Identifica quin correspon a l'anunci mensual de novetats de manga o anime "
    "en català. Retorna exclusivament l'id d'aquest post en format JSON: "
    '{"target_id": <id>}. Si cap post encaixa amb aquesta descripció, retorna '
    '{"target_id": null}.'
)


def llm_candidates(feed: list[dict], now: datetime,
                   max_age_days: int = MAX_AGE_DAYS) -> list[dict]:
    """Posts recents NO-repost com a {id, uri, text}, per passar a l'LLM."""
    cutoff = now - timedelta(days=max_age_days)
    out: list[dict] = []
    for item in feed:
        if "reason" in item:
            continue
        post = item.get("post") or {}
        record = post.get("record") or {}
        try:
            created = _parse_iso(record.get("createdAt", ""))
        except (ValueError, AttributeError):
            continue
        if created < cutoff:
            continue
        out.append({"id": len(out) + 1, "uri": post.get("uri", ""),
                    "text": record.get("text", "")})
    return out


def should_try_llm(now: datetime, history: dict,
                   day_limit: int = LLM_DAY_LIMIT) -> bool:
    """La xarxa de seguretat només actua a principi de mes i si encara no s'ha
    publicat el post d'aquest mes (acota les crides a ~1-2/mes)."""
    if now.day > day_limit:
        return False
    if now.strftime("%Y-%m") in history.get("months", set()):
        return False
    return True


def select_post_by_uri(feed: list[dict], uri: str) -> Optional[dict]:
    for item in feed:
        post = item.get("post") or {}
        if post.get("uri") == uri:
            return post
    return None


def llm_select_monthly_post(feed: list[dict], now: datetime,
                            use_llm: bool = True) -> Optional[dict]:
    """Demana a DeepSeek quin post recent és l'anunci mensual. Retorna el `post`
    o None. Si no hi ha clau / falla / no n'hi ha cap, retorna None (queda
    determinista pur). Mateix patró de fallback que sx3_schedule."""
    cands = llm_candidates(feed, now)
    if not cands or not use_llm:
        return None
    try:
        from processor import _deepseek_chat, _extract_json  # client del projecte
    except Exception:
        return None
    listing = [{"id": c["id"], "text": c["text"]} for c in cands]
    raw = _deepseek_chat(
        [
            {"role": "system", "content": _LLM_SYSTEM_PROMPT},
            {"role": "user", "content":
                "Posts:\n" + json.dumps(listing, ensure_ascii=False) +
                "\n\nRespon NOMÉS amb el JSON indicat."},
        ],
        temperature=0, max_tokens=60,
    )
    data = _extract_json(raw) if raw else None
    if not isinstance(data, dict):
        return None
    target = data.get("target_id")
    if target is None or isinstance(target, bool):
        return None
    try:
        target = int(target)
    except (TypeError, ValueError):
        return None
    match = next((c for c in cands if c["id"] == target), None)
    if not match:
        return None
    log.info("Xarxa de seguretat LLM: post triat id=%d (%s).",
             target, match["uri"])
    return select_post_by_uri(feed, match["uri"])


# --------------------------------------------------------------------------- #
# IO: feed, històric, make                                                     #
# --------------------------------------------------------------------------- #
def build_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(_HEADERS)
    return s


def fetch_feed(session: requests.Session, actor: str,
               limit: int = FEED_LIMIT) -> list[dict]:
    """Baixa el feed de l'autor. Llança RuntimeError si tots els intents fallen."""
    params = {"actor": actor, "limit": str(limit), "filter": "posts_no_replies"}
    last_exc: Optional[Exception] = None
    for attempt in range(1, REQUEST_RETRIES + 2):
        try:
            r = session.get(API_URL, params=params, timeout=REQUEST_TIMEOUT)
            r.raise_for_status()
            feed = r.json().get("feed", []) or []
            log.info("Feed de @%s: %d ítems.", actor, len(feed))
            return feed
        except (requests.RequestException, ValueError) as exc:
            last_exc = exc
            log.warning("Petició al feed fallida (%d/%d): %s",
                        attempt, REQUEST_RETRIES + 1, exc)
    raise RuntimeError(f"No s'ha pogut descarregar el feed de Bluesky: {last_exc}")


def load_history() -> dict:
    """Retorna {'uris': set, 'months': set}. Tolera el format antic (llista)."""
    if not _HISTORY_FILE.exists():
        return {"uris": set(), "months": set()}
    try:
        data = json.loads(_HISTORY_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {"uris": set(), "months": set()}
    if isinstance(data, list):                       # format antic: només URIs
        return {"uris": set(data), "months": set()}
    return {"uris": set(data.get("uris", [])),
            "months": set(data.get("months", []))}


def update_history(uri: str, month: str) -> None:
    """Afegeix l'URI publicada i el mes (YYYY-MM) a l'històric."""
    _HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    history = load_history()
    history["uris"].add(uri)
    history["months"].add(month)
    _HISTORY_FILE.write_text(
        json.dumps({"uris": sorted(history["uris"]),
                    "months": sorted(history["months"])},
                   ensure_ascii=False, indent=0),
        encoding="utf-8",
    )


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
# Per quan la connexió de Reddit de make no funciona (o no es vol fer servir).
# Copia el títol al porta-retalls, baixa la imatge a un fitxer i obre Reddit;
# tu només enganxes el títol, puges la imatge i cliques Post. Mateix patró
# (WSL → Windows) que publish_manual.py del recull setmanal.
def _image_ext(content_type: str) -> str:
    """Extensió de fitxer segons el Content-Type de la imatge (per defecte .jpg)."""
    ct = (content_type or "").lower()
    if "png" in ct:
        return ".png"
    if "webp" in ct:
        return ".webp"
    if "gif" in ct:
        return ".gif"
    return ".jpg"


def download_image(url: str, dest_base):
    """Baixa la imatge a `dest_base` + extensió segons el Content-Type. Retorna
    el Path del fitxer desat, o None si falla."""
    try:
        r = requests.get(url, headers=_HEADERS, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        log.error("No s'ha pogut baixar la imatge: %s", exc)
        return None
    dest = dest_base.with_suffix(_image_ext(r.headers.get("Content-Type", "")))
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(r.content)
    return dest


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


def _win_path(path) -> str:
    """Ruta Windows d'un fitxer WSL (per trobar-lo al diàleg de pujada)."""
    try:
        import subprocess
        return subprocess.check_output(["wslpath", "-w", str(path)]).decode().strip()
    except Exception:  # noqa: BLE001
        return str(path)


def run_manual(structured: dict, image_url: str, month_year: str) -> int:
    """Prepara el post d'imatge per publicar-lo a mà: copia el títol, baixa la
    imatge i obre Reddit. No fa servir make ni l'API de Reddit."""
    title = structured["title"]
    slug = month_year.replace(" ", "-")
    img = download_image(image_url, _OUTPUT_DIR / f"manga-{slug}")
    ok_clip = _to_clipboard(title)
    _open_browser(f"https://www.reddit.com/r/{_SUBREDDIT}/submit")

    print("\n" + "=" * 66)
    print("📋  TÍTOL " + ("(ja al porta-retalls → Ctrl+V al camp «Title»):"
                          if ok_clip else "(copia'l al camp «Title»):"))
    print("\n    " + title + "\n")
    if img:
        print("🖼️  IMATGE baixada — puja-la al post:")
        print("    " + _win_path(img))
    else:
        print("🖼️  No s'ha pogut baixar la imatge. URL per desar-la a mà:")
        print("    " + image_url)
    print("\nPassos a Reddit (s'ha obert al navegador):")
    print("   1) Tria  Type = Images & Video")
    print("   2) Title → enganxa el títol (Ctrl+V)")
    print("   3) Puja la imatge baixada (arrossega-la o tria el fitxer)")
    print("   4) Clica  Post")
    print("=" * 66)
    return 0


# --------------------------------------------------------------------------- #
# CLI                                                                         #
# --------------------------------------------------------------------------- #
def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Novetats mensuals de manga (Bluesky → make/Reddit).")
    p.add_argument("--post", action="store_true",
                   help="Preview del post (títol + URL d'imatge), sense publicar.")
    p.add_argument("--push", action="store_true",
                   help="Envia el post a make (MAKE_WEBHOOK_URL) i desa l'històric.")
    p.add_argument("--manual", action="store_true",
                   help="Publicació manual assistida: copia el títol, baixa la "
                        "imatge i obre Reddit (sense make ni API).")
    p.add_argument("--no-llm", action="store_true",
                   help="Desactiva la xarxa de seguretat amb DeepSeek.")
    p.add_argument("--debug", action="store_true",
                   help="Estadístiques crues del feed i surt.")
    p.add_argument("--quiet", action="store_true", help="Menys missatges.")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(levelname)-7s | %(message)s",
    )

    session = build_session()
    try:
        feed = fetch_feed(session, _ACTOR)
    except RuntimeError as exc:
        log.error("%s", exc)
        return 1

    if args.debug:
        print(f"\n[DEBUG] Feed de @{_ACTOR}: {len(feed)} ítems")
        reposts = sum(1 for i in feed if "reason" in i)
        matches = sum(
            1 for i in feed
            if "reason" not in i
            and NEEDLE in (i.get("post", {}).get("record", {}).get("text", "")).upper()
        )
        print(f"[DEBUG] reposts: {reposts} · matches no-repost: {matches}")
        for i in feed[:10]:
            rec = i.get("post", {}).get("record", {})
            tag = "REPOST" if "reason" in i else "post  "
            print(f"  {rec.get('createdAt','')[:10]} | {tag} | "
                  f"{(rec.get('text','') or '')[:60]!r}")
        return 0

    now = datetime.now(timezone.utc)
    history = load_history()

    # 1) Filtre determinista (primari).
    post = select_monthly_post(feed, now)
    # 2) Xarxa de seguretat LLM (secundària i acotada).
    if not post and not args.no_llm and should_try_llm(now, history):
        log.info("Cap match determinista; provo la xarxa de seguretat amb DeepSeek…")
        post = llm_select_monthly_post(feed, now, use_llm=True)

    if not post:
        log.info("Cap post mensual de novetats al feed (normal la majoria de "
                 "setmanes).")
        return 0

    uri = extract_post_uri(post)
    record = post.get("record", {})
    try:
        created = _parse_iso(record.get("createdAt", ""))
    except (ValueError, AttributeError):
        log.warning("Post mensual trobat (%s) però sense data vàlida; no es publica.",
                    uri)
        return 0
    month_year = extract_month_year(record.get("text", ""), created)
    image_url = extract_image_url(post)

    if not image_url:
        log.warning("Post mensual trobat (%s) però sense imatge; no es publica.",
                    uri)
        return 0

    # En mode manual no apliquem el dedup: el publiques tu quan vols.
    if uri in history["uris"] and not args.manual:
        log.info("El post mensual %s ja s'havia processat; res a fer.", uri)
        return 0

    structured = build_structured(uri, month_year, image_url, now)

    # Publicació manual assistida: prepara-ho tot i surt (no toca make ni històric).
    if args.manual:
        return run_manual(structured, image_url, month_year)

    # Preview (mode per defecte i --post): no publica.
    if not args.push:
        print("\n" + "=" * 64)
        print(f"TÍTOL: {structured['title']}")
        print(f"IMATGE: {structured['url']}")
        print(f"URI:   {uri}")
        print("=" * 64)
        return 0

    # Publicació: encua el post i, si s'ha encuat bé, actualitza l'històric.
    import queue_store
    if queue_store.enqueue(structured):
        update_history(uri, month_key(month_year))
        log.info("✅ Encuat per a l'extensió i desat a l'històric: %s", uri)
        return 0
    log.error("❌ No s'ha pogut encuar; no s'actualitza l'històric.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
