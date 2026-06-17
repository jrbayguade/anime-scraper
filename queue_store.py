"""
queue_store.py — Cua de posts pendents per a l'extensió de Chrome.

Substitueix el webhook de make.com: en lloc d'enviar el post a un servei extern,
el desa dins del repo (`queue/`) i manté un índex lleuger (`queue/index.json`) que
l'extensió de Chrome llegeix sense autenticació via raw.githubusercontent.com.

És GENÈRIC: no conté res específic d'aquest pack (anime), així que es pot copiar
tal qual a futurs scrapers (receptes, pla de cap de setmana...). L'únic contracte
és la forma del `payload` que rep `enqueue()`.

Contracte del payload (mínim):
    {
      "generated_at": "2026-06-10T08:53:34",   # ISO; serveix d'id
      "tipus": "text" | "imatge",
      "title": "...",
      "subreddit": "AnimeCatala",
      # text:   "markdown": "...", opcional "comment_markdown": "..."
      # imatge: "url": "https://..."
    }
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import config

log = logging.getLogger(__name__)

INDEX_VERSION = 1
RETENTION_DAYS = 30  # ha de coincidir amb RETENTION_DAYS de l'extensió


def _queue_dir() -> Path:
    d = Path(getattr(config, "QUEUE_DIR", None) or (config.BASE_DIR / "queue"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def _slug(text: str) -> str:
    """Converteix un id en un nom de fitxer segur (':' i '.' → '-')."""
    return "".join(c if (c.isalnum() or c in "-_") else "-" for c in text)


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _parse(dt_str: str) -> datetime:
    dt = datetime.fromisoformat(dt_str)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _enqueue_worker(payload: dict) -> str:
    """Publica el payload a la cua privada del Cloudflare Worker (POST /enqueue).

    Mapeja el contracte intern (amb 'generated_at') al del Worker (amb
    'created_at') i hi afegeix 'source'/'source_label' perquè l'extensió agrupi
    per pack. Retorna l'id que assigna el Worker.
    """
    import urllib.request
    import urllib.error

    body = {
        "tipus": payload.get("tipus", "text"),
        "title": payload.get("title", ""),
        "subreddit": payload.get("subreddit", ""),
        "source": getattr(config, "QUEUE_SOURCE", "anime"),
        "source_label": getattr(config, "QUEUE_SOURCE_LABEL", "anime"),
        "created_at": payload.get("generated_at") or _now_iso(),
    }
    if payload.get("markdown"):
        body["markdown"] = payload["markdown"]
    if payload.get("url"):
        body["url"] = payload["url"]
    if payload.get("comment_markdown"):
        body["comment_markdown"] = payload["comment_markdown"]

    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        config.WORKER_URL + "/enqueue",
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + config.WORKER_WRITE_TOKEN,
            # Sense un User-Agent realista, Cloudflare veta la signatura per
            # defecte d'urllib ('Python-urllib/x.y') amb un 403 «error code: 1010».
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            out = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        raise RuntimeError(f"Worker /enqueue HTTP {e.code}: {detail}") from e

    item_id = out.get("id", "")
    log.info("Encuat al Worker %s (r/%s) com a font '%s'.",
             item_id, body["subreddit"], body["source"])
    return item_id


def enqueue(payload: dict) -> str:
    """Desa un payload a la cua i retorna l'id de l'item.

    Si el Worker està configurat (WORKER_URL + WORKER_WRITE_TOKEN), publica-hi i
    prou — res no toca queue/ ni GitHub. Si no, escriu fitxers a queue/ (legacy).
    """
    if getattr(config, "WORKER_URL", "") and getattr(config, "WORKER_WRITE_TOKEN", ""):
        return _enqueue_worker(payload)

    qdir = _queue_dir()

    tipus = payload.get("tipus", "text")
    created = payload.get("generated_at") or _now_iso()
    item_id = f"{tipus}-{created}"
    fname = f"{_slug(item_id)}.json"

    (qdir / fname).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # Carrega l'índex existent (o en crea un de nou si està absent/malmès).
    index_path = qdir / "index.json"
    items: list[dict] = []
    if index_path.exists():
        try:
            items = json.loads(index_path.read_text(encoding="utf-8")).get("items", [])
        except (json.JSONDecodeError, OSError):
            log.warning("queue/index.json malmès; es recrea de zero.")

    # Substitueix qualsevol entrada amb el mateix id i afegeix la nova.
    items = [it for it in items if it.get("id") != item_id]
    items.append({
        "id": item_id,
        "tipus": tipus,
        "title": payload.get("title", ""),
        "subreddit": payload.get("subreddit", ""),
        "created_at": created,
        "file": f"queue/{fname}",
        "has_comment": bool(payload.get("comment_markdown")),
    })

    # Retenció: descarta items més antics que RETENTION_DAYS.
    cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
    kept = []
    for it in items:
        try:
            if _parse(it.get("created_at", "")) >= cutoff:
                kept.append(it)
        except (ValueError, TypeError):
            kept.append(it)  # data il·legible → la conservem per seguretat
    kept.sort(key=lambda it: it.get("created_at", ""))

    index_path.write_text(
        json.dumps(
            {"version": INDEX_VERSION, "updated_at": _now_iso(), "items": kept},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    # Neteja payloads orfes (fitxers sense entrada vigent a l'índex).
    valid = {it["file"].rsplit("/", 1)[-1] for it in kept}
    for f in qdir.glob("*.json"):
        if f.name != "index.json" and f.name not in valid:
            try:
                f.unlink()
            except OSError:
                pass

    log.info("Encuat %s (r/%s) → %s", item_id, payload.get("subreddit", "?"), fname)
    return item_id


def rebuild_index() -> int:
    """Reconstrueix queue/index.json a partir de TOTS els payloads presents a
    queue/. Pensat per resoldre curses entre workflows concurrents: cada item és
    un fitxer de nom únic (mai col·lisiona), així que l'índex sempre es pot
    derivar de la unió de fitxers sense un merge manual. Retorna el nombre d'items.
    """
    qdir = _queue_dir()
    items: list[dict] = []
    for f in sorted(qdir.glob("*.json")):
        if f.name == "index.json":
            continue
        try:
            payload = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        tipus = payload.get("tipus", "text")
        created = payload.get("generated_at") or _now_iso()
        items.append({
            "id": f"{tipus}-{created}",
            "tipus": tipus,
            "title": payload.get("title", ""),
            "subreddit": payload.get("subreddit", ""),
            "created_at": created,
            "file": f"queue/{f.name}",
            "has_comment": bool(payload.get("comment_markdown")),
        })

    cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
    kept = []
    for it in items:
        try:
            if _parse(it["created_at"]) >= cutoff:
                kept.append(it)
        except (ValueError, TypeError):
            kept.append(it)
    kept.sort(key=lambda it: it.get("created_at", ""))

    (qdir / "index.json").write_text(
        json.dumps(
            {"version": INDEX_VERSION, "updated_at": _now_iso(), "items": kept},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return len(kept)
