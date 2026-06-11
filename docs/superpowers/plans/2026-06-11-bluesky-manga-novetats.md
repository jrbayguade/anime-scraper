# Novetats mensuals de manga (Bluesky → make → Reddit) — Pla d'implementació

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publicar un cop al mes a r/AnimeCatala el post de "Llançaments de manga en català" del compte de Bluesky @samfainavisual, com a post d'imatge, reutilitzant l'únic webhook de make del canal.

**Architecture:** Un script autònom nou (`bluesky_manga.py`) que mirall·la l'estil de `sx3_schedule.py`: baixa el feed públic de Bluesky i selecciona el post mensual amb un filtre **determinista** (frase fixa + descartar reposts + finestra de recència). Si el filtre no troba res, hi ha una **xarxa de seguretat amb DeepSeek acotada** (només a principi de mes i si el mes encara no s'ha publicat) que tria entre els textos recents. El post escollit s'envia com a JSON `{subreddit, title, kind: "image", url}` al webhook de make. Dedup i gating via un històric JSON commitejat (`{uris, months}`).

**Tech Stack:** Python 3.12, `requests` (ja al projecte), DeepSeek reutilitzat via `processor._deepseek_chat` (opcional), pytest (només dev local), GitHub Actions.

---

## Estructura de fitxers

**Es creen:**
- `bluesky_manga.py` — script autònom: feed → selecció determinista (+xarxa LLM) → post d'imatge → `--push` a make. Responsabilitat única: el flux mensual de manga.
- `tests/test_bluesky_manga.py` — tests unitaris de les funcions pures.
- `tests/fixtures/samfaina_feed.json` — fixture retallat del feed real (5 ítems representatius).
- `tests/__init__.py` — buit (perquè pytest trobi el paquet).
- `.github/workflows/manga-novetats.yml` — cron de dilluns + dispatch manual.

**Es modifiquen:**
- `config.py` — afegeix `BSKY_ACTOR` i `BSKY_HISTORY_FILE`.
- `processor.py` — afegeix `"kind": "self"` al payload del recull setmanal.
- `sx3_schedule.py` — afegeix `"kind": "self"` al payload de la graella.
- `CLAUDE.md` — documenta el flux nou.
- `README.md` — secció del flux nou + estructura.

**Convencions a respectar** (del codi existent): tot en català (comentaris, logs, CLI); una font/crida que falla no tomba el procés (es registra i es continua); secrets només via `.env`/secrets; un sol webhook de make per al canal; la xarxa de seguretat LLM segueix el patró de fallback de `sx3_schedule.generate_intro_question` (si DeepSeek no respon, es continua sense ell).

---

## Task 1: Configuració i fixture de proves

**Files:**
- Modify: `config.py` (després de la línia 171, `MAKE_BODY_MAX_ENCODED = 7500`)
- Create: `tests/fixtures/samfaina_feed.json`
- Create: `tests/__init__.py` (buit)

- [ ] **Step 1: Afegeix la configuració de Bluesky a `config.py`**

Insereix aquest bloc just després de la línia `MAKE_BODY_MAX_ENCODED = 7500` (línia 171):

```python

# --------------------------------------------------------------------------- #
# Bluesky — novetats mensuals de manga (@samfainavisual)                       #
# --------------------------------------------------------------------------- #
# Compte de Bluesky d'on s'extreu el post mensual de "Llançaments de manga en
# català". La detecció és determinista (frase fixa); DeepSeek (config DEEPSEEK_*
# de més amunt) només actua de xarxa de seguretat acotada quan el filtre falla.
BSKY_ACTOR = os.getenv("BSKY_ACTOR", "samfainavisual.bsky.social").strip()

# Històric del flux de manga: {"uris": [...], "months": [...]}. Independent de
# HISTORY_FILE (recull setmanal). `uris` evita repetir posts; `months` (YYYY-MM)
# evita cridar la xarxa de seguretat LLM un cop el mes ja s'ha publicat.
BSKY_HISTORY_FILE = OUTPUT_DIR / "bsky_history.json"
```

- [ ] **Step 2: Crea el fixture del feed (`tests/fixtures/samfaina_feed.json`)**

5 ítems triats per cobrir tots els casos: post de juny (s'ha de triar), repost d'un post mensual de juliol més recent (descartar tot i ser el match més nou), repost de Planeta sense la frase (descartar), post de maig (match però fora de la finestra de 35 dies respecte al 10 de juny), i un post normal sense imatge ni frase (el més nou de tots).

```json
{
  "feed": [
    {
      "post": {
        "uri": "at://did:plc:xf2/app.bsky.feed.post/RANDOM",
        "cid": "bafyrandom",
        "record": {
          "$type": "app.bsky.feed.post",
          "text": "Així comença \"Fullmetal Alchemist\" en CATALÀ!",
          "createdAt": "2026-06-09T10:00:00.000Z",
          "langs": ["ca"]
        }
      }
    },
    {
      "post": {
        "uri": "at://did:plc:xf2/app.bsky.feed.post/REPOSTJULIOL",
        "cid": "bafyjuliol",
        "record": {
          "$type": "app.bsky.feed.post",
          "text": "🗓️ LLANÇAMENTS MANGA EN CATALÀ DEL MES DE JULIOL!",
          "createdAt": "2026-06-08T10:00:00.000Z",
          "langs": ["ca"]
        },
        "embed": {
          "$type": "app.bsky.embed.images#view",
          "images": [
            {"thumb": "x", "fullsize": "https://cdn.bsky.app/fullsize/juliol.jpg", "alt": ""}
          ]
        }
      },
      "reason": {"$type": "app.bsky.feed.defs#reasonRepost"}
    },
    {
      "post": {
        "uri": "at://did:plc:xf2/app.bsky.feed.post/JUNY",
        "cid": "bafyjuny",
        "record": {
          "$type": "app.bsky.feed.post",
          "text": "🗓️ LLANÇAMENTS MANGA EN CATALÀ DEL MES DE JUNY!  \n\nAquests son els 6 mangues que sortiran aquest mes en la nostra llengua:",
          "createdAt": "2026-06-03T12:33:27.666Z",
          "langs": ["ca"]
        },
        "embed": {
          "$type": "app.bsky.embed.images#view",
          "images": [
            {"thumb": "https://cdn.bsky.app/thumb/juny.jpg", "fullsize": "https://cdn.bsky.app/fullsize/juny.jpg", "alt": "", "aspectRatio": {"width": 1080, "height": 1350}}
          ]
        }
      }
    },
    {
      "post": {
        "uri": "at://did:plc:zse/app.bsky.feed.post/PLANETA",
        "cid": "bafyplaneta",
        "record": {
          "$type": "app.bsky.feed.post",
          "text": "Novedades de Planeta Cómic: manga en castellano y catalán.",
          "createdAt": "2026-05-06T16:00:21.000Z",
          "langs": ["es"]
        },
        "embed": {
          "$type": "app.bsky.embed.images#view",
          "images": [
            {"thumb": "x", "fullsize": "https://cdn.bsky.app/fullsize/planeta.jpg", "alt": ""}
          ]
        }
      },
      "reason": {"$type": "app.bsky.feed.defs#reasonRepost"}
    },
    {
      "post": {
        "uri": "at://did:plc:xf2/app.bsky.feed.post/MAIG",
        "cid": "bafymaig",
        "record": {
          "$type": "app.bsky.feed.post",
          "text": "🗓️ LLANÇAMENTS MANGA EN CATALÀ DEL MES DE MAIG!\n\nSakura 7 - Ja disponible",
          "createdAt": "2026-05-04T09:36:07.843Z",
          "langs": ["ca"]
        },
        "embed": {
          "$type": "app.bsky.embed.images#view",
          "images": [
            {"thumb": "x", "fullsize": "https://cdn.bsky.app/fullsize/maig.jpg", "alt": ""}
          ]
        }
      }
    }
  ]
}
```

- [ ] **Step 3: Crea `tests/__init__.py` buit i instal·la pytest (dev)**

```bash
mkdir -p tests/fixtures
: > tests/__init__.py
pip install pytest
```

Expected: pytest s'instal·la sense errors. (pytest NO va a `requirements.txt`: la CI només executa l'script.)

- [ ] **Step 4: Commit**

```bash
git add config.py tests/__init__.py tests/fixtures/samfaina_feed.json
git commit -m "feat: config de Bluesky (actor + històric) i fixture de proves"
```

---

## Task 2: Esquelet del mòdul + selecció determinista

**Files:**
- Create: `bluesky_manga.py`
- Test: `tests/test_bluesky_manga.py`

- [ ] **Step 1: Crea l'esquelet de `bluesky_manga.py` (imports, config, constants, helper)**

```python
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
```

- [ ] **Step 2: Escriu el test que falla per a `select_monthly_post`**

Crea `tests/test_bluesky_manga.py`:

```python
import json
import pathlib
from datetime import datetime, timezone

import bluesky_manga as bm

_FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "samfaina_feed.json"


def load_feed():
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))["feed"]


def post_by_uri(feed, frag):
    return next(i["post"] for i in feed if frag in i["post"]["uri"])


NOW = datetime(2026, 6, 10, tzinfo=timezone.utc)


def test_selects_most_recent_non_repost_monthly_post():
    post = bm.select_monthly_post(load_feed(), NOW)
    assert post is not None
    assert post["uri"].endswith("/JUNY")


def test_ignores_reposts_even_if_newer_match():
    # La repost de JULIOL és un match més recent que JUNY però s'ha de descartar.
    post = bm.select_monthly_post(load_feed(), NOW)
    assert "REPOSTJULIOL" not in post["uri"]


def test_returns_none_when_no_monthly_post():
    feed = [i for i in load_feed() if i["post"]["uri"].endswith("/RANDOM")]
    assert bm.select_monthly_post(feed, NOW) is None


def test_respects_recency_window():
    # Molt al futur: tots els matches queden fora dels 35 dies.
    far = datetime(2026, 8, 1, tzinfo=timezone.utc)
    assert bm.select_monthly_post(load_feed(), far) is None
```

- [ ] **Step 3: Executa els tests i comprova que fallen**

Run: `python -m pytest tests/test_bluesky_manga.py -v`
Expected: FAIL amb `AttributeError: module 'bluesky_manga' has no attribute 'select_monthly_post'`.

- [ ] **Step 4: Implementa `select_monthly_post` (afegeix al final de `bluesky_manga.py`)**

```python


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
```

- [ ] **Step 5: Executa els tests i comprova que passen**

Run: `python -m pytest tests/test_bluesky_manga.py -v`
Expected: PASS (4 passed).

- [ ] **Step 6: Commit**

```bash
git add bluesky_manga.py tests/test_bluesky_manga.py
git commit -m "feat: selecció determinista del post mensual de manga (Bluesky)"
```

---

## Task 3: Extracció de mes/any i month-key

**Files:**
- Modify: `bluesky_manga.py` (afegeix funcions al final)
- Test: `tests/test_bluesky_manga.py` (afegeix tests)

- [ ] **Step 1: Escriu els tests que fallen**

Afegeix a `tests/test_bluesky_manga.py`:

```python
def test_extract_month_year_from_text():
    created = datetime(2026, 6, 3, tzinfo=timezone.utc)
    assert bm.extract_month_year("DEL MES DE JUNY!", created) == "juny 2026"


def test_extract_month_year_handles_accented_month():
    created = datetime(2026, 3, 1, tzinfo=timezone.utc)
    assert bm.extract_month_year("...DEL MES DE MARÇ!", created) == "març 2026"


def test_extract_month_year_falls_back_to_createdat():
    created = datetime(2026, 5, 4, tzinfo=timezone.utc)
    assert bm.extract_month_year("sense mes al text", created) == "maig 2026"


def test_month_key():
    assert bm.month_key("juny 2026") == "2026-06"
    assert bm.month_key("març 2026") == "2026-03"
```

- [ ] **Step 2: Executa i comprova que fallen**

Run: `python -m pytest tests/test_bluesky_manga.py -k "month_year or month_key" -v`
Expected: FAIL amb `AttributeError` per `extract_month_year` / `month_key`.

- [ ] **Step 3: Implementa les funcions (afegeix al final de `bluesky_manga.py`)**

```python


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
```

- [ ] **Step 4: Executa i comprova que passen**

Run: `python -m pytest tests/test_bluesky_manga.py -k "month_year or month_key" -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add bluesky_manga.py tests/test_bluesky_manga.py
git commit -m "feat: extracció de mes/any i month-key del post mensual"
```

---

## Task 4: Extracció d'imatge i URI

**Files:**
- Modify: `bluesky_manga.py` (afegeix funcions al final)
- Test: `tests/test_bluesky_manga.py` (afegeix tests)

- [ ] **Step 1: Escriu els tests que fallen**

Afegeix a `tests/test_bluesky_manga.py`:

```python
def test_extract_image_url_returns_fullsize():
    post = post_by_uri(load_feed(), "/JUNY")
    assert bm.extract_image_url(post) == "https://cdn.bsky.app/fullsize/juny.jpg"


def test_extract_image_url_none_when_no_embed():
    post = post_by_uri(load_feed(), "/RANDOM")
    assert bm.extract_image_url(post) is None


def test_extract_post_uri():
    post = post_by_uri(load_feed(), "/JUNY")
    assert bm.extract_post_uri(post).endswith("/JUNY")
```

- [ ] **Step 2: Executa i comprova que fallen**

Run: `python -m pytest tests/test_bluesky_manga.py -k "image_url or post_uri" -v`
Expected: FAIL amb `AttributeError` per `extract_image_url` / `extract_post_uri`.

- [ ] **Step 3: Implementa les funcions (afegeix al final de `bluesky_manga.py`)**

```python


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
```

- [ ] **Step 4: Executa i comprova que passen**

Run: `python -m pytest tests/test_bluesky_manga.py -k "image_url or post_uri" -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add bluesky_manga.py tests/test_bluesky_manga.py
git commit -m "feat: extracció d'URL d'imatge i URI del post"
```

---

## Task 5: Títol i payload per a make

**Files:**
- Modify: `bluesky_manga.py` (afegeix funcions al final)
- Test: `tests/test_bluesky_manga.py` (afegeix tests)

- [ ] **Step 1: Escriu els tests que fallen**

Afegeix a `tests/test_bluesky_manga.py`:

```python
def test_build_title():
    assert bm.build_title("juny 2026") == (
        "📚 Llançaments de manga en català — juny 2026 (via Samfaina Visual)"
    )


def test_build_structured_has_image_contract():
    s = bm.build_structured(
        "at://x/JUNY", "juny 2026", "https://cdn.bsky.app/fullsize/juny.jpg",
        now=datetime(2026, 6, 10, tzinfo=timezone.utc),
    )
    assert s["subreddit"] == "AnimeCatala"
    assert s["kind"] == "image"
    assert s["url"] == "https://cdn.bsky.app/fullsize/juny.jpg"
    assert s["title"].endswith("(via Samfaina Visual)")
    assert s["source_uri"] == "at://x/JUNY"
```

- [ ] **Step 2: Executa i comprova que fallen**

Run: `python -m pytest tests/test_bluesky_manga.py -k "build_title or build_structured" -v`
Expected: FAIL amb `AttributeError` per `build_title` / `build_structured`.

- [ ] **Step 3: Implementa les funcions (afegeix al final de `bluesky_manga.py`)**

```python


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
```

- [ ] **Step 4: Executa i comprova que passa**

Run: `python -m pytest tests/test_bluesky_manga.py -k "build_title or build_structured" -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add bluesky_manga.py tests/test_bluesky_manga.py
git commit -m "feat: títol determinista i payload d'imatge per a make"
```

---

## Task 6: Xarxa de seguretat amb DeepSeek (acotada)

Aquesta tasca afegeix la part secundària: només actua quan el filtre determinista no troba res i estem a principi de mes sense haver publicat. Les funcions pures (candidats, gating, mapatge) es proven; la crida real a DeepSeek es verifica manualment.

**Files:**
- Modify: `bluesky_manga.py` (afegeix funcions al final)
- Test: `tests/test_bluesky_manga.py` (afegeix tests)

- [ ] **Step 1: Escriu els tests que fallen**

Afegeix a `tests/test_bluesky_manga.py`:

```python
def test_llm_candidates_excludes_reposts_and_old():
    cands = bm.llm_candidates(load_feed(), NOW)
    uris = [c["uri"] for c in cands]
    # RANDOM (9 juny) i JUNY (3 juny) entren; MAIG (>35 dies) i reposts, no.
    assert any(u.endswith("/RANDOM") for u in uris)
    assert any(u.endswith("/JUNY") for u in uris)
    assert not any("REPOST" in u for u in uris)
    assert not any(u.endswith("/MAIG") for u in uris)
    # Cada candidat porta un id enter i el text.
    assert all(isinstance(c["id"], int) and "text" in c for c in cands)


def test_should_try_llm_gating():
    history = {"uris": set(), "months": set()}
    early = datetime(2026, 6, 5, tzinfo=timezone.utc)
    late = datetime(2026, 6, 20, tzinfo=timezone.utc)
    # Principi de mes, mes no publicat → sí.
    assert bm.should_try_llm(early, history) is True
    # Passat el dia 14 → no.
    assert bm.should_try_llm(late, history) is False
    # Mes ja publicat → no, encara que sigui principi de mes.
    assert bm.should_try_llm(early, {"uris": set(), "months": {"2026-06"}}) is False


def test_select_post_by_uri():
    feed = load_feed()
    post = bm.select_post_by_uri(feed, post_by_uri(feed, "/JUNY")["uri"])
    assert post["uri"].endswith("/JUNY")
    assert bm.select_post_by_uri(feed, "at://inexistent") is None
```

- [ ] **Step 2: Executa i comprova que fallen**

Run: `python -m pytest tests/test_bluesky_manga.py -k "llm_candidates or should_try_llm or select_post_by_uri" -v`
Expected: FAIL amb `AttributeError` per `llm_candidates` / `should_try_llm` / `select_post_by_uri`.

- [ ] **Step 3: Implementa les funcions pures + la crida (afegeix al final de `bluesky_manga.py`)**

```python


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
```

- [ ] **Step 4: Executa i comprova que passen**

Run: `python -m pytest tests/test_bluesky_manga.py -k "llm_candidates or should_try_llm or select_post_by_uri" -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Tota la suite verda**

Run: `python -m pytest tests/test_bluesky_manga.py -v`
Expected: PASS (~16 passed).

- [ ] **Step 6: Commit**

```bash
git add bluesky_manga.py tests/test_bluesky_manga.py
git commit -m "feat: xarxa de seguretat amb DeepSeek acotada (gating + mapatge)"
```

---

## Task 7: IO (feed, històric, make) i CLI/main

Parts amb efectes (xarxa/fitxers) i orquestració. Sense test unitari (mirall del patró de `sx3_schedule.py`); es verifica manualment.

**Files:**
- Modify: `bluesky_manga.py` (afegeix al final)

- [ ] **Step 1: Afegeix sessió HTTP i descàrrega del feed**

Afegeix al final de `bluesky_manga.py`:

```python


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
```

- [ ] **Step 2: Afegeix l'històric (format {uris, months}) i l'enviament a make**

Afegeix al final de `bluesky_manga.py`:

```python


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
```

- [ ] **Step 3: Afegeix la CLI i el `main()`**

Afegeix al final de `bluesky_manga.py`:

```python


# --------------------------------------------------------------------------- #
# CLI                                                                         #
# --------------------------------------------------------------------------- #
def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Novetats mensuals de manga (Bluesky → make → Reddit).")
    p.add_argument("--post", action="store_true",
                   help="Preview del post (títol + URL d'imatge), sense publicar.")
    p.add_argument("--push", action="store_true",
                   help="Envia el post a make (MAKE_WEBHOOK_URL) i desa l'històric.")
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
    created = _parse_iso(record["createdAt"])
    month_year = extract_month_year(record.get("text", ""), created)
    image_url = extract_image_url(post)

    if not image_url:
        log.warning("Post mensual trobat (%s) però sense imatge; no es publica.",
                    uri)
        return 0

    if uri in history["uris"]:
        log.info("El post mensual %s ja s'havia processat; res a fer.", uri)
        return 0

    structured = build_structured(uri, month_year, image_url, now)

    # Preview (mode per defecte i --post): no publica.
    if not args.push:
        print("\n" + "=" * 64)
        print(f"TÍTOL: {structured['title']}")
        print(f"IMATGE: {structured['url']}")
        print(f"URI:   {uri}")
        print("=" * 64)
        return 0

    # Publicació: només actualitzem l'històric si make respon bé.
    if push_to_make(structured, _WEBHOOK):
        update_history(uri, month_key(month_year))
        log.info("✅ Publicat i desat a l'històric: %s", uri)
        return 0
    log.error("❌ No s'ha pogut enviar a make; no s'actualitza l'històric.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Verificació manual — `--debug` contra el feed real**

Run: `python bluesky_manga.py --debug`
Expected: imprimeix nombre d'ítems, reposts i matches; ≥1 match no-repost si hi ha post mensual recent. Sense errors d'import ni de xarxa.

- [ ] **Step 5: Verificació manual — `--post` (preview, no publica)**

Run: `python bluesky_manga.py --post`
Expected: si hi ha post mensual recent, imprimeix TÍTOL (`📚 Llançaments de manga en català — <mes> <any> (via Samfaina Visual)`), IMATGE (URL `cdn.bsky.app/.../fullsize/...`) i URI. **No** crea `output/bsky_history.json`.

- [ ] **Step 6: Commit**

```bash
git add bluesky_manga.py
git commit -m "feat: IO (feed, històric, make) i CLI/main de bluesky_manga"
```

---

## Task 8: Afegir `kind: "self"` als fluxos de text existents

**Files:**
- Modify: `processor.py:312-325` (dict `structured`)
- Modify: `sx3_schedule.py:465-472` (dict de retorn de `build_structured`)

- [ ] **Step 1: Afegeix `"kind": "self"` a `processor.py`**

A `processor.py`, dins del dict `structured`, just després de `"subreddit": config.SUBREDDIT,` (línia 316):

```python
        "subreddit": config.SUBREDDIT,
        "kind": "self",
        "title": title,
```

- [ ] **Step 2: Afegeix `"kind": "self"` a `sx3_schedule.py`**

A `sx3_schedule.py`, dins del dict que retorna `build_structured`, just després de `"subreddit": _SUBREDDIT,` (línia 469):

```python
        "subreddit": _SUBREDDIT,
        "kind": "self",
        "title": post["title"],
```

- [ ] **Step 3: Verificació manual — SX3 genera `kind: "self"`**

Run: `python -c "import sx3_schedule as s; print(s.build_structured({'title':'t','markdown':'m'}, []).get('kind'))"`
Expected: `self`

- [ ] **Step 4: Commit**

```bash
git add processor.py sx3_schedule.py
git commit -m "feat: marca kind=self als posts de text (recull i SX3)"
```

---

## Task 9: Workflow de GitHub Actions

**Files:**
- Create: `.github/workflows/manga-novetats.yml`

- [ ] **Step 1: Crea el workflow**

```yaml
name: Manga · Novetats mensuals (dilluns)

# Quan s'executa:
on:
  schedule:
    # Cada DILLUNS a les 09:00 UTC (10:00 hivern / 11:00 estiu, hora de Catalunya).
    # Una hora després del recull setmanal (08:00) per no col·lidir amb el seu push.
    # El filtre de duplicats fa la majoria d'execucions idempotents (no publiquen res).
    - cron: "0 9 * * 1"
  workflow_dispatch: {}   # botó per executar-lo a mà des de la pestanya "Actions"

# Permís perquè el job pugui desar l'històric (output/bsky_history.json) al repo
permissions:
  contents: write

# Evita execucions solapades
concurrency:
  group: manga-novetats
  cancel-in-progress: false

jobs:
  novetats:
    runs-on: ubuntu-latest
    timeout-minutes: 15
    steps:
      - name: Descarrega el codi
        uses: actions/checkout@v4

      - name: Prepara Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Instal·la dependències
        run: pip install -r requirements.txt

      - name: Cerca el post mensual i, si n'hi ha de nou, publica'l a make.com
        env:
          # Reutilitza el webhook del canal (mateix escenari → r/AnimeCatala).
          MAKE_WEBHOOK_URL: ${{ secrets.MAKE_WEBHOOK_URL }}
          # Només per a la xarxa de seguretat (si el filtre determinista falla).
          DEEPSEEK_API_KEY: ${{ secrets.DEEPSEEK_API_KEY }}
        run: python bluesky_manga.py --push --quiet

      - name: Desa l'històric (per no repetir el post el mes vinent)
        if: always()
        run: |
          git config user.name "github-actions[bot]"
          git config user.email "github-actions[bot]@users.noreply.github.com"
          git add -f output/bsky_history.json || true
          if git diff --cached --quiet; then
            echo "Res nou a desar."
          else
            git pull --rebase --autostash || true
            git commit -m "chore: històric de novetats de manga [skip ci]"
            git push
          fi
```

- [ ] **Step 2: Verificació — sintaxi YAML vàlida**

Run: `python -c "import yaml; yaml.safe_load(open('.github/workflows/manga-novetats.yml')); print('YAML OK')"`
Expected: `YAML OK` (si `yaml` no està instal·lat: `pip install pyyaml` abans, o omet i revisa visualment).

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/manga-novetats.yml
git commit -m "ci: workflow mensual de novetats de manga (dilluns)"
```

---

## Task 10: Documentació — CLAUDE.md

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Afegeix la tercera sortida a "## Què és"**

Després del punt 2 (Graella de SX3):

```markdown
3. **Novetats de manga** (dilluns) — `bluesky_manga.py`. Un cop al mes, agafa el
   post de "Llançaments de manga en català" del compte de Bluesky @samfainavisual
   i el publica com a **post d'imatge** (títol + URL). Detecció determinista; amb
   una **xarxa de seguretat DeepSeek acotada** si el filtre per frase falla.
```

- [ ] **Step 2: Actualitza el contracte del webhook**

A "### Contracte del webhook", substitueix el bloc JSON i afegeix la nota del `kind`:

```markdown
Envia un JSON a `MAKE_WEBHOOK_URL` amb (com a mínim) aquestes claus, que és el
que l'escenari mapeja:

​```json
{ "subreddit": "AnimeCatala", "kind": "self", "title": "…", "markdown": "… (cos del post) …" }
​```

El camp **`kind`** indica el tipus de post a Reddit:
- `"self"` → post de text (recull setmanal i graella de SX3): mapeja `markdown`.
- `"image"` → post d'imatge (novetats de manga): mapeja `url` (sense `markdown`).

`markdown` és el cos complet del post (ja muntat). Hi pot haver més claus; make
ignora les que no mapeja.
```

- [ ] **Step 3: Afegeix files a les taules**

A "## Fitxers clau":

```markdown
| `bluesky_manga.py` | Novetats mensuals de manga (autònom): feed de Bluesky → selecció determinista (+xarxa DeepSeek acotada) → `--push` a make com a post d'imatge. |
```

A "## Automatització (GitHub Actions)":

```markdown
| `.github/workflows/manga-novetats.yml` | dilluns 09:00 UTC | `python bluesky_manga.py --push --quiet` (novetats de manga) |
```

- [ ] **Step 4: Afegeix la secció de la font de Bluesky**

Després de "## Font de dades de SX3":

```markdown
## Font de dades de novetats de manga (Bluesky)

API pública de Bluesky, sense autenticació (`requests` pur):

​```
https://public.api.bsky.app/xrpc/app.bsky.feed.getAuthorFeed
    ?actor=samfainavisual.bsky.social&limit=50&filter=posts_no_replies
​```

- Cada ítem de `feed` és `{"post": {...}, "reason"?: {...}}`. Si porta `reason`,
  és una **repost** (es descarta).
- Camps: `post.uri` (clau de dedup), `post.record.text`, `post.record.createdAt`
  (ISO 8601), `post.embed.images[0].fullsize` (imatge).
- **Detecció determinista**: el post mensual conté la frase fixa
  `LLANÇAMENTS MANGA EN CATALÀ` i ha de ser recent (≤35 dies). El títol de Reddit
  es genera amb plantilla; el mes surt de `DEL MES DE <MES>` al text.
- **Xarxa de seguretat (DeepSeek)**: si el filtre no troba res i som dins dels
  primers 14 dies del mes sense haver-lo publicat, es demana a DeepSeek que triï
  entre els textos recents (~1-2 crides/mes com a molt). `--no-llm` la desactiva.
- L'històric és `output/bsky_history.json` (`{uris, months}`), independent del
  del recull setmanal.
```

- [ ] **Step 5: Actualitza "Com executar-ho en local"**

Afegeix al bloc de comandes:

```markdown
# Novetats de manga (Bluesky)
python bluesky_manga.py --debug             # estadístiques crues del feed
python bluesky_manga.py --post              # preview (títol + URL d'imatge)
python bluesky_manga.py --push              # publica a make si hi ha post nou
python bluesky_manga.py --no-llm --post     # només filtre determinista
```

- [ ] **Step 6: Commit**

```bash
git add CLAUDE.md
git commit -m "docs(CLAUDE): documenta el flux de novetats de manga (Bluesky)"
```

---

## Task 11: Documentació — README.md

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Afegeix la secció del flux nou (després de "## 🔞 Filtre de contingut", abans de "## 📁 Estructura")**

```markdown
## 📚 Novetats mensuals de manga (Bluesky → Reddit)

A banda del recull setmanal i la graella de SX3, `bluesky_manga.py` publica un cop
al mes les **novetats de manga en català** que anuncia el compte de Bluesky
[@samfainavisual](https://bsky.app/profile/samfainavisual.bsky.social), com a
**post d'imatge** a r/AnimeCatala (títol + la imatge amb la llista de llançaments).

```
Dilluns 09:00 UTC → GitHub Actions executa bluesky_manga.py --push
   → busca el post mensual al feed de Bluesky (filtre determinista)
        → POST {kind:"image", url} al webhook de make → make publica a Reddit
```

- **Detecció sense soroll:** descarta reposts i busca la frase fixa
  `LLANÇAMENTS MANGA EN CATALÀ` en posts recents (≤35 dies). Si el compte canviés
  la redacció, hi ha una **xarxa de seguretat amb DeepSeek** que només s'activa a
  principi de mes i si el mes encara no s'ha publicat (~1-2 crides/mes com a molt).
- **Sense duplicats:** `output/bsky_history.json` recorda els posts ja publicats.

```bash
python bluesky_manga.py --debug    # estadístiques crues del feed
python bluesky_manga.py --post     # preview (títol + URL d'imatge), no publica
python bluesky_manga.py --push     # publica a make si hi ha un post nou
python bluesky_manga.py --no-llm --post   # només filtre determinista
```

**Secrets del workflow** (Settings → Secrets → Actions, ja compartits):
`MAKE_WEBHOOK_URL` (sempre) i `DEEPSEEK_API_KEY` (només per a la xarxa de seguretat).
```

- [ ] **Step 2: Actualitza l'arbre d'estructura ("## 📁 Estructura")**

Substitueix el bloc de l'arbre per:

```markdown
```
anime-scraper/
├── main.py            # Orquestra el recull setmanal
├── scraper.py         # Descàrrega i parsing de les fonts (recull)
├── processor.py       # Resum (DeepSeek), post Markdown, JSON, històric (recull)
├── sx3_schedule.py    # Graella d'anime de SX3 (divendres)
├── bluesky_manga.py   # Novetats mensuals de manga des de Bluesky (dilluns)
├── config.py          # Configuració (fonts, delays, claus, filtres)
├── requirements.txt
├── run.sh / run.bat   # Executors per a Linux-Mac / Windows
├── .env.example       # Plantilla de variables d'entorn
├── output/
│   ├── posts/         # Posts .md + latest.json
│   ├── history.json   # Notícies ja publicades (recull setmanal)
│   └── bsky_history.json  # Posts de manga ja publicats (Bluesky)
└── logs/              # Logs per dia
```
```

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs(README): documenta el flux de novetats de manga (Bluesky)"
```

---

## Task 12: Verificació integral (manual)

**Files:** cap (només execució)

- [ ] **Step 1: Tota la suite de tests passa**

Run: `python -m pytest tests/ -v`
Expected: PASS (totes, ~16 tests).

- [ ] **Step 2: Prova end-to-end contra un subreddit de proves**

> ⚠️ Abans de tocar producció, canvia temporalment el destí: exporta
> `SUBREDDIT=<usuari_de_proves>` (p. ex. `u_<usuari>`) **o** edita l'escenari de
> make perquè publiqui al subreddit de proves.

Prerequisit (un cop, manual, fora del repo): a make, edita l'escenari del webhook
perquè el mòdul de Reddit mapegi `kind` i `url` (a més de `title`/`markdown`).

Run: `python bluesky_manga.py --push`
Expected: HTTP 2xx de make i, a Reddit (subreddit de proves), un post d'imatge amb
el títol correcte i la imatge de les novetats. Es crea/actualitza
`output/bsky_history.json` amb l'URI i el mes.

- [ ] **Step 3: Dedup — segona execució no publica**

Run: `python bluesky_manga.py --push`
Expected: log "El post mensual … ja s'havia processat; res a fer." i **cap** post nou.

- [ ] **Step 4: Els fluxos de text segueixen funcionant amb `kind: "self"`**

Run (contra el subreddit de proves): `python sx3_schedule.py --push`
Expected: make publica un self post (text) correctament; `kind: "self"` no trenca
el mapatge.

- [ ] **Step 5: Restaura el destí de producció**

Reverteix el canvi del pas 2 (`SUBREDDIT=AnimeCatala` i/o l'escenari de make).
Confirma amb `python bluesky_manga.py --post` que TÍTOL i IMATGE són correctes.

- [ ] **Step 6: Commit de qualsevol ajust final** (si cal)

```bash
git add -A
git commit -m "chore: ajustos finals del flux de novetats de manga"
```

---

## Notes per a l'implementador

- **Edició de make:** és **manual** i fora del repo (la fa l'usuari). El codi
  garanteix el payload (`kind` + `url` per a imatge, `kind` + `markdown` per a
  text); el mapatge a l'escenari de Reddit el fa l'usuari.
- **Xarxa de seguretat, no automatisme cec:** el filtre determinista mana
  (precisió). L'LLM només omple el buit, acotat per dia del mes i estat publicat,
  per no disparar-se cada dilluns (quan normalment no hi ha post nou).
- **Recència (35 dies):** evita pescar un post del mes anterior a la 1a execució.
- **Risc acceptat:** si el compte reedita el post (URI nova) el mateix mes, es
  podria tornar a publicar. Poc probable; s'assumeix.
- **DeepSeek opcional:** sense `DEEPSEEK_API_KEY` tot funciona en determinista pur
  (la xarxa de seguretat retorna `None`).
```
