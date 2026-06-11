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
