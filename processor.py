"""
processor.py — Converteix les notícies en un post de Reddit en català.

Passos:
  1. summarize_items(): genera un resum curt en català de cada notícia amb
     DeepSeek (tradueix l'anglès d'Anime Corner). Si no hi ha clau d'API,
     fa servir l'extracte original.
  2. build_post(): munta el Markdown atractiu (títol, seccions, llistes, emojis).
  3. save_outputs(): desa el .md a output/posts/ i un latest.json estructurat
     (perquè make.com el reciclui cap a Reddit).
  4. push_to_make(): opcionalment envia el JSON al webhook de make.com.
  5. update_history(): recorda les notícies publicades per no repetir-les.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

import requests

import config
from scraper import NewsItem

log = logging.getLogger("anime-scraper.processor")

_MONTHS_CA = [
    "gener", "febrer", "març", "abril", "maig", "juny",
    "juliol", "agost", "setembre", "octubre", "novembre", "desembre",
]


# --------------------------------------------------------------------------- #
# Format de dates en català                                                    #
# --------------------------------------------------------------------------- #
def date_ca(d: datetime) -> str:
    return f"{d.day} de {_MONTHS_CA[d.month - 1]}"


def date_ca_year(d: datetime) -> str:
    return f"{d.day} de {_MONTHS_CA[d.month - 1]} de {d.year}"


# --------------------------------------------------------------------------- #
# DeepSeek (resum + traducció)                                                 #
# --------------------------------------------------------------------------- #
def _deepseek_chat(messages: list[dict], *, temperature: float = 0.4,
                   max_tokens: int = 2000) -> str | None:
    """Crida l'API de DeepSeek (compatible amb OpenAI). Retorna el text o None."""
    if not config.USE_LLM:
        return None
    try:
        resp = requests.post(
            f"{config.DEEPSEEK_BASE_URL}/chat/completions",
            headers={
                "Authorization": f"Bearer {config.DEEPSEEK_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": config.DEEPSEEK_MODEL,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "stream": False,
            },
            timeout=60,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"].strip()
    except Exception as exc:  # noqa: BLE001
        log.warning("Crida a DeepSeek fallida: %s", exc)
        return None


def _extract_json(text: str):
    """Extreu el primer bloc JSON (objecte o array) d'un text del LLM."""
    start = min((i for i in (text.find("["), text.find("{")) if i != -1), default=-1)
    if start == -1:
        return None
    end = max(text.rfind("]"), text.rfind("}"))
    if end <= start:
        return None
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None


def summarize_items(items: list[NewsItem]) -> None:
    """Omple item.summary_ca per a cada notícia (in-place)."""
    if not items:
        return

    if not config.USE_LLM:
        log.info("Sense clau DeepSeek: es fan servir els extractes originals.")
        for it in items:
            base = it.summary or it.title
            it.summary_ca = base if it.lang == "ca" else f"[EN] {base}"
        return

    log.info("Generant resums en català amb DeepSeek (%d notícies)...", len(items))
    payload = [
        {
            "id": i,
            "lang": it.lang,
            "title": it.title,
            "text": it.summary or "",
        }
        for i, it in enumerate(items)
    ]
    system = (
        "Ets un redactor d'una comunitat catalana d'anime i manga. Escrius en "
        "català natural, clar i proper. Tradueixes al català qualsevol text en "
        "anglès."
    )
    user = (
        "Per a cada notícia, escriu un resum curt (1-2 frases, màxim "
        f"{config.SUMMARY_MAX_CHARS} caràcters) en català, atractiu i informatiu. "
        "Si el text està en anglès, tradueix-lo. No inventis dades que no "
        "apareguin. Respon NOMÉS amb un array JSON d'objectes "
        '{"id": <int>, "summary": "<resum en català>"}.\n\n'
        f"Notícies:\n{json.dumps(payload, ensure_ascii=False)}"
    )
    raw = _deepseek_chat(
        [{"role": "system", "content": system},
         {"role": "user", "content": user}],
        max_tokens=3000,
    )
    data = _extract_json(raw) if raw else None

    summaries: dict[int, str] = {}
    if isinstance(data, list):
        for obj in data:
            try:
                summaries[int(obj["id"])] = str(obj["summary"]).strip()
            except (KeyError, ValueError, TypeError):
                continue

    # Apliquem resultats amb fallback per notícia
    for i, it in enumerate(items):
        if i in summaries and summaries[i]:
            it.summary_ca = summaries[i]
        else:
            base = it.summary or it.title
            it.summary_ca = base if it.lang == "ca" else f"[EN] {base}"
    if not summaries:
        log.warning("DeepSeek no ha retornat resums vàlids; s'usen els extractes.")


def build_intro(items: list[NewsItem], week_start: datetime, week_end: datetime) -> str:
    """Intro generada per DeepSeek (o estàtica de fallback)."""
    n = len(items)
    fallback = (
        f"Hola, comunitat! 👋 Aquí teniu el recull setmanal de novetats d'anime "
        f"i manga en català: **{n} notícies** de la setmana del "
        f"{date_ca(week_start)} al {date_ca(week_end)}. Bona lectura! 🍿"
    )
    if not config.USE_LLM or n == 0:
        return fallback
    titles = "\n".join(f"- {it.title}" for it in items[:12])
    raw = _deepseek_chat(
        [
            {"role": "system", "content": "Ets el community manager d'una "
             "comunitat catalana d'anime. Escrius en català, to proper i animat."},
            {"role": "user", "content":
                f"Escriu una introducció breu (2-3 frases, amb 1-2 emojis) per a "
                f"un recull setmanal de novetats d'anime i manga en català "
                f"(setmana del {date_ca(week_start)} al {date_ca(week_end)}, "
                f"{n} notícies). No facis una llista, només el paràgraf "
                f"d'introducció. Temes destacats:\n{titles}"},
        ],
        temperature=0.7, max_tokens=300,
    )
    return raw.strip() if raw else fallback


# --------------------------------------------------------------------------- #
# Construcció del post Markdown                                                 #
# --------------------------------------------------------------------------- #
def _category_badge(category: str) -> str:
    if not category:
        return ""
    emoji = config.CATEGORY_EMOJI.get(category.lower(), config.DEFAULT_CATEGORY_EMOJI)
    return f"{emoji} `{category}`"


def _build_comment(items: list[NewsItem], lead_image: str) -> str:
    """Cos del comentari de Reddit amb la galeria d'imatges de la setmana.

    Es publica com a primer comentari del post (Reddit no permet imatges dins
    d'un post de text). La URL destacada va sola en una línia perquè la web nova
    de Reddit en mostri la previsualització.
    """
    if not items:
        return ""
    lines = ["🖼️ **Galeria d'imatges de la setmana**", ""]
    if lead_image:
        lines += [lead_image, ""]
    for it in items:
        if it.image_url:
            lines.append(f"- [{it.title}]({it.image_url})")
    lines += ["", "*Recull automàtic 🤖*"]
    return "\n".join(lines)


def build_post(items: list[NewsItem]) -> dict:
    """Retorna un dict amb title, markdown i dades estructurades."""
    today = datetime.now()
    week_start = today - timedelta(days=config.DAYS_BACK)
    week_end = today

    title = (f"📺 Novetats d'anime i manga en català · Setmana del "
             f"{date_ca(week_start)} al {date_ca(week_end)}")

    lines: list[str] = [f"# {title}", ""]
    lines.append(build_intro(items, week_start, week_end))
    lines.append("")

    if not items:
        lines.append("_Aquesta setmana no s'han trobat novetats a les fonts. "
                     "Torna la setmana que ve! 🙂_")
    else:
        # Agrupem per font, respectant l'ordre de config.SOURCES
        order = {s["key"]: i for i, s in enumerate(config.SOURCES)}
        by_source: dict[str, list[NewsItem]] = {}
        for it in items:
            by_source.setdefault(it.source_key, []).append(it)

        for key in sorted(by_source, key=lambda k: order.get(k, 99)):
            group = by_source[key]
            head = group[0]
            lines.append(f"## {head.source_emoji} {head.source} "
                         f"({len(group)})")
            lines.append("")
            for it in group:
                badge = _category_badge(it.category)
                meta = " · ".join(p for p in (badge, date_ca(it.date)) if p)
                lines.append(f"**[{it.title}]({it.url})**  ")
                if meta:
                    lines.append(f"{meta}  ")
                if it.summary_ca:
                    lines.append(f"{it.summary_ca}  ")
                if it.image_url:
                    lines.append(f"🖼️ [Imatge]({it.image_url})  ")
                lines.append("")
            lines.append("---")
            lines.append("")

    fonts = " · ".join(s["name"] for s in config.SOURCES if s.get("enabled", True))
    lines.append(f"*🤖 Recull generat automàticament. Fonts: {fonts}.*  ")
    lines.append("*Has vist algun error o vols proposar una font nova? "
                 "Comenta-ho! 💬*")

    markdown = "\n".join(lines)
    lead_image = next((it.image_url for it in items if it.image_url), "")
    comment_markdown = _build_comment(items, lead_image)

    structured = {
        "generated_at": today.isoformat(timespec="seconds"),
        "week_start": week_start.strftime("%Y-%m-%d"),
        "week_end": week_end.strftime("%Y-%m-%d"),
        "subreddit": config.SUBREDDIT,
        "title": title,
        "item_count": len(items),
        "lead_image_url": lead_image,
        "markdown": markdown,
        "comment_markdown": comment_markdown,
        "items": [it.to_dict() for it in items],
    }
    return structured


# --------------------------------------------------------------------------- #
# Sortida: fitxers, webhook, històric                                          #
# --------------------------------------------------------------------------- #
def save_outputs(structured: dict) -> tuple:
    config.POSTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d")
    md_path = config.POSTS_DIR / f"{stamp}-anime-catala.md"
    md_path.write_text(structured["markdown"], encoding="utf-8")
    config.LATEST_JSON.write_text(
        json.dumps(structured, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    log.info("Post desat a %s", md_path)
    log.info("JSON estructurat desat a %s", config.LATEST_JSON)
    return md_path, config.LATEST_JSON


def push_to_make(structured: dict) -> bool:
    if not config.MAKE_WEBHOOK_URL:
        return False
    try:
        resp = requests.post(config.MAKE_WEBHOOK_URL, json=structured, timeout=30)
        resp.raise_for_status()
        log.info("JSON enviat al webhook de make.com (HTTP %s).", resp.status_code)
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("No s'ha pogut enviar a make.com: %s", exc)
        return False


def load_history() -> set[str]:
    if not config.HISTORY_FILE.exists():
        return set()
    try:
        return set(json.loads(config.HISTORY_FILE.read_text(encoding="utf-8")))
    except Exception:  # noqa: BLE001
        return set()


def update_history(items: list[NewsItem]) -> None:
    config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    history = load_history()
    history.update(it.dedupe_key() for it in items)
    config.HISTORY_FILE.write_text(
        json.dumps(sorted(history), indent=0), encoding="utf-8"
    )
