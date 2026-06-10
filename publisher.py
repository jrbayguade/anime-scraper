"""
publisher.py — Publica el post a Reddit amb PRAW (API oficial).

A diferència del mòdul de Reddit de make.com, PRAW envia el text al COS de la
petició, així que no hi ha límit pràctic de llargada (selftext admet ~40.000
caràcters). Publica el post de text i, com a primer comentari, la galeria
d'imatges (`comment_markdown`).
"""

from __future__ import annotations

import logging

import config

log = logging.getLogger("anime-scraper.publisher")


def get_reddit():
    """Crea el client de PRAW amb les credencials del .env."""
    import praw  # import diferit: només cal si es publica

    return praw.Reddit(
        client_id=config.REDDIT_CLIENT_ID,
        client_secret=config.REDDIT_CLIENT_SECRET,
        username=config.REDDIT_USERNAME,
        password=config.REDDIT_PASSWORD,
        user_agent=config.REDDIT_USER_AGENT,
    )


def check_auth() -> str:
    """Comprova que les credencials funcionen. Retorna el nom d'usuari o llança error."""
    reddit = get_reddit()
    me = reddit.user.me()
    if me is None:
        raise RuntimeError("Autenticació a Reddit fallida (revisa usuari/contrasenya).")
    return str(me)


def _summary(structured: dict) -> str:
    return (
        f"\n  Subreddit : r/{structured['subreddit']}"
        f"\n  Títol     : {structured['title']}"
        f"\n  Notícies  : {structured['item_count']}"
        f"\n  Cos       : {len(structured['markdown'])} caràcters"
        f"\n  Comentari : {len(structured.get('comment_markdown', ''))} caràcters\n"
    )


def publish(structured: dict, *, skip_confirm: bool = False,
            post_comment: bool = True):
    """Publica el post (i el comentari amb les imatges) a Reddit.

    Retorna l'objecte submission, o None si es cancel·la.
    """
    if not config.CAN_PUBLISH:
        raise RuntimeError(
            "Falten credencials de Reddit al .env "
            "(REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET, REDDIT_USERNAME, REDDIT_PASSWORD)."
        )

    # Validació prèvia de credencials amb un missatge clar si fallen
    try:
        user = check_auth()
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"No s'ha pogut connectar a Reddit: {exc}") from exc
    log.info("Autenticat a Reddit com a u/%s", user)

    # Pas d'aprovació (a no ser que es passi --yes)
    if not skip_confirm:
        print(_summary(structured))
        answer = input(f"Publicar a r/{structured['subreddit']}? [s/N] ").strip().lower()
        if answer not in ("s", "si", "sí", "y", "yes"):
            print("❌ Publicació cancel·lada.")
            return None

    reddit = get_reddit()
    subreddit = reddit.subreddit(structured["subreddit"])
    submission = subreddit.submit(
        title=structured["title"],
        selftext=structured["markdown"],
    )
    permalink = f"https://www.reddit.com{submission.permalink}"
    log.info("✅ Post publicat: %s", permalink)

    comment = structured.get("comment_markdown", "")
    if post_comment and comment:
        try:
            submission.reply(body=comment)
            log.info("💬 Comentari amb la galeria d'imatges publicat.")
        except Exception as exc:  # noqa: BLE001 — el post ja està fet, el comentari és secundari
            log.warning("El post s'ha publicat però el comentari ha fallat: %s", exc)

    return submission
