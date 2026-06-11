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
     {subreddit, title, kind: "image", url}. make el penja a r/AnimeCatala.

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

    Reutilitza el webhook MAKE_WEBHOOK_URL del canal. `kind: "image"` indica a
    make que publiqui un post d'imatge a partir de `url` (no un self post). Les
    claus extra (source_*, month) les ignora make; serveixen per a traçabilitat.
    """
    now = now or datetime.now(timezone.utc)
    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "subreddit": _SUBREDDIT,
        "title": build_title(month_year),
        "kind": "image",
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
    if target is None:
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
