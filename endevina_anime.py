"""
endevina_anime.py — Joc «Endevina-ho, otaku!» generat amb IA (DeepSeek).

Quarta sortida (autònoma) del pack d'anime, pensada per a un cron dos cops per
setmana (omple els buits dels altres: el recull és dilluns i la graella SX3
divendres, així que aquí va bé **dimecres i dissabte**).

Demana a DeepSeek una endevinalla d'anime/manga d'una **categoria rotativa**
(personatge, sèrie, autor/a, estudi, opening, cita, època, trivia...) perquè els
temes siguin ben variats, i en munta un post de text per a r/AnimeCatala: la
pista visible i la solució amagada amb el spoiler de Reddit (`>!resposta!<`).

Reaprofita la infraestructura del projecte: el client DeepSeek de `processor`,
`config.SUBREDDIT` i la cua de `queue_store` (el mateix Worker que la resta de
sortides). Com que el contingut del joc ÉS la resposta de l'IA, no hi ha
fallback estàtic: si DeepSeek no respon (o falta `DEEPSEEK_API_KEY`), amb `--push`
el procés acaba amb codi d'error perquè es vegi al workflow i ho puguis arreglar.

Ús:
    python endevina_anime.py --post     # preview (no encua res)
    python endevina_anime.py --push     # genera i encua a la cua del Worker
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime

import config

log = logging.getLogger("anime-scraper.endevina")

# Cua per a l'extensió (mateix patró que sx3_schedule / bluesky_manga).
try:
    import queue_store
    _HAS_QUEUE = True
except Exception:  # pragma: no cover
    _HAS_QUEUE = False


# To de marca per al contingut generat (català directe, Reddit, sense farciment).
SYSTEM_PROMPT = (
    "Ets el game master otaku de r/AnimeCatala. Crees un joc per endevinar coses "
    "d'anime i manga. Escrius sempre en català natural, directe i col·loquial, "
    "adequat per a usuaris de Reddit. Evites estructures artificials, paraules "
    "excessivament formals i introduccions de farciment tipus «és un plaer "
    "ajudar-te». Vas al gra, fas servir Markdown amb gràcia (negretes, llistes) i "
    "tens un punt fan, gamberro i apassionat. Coneixes des dels clàssics fins a "
    "joies poc conegudes, no només els quatre mainstream de sempre."
)

# Categories rotatives: garanteixen que els temes vagin variant a cada partida.
# (label visible, instrucció per a l'LLM)
_CATEGORIES: list[tuple[str, str]] = [
    ("Personatge",
     "un personatge d'anime o manga: descriu-lo amb pistes (aspecte, poders, "
     "rol, manies, alguna frase) sense dir-ne mai el nom"),
    ("Sèrie",
     "una sèrie d'anime: descriu la trama, l'ambientació o el gènere amb pistes "
     "sense dir-ne mai el títol"),
    ("Autor/a",
     "un/a mangaka o autor/a: dona una curiositat o pista sobre qui és o què va "
     "crear, sense dir-ne el nom"),
    ("Estudi",
     "un estudi d'animació: pistes pel seu estil o les seves obres, sense "
     "dir-ne el nom"),
    ("Opening/Ending",
     "un opening o ending famós: descriu la cançó, l'artista o l'escena sense "
     "dir-ne el nom ni la sèrie"),
    ("Cita mítica",
     "una frase o cita mítica: pistes de qui la diu i/o de quina obra és, sense "
     "dir la resposta"),
    ("Època",
     "l'any o època d'estrena d'una obra coneguda: pistes de context (què passava, "
     "amb què competia) sense dir el títol ni l'any de cop"),
    ("Trivia",
     "una curiositat o dada sorprenent del món de l'anime/manga, en pla pregunta "
     "de trivia o «veritat o mentida»"),
]


def _categoria_del_dia(avui: date) -> tuple[str, str]:
    """Rota de categoria segons el dia, perquè dimecres i dissabte no coincideixin."""
    return _CATEGORIES[avui.toordinal() % len(_CATEGORIES)]


def _user_prompt(label: str, instruccio: str, avui: date) -> str:
    return (
        f"Avui és {avui.strftime('%d/%m/%Y')}. Crea UNA endevinalla de la "
        f"categoria «{label}»: {instruccio}. Barreja obres mainstream i joies "
        "menys conegudes; sigues imprevisible i evita sempre el més obvi. La "
        "pista ha de ser divertida i jugable als comentaris, amb una mica de "
        "Markdown (negretes, potser una llista curta). No revelis mai la "
        "resposta dins la pista ni hi posis paraules massa òbvies. Respon NOMÉS "
        'amb un objecte JSON amb aquestes claus exactes: '
        '{"pista": "<la pista en markdown>", "resposta": "<la solució>"}.'
    )


def generar_joc(avui: date, use_llm: bool = True) -> tuple[str, str, str] | None:
    """(categoria, pista, resposta) via DeepSeek; None si l'LLM no respon bé."""
    label, instruccio = _categoria_del_dia(avui)
    if not use_llm:
        return None
    try:
        from processor import _deepseek_chat, _extract_json  # client del projecte
    except Exception as exc:  # pragma: no cover
        log.warning("No es pot importar el client DeepSeek: %s", exc)
        return None

    raw = _deepseek_chat(
        [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": _user_prompt(label, instruccio, avui)},
        ],
        temperature=1.15,
        max_tokens=700,
    )
    if not raw:
        return None
    dades = _extract_json(raw)
    if not isinstance(dades, dict):
        log.warning("DeepSeek no ha tornat JSON vàlid: %r", raw[:200])
        return None
    pista = (dades.get("pista") or "").strip()
    resposta = (dades.get("resposta") or "").strip()
    if not pista or not resposta:
        log.warning("Resposta de DeepSeek incompleta: %r", dades)
        return None
    return label, pista, resposta


def build_post(avui: date, use_llm: bool = True) -> dict | None:
    """Munta el payload del post (o None si no s'ha pogut generar el joc)."""
    joc = generar_joc(avui, use_llm=use_llm)
    if not joc:
        return None
    label, pista, resposta = joc
    title = f"🎌 [JOC] Repte otaku · {avui.strftime('%d/%m/%Y')}"
    markdown = (
        f"**[{label}]**\n\n"
        f"{pista}\n\n"
        "---\n"
        "👇 Ho saps? Etziba la teva resposta als comentaris.\n\n"
        f"Solució (sense trampes): >!{resposta}!<"
    )
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "tipus": "text",
        "title": title,
        "subreddit": config.SUBREDDIT,
        "markdown": markdown,
    }


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Joc «Endevina-ho, otaku!» amb DeepSeek.")
    p.add_argument("--post", action="store_true",
                   help="Imprimeix el post (no encua res).")
    p.add_argument("--push", action="store_true",
                   help="Genera i encua el joc a la cua del Worker.")
    p.add_argument("--no-llm", action="store_true",
                   help="Desactiva DeepSeek (només per a proves; no genera res).")
    p.add_argument("--quiet", action="store_true", help="Menys missatges.")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(message)s",
    )
    use_llm = config.USE_LLM and not args.no_llm

    post = build_post(date.today(), use_llm=use_llm)
    if not post:
        print(
            "❌ No s'ha pogut generar el joc: DeepSeek no ha respost o falta "
            "DEEPSEEK_API_KEY. Revisa el secret.",
            file=sys.stderr,
        )
        return 1

    if args.push:
        if not _HAS_QUEUE:
            print("❌ queue_store no disponible: no s'ha encuat.", file=sys.stderr)
            return 1
        item_id = queue_store.enqueue(post)
        print(f"✅ Encuat: {post['title']} → {item_id}")
    else:
        print(post["title"])
        print("-" * 70)
        print(post["markdown"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
