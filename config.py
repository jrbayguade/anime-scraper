"""
config.py — Configuració central del projecte.

Tot el que vulguis ajustar (fonts, delays, claus d'API, rutes...) viu aquí.
Per afegir una font nova en el futur només cal afegir un diccionari a SOURCES
i una funció de parsing a scraper.py (veure les instruccions del README).
"""

from __future__ import annotations

import os
from pathlib import Path

# --------------------------------------------------------------------------- #
# Carrega de variables d'entorn (.env)                                         #
# --------------------------------------------------------------------------- #
# Intentem fer servir python-dotenv; si no està instal·lat, fem un loader mínim.
BASE_DIR = Path(__file__).resolve().parent
_ENV_FILE = BASE_DIR / ".env"


def _load_env() -> None:
    try:
        from dotenv import load_dotenv  # type: ignore

        load_dotenv(_ENV_FILE)
        return
    except Exception:
        pass
    # Fallback mínim: parseja KEY=VALUE línia a línia.
    if _ENV_FILE.exists():
        for line in _ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_env()

# --------------------------------------------------------------------------- #
# Rutes                                                                        #
# --------------------------------------------------------------------------- #
OUTPUT_DIR = BASE_DIR / "output"
POSTS_DIR = OUTPUT_DIR / "posts"
LOGS_DIR = BASE_DIR / "logs"
HISTORY_FILE = OUTPUT_DIR / "history.json"
LATEST_JSON = POSTS_DIR / "latest.json"

# --------------------------------------------------------------------------- #
# Comportament de l'scraping                                                   #
# --------------------------------------------------------------------------- #
DAYS_BACK = 7                 # Només notícies dels últims N dies
REQUEST_TIMEOUT = 20          # Segons abans de donar una petició per perduda
REQUEST_RETRIES = 2           # Reintents per petició fallida
DELAY_MIN = 3.0               # Delay mínim entre peticions (segons)
DELAY_MAX = 5.0               # Delay màxim entre peticions (segons)
MAX_ITEMS_PER_SOURCE = 5      # Topall de notícies per font (manté el post raonable)
SUMMARY_MAX_CHARS = 160       # Resum curt: una frase, amb enllaç a l'original per a més info

# User-Agent realista de navegador (Chrome a Linux)
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Capçaleres per a totes les peticions
HTTP_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ca,es;q=0.9,en;q=0.8",
}

# --------------------------------------------------------------------------- #
# Fonts a scrapejar                                                            #
# --------------------------------------------------------------------------- #
# type:  "html"  -> es descarrega la pàgina i es parseja amb BeautifulSoup
#        "rss"   -> es parseja el feed amb feedparser
# key:   identificador que connecta la font amb la seva funció de parsing
#        a scraper.py (PARSERS[key]).
# lang:  idioma original del contingut ("ca" o "en"); "en" es traduirà.
SOURCES = [
    {
        "name": "El Racó del Manga",
        "key": "elracodelmanga",
        "type": "html",
        "url": "https://elracodelmanga.cat/noticies/",
        "emoji": "📚",
        "lang": "ca",
        "enabled": True,
    },
    {
        "name": "Fansubs.cat",
        "key": "fansubs",
        "type": "html",
        "url": "https://noticies.fansubs.cat/",
        "emoji": "🎬",
        "lang": "ca",
        "enabled": True,
    },
    {
        "name": "Anime Corner",
        "key": "animecorner",
        "type": "rss",
        "url": "https://animecorner.me/category/news/feed/",
        "emoji": "🌐",
        "lang": "en",
        "enabled": True,
    },
]

# --------------------------------------------------------------------------- #
# Filtre de contingut (NSFW / paraules a evitar)                              #
# --------------------------------------------------------------------------- #
# Fansubs.cat agrega TOTES les publicacions dels fansubs, incloent contingut
# +18. Si el títol o el resum conté alguna d'aquestes paraules, la notícia
# s'omet. Edita la llista segons el criteri de la comunitat (buida = no filtra).
SKIP_KEYWORDS = [
    "hentai",
    "+18",
    "nsfw",
    "ecchi",
]

# --------------------------------------------------------------------------- #
# Emojis per categoria (per fer el post més visual)                            #
# --------------------------------------------------------------------------- #
CATEGORY_EMOJI = {
    "anime": "📺",
    "manga": "📖",
    "videojocs": "🎮",
    "gaming": "🎮",
    "cultura": "🎏",
    "podcast": "🎙️",
    "news": "📰",
    "fansub": "💬",
    "seasonal previews": "🌸",
    "anime news": "📺",
    "gaming news": "🎮",
}
DEFAULT_CATEGORY_EMOJI = "🔹"

# --------------------------------------------------------------------------- #
# DeepSeek (resum + traducció al català)                                       #
# --------------------------------------------------------------------------- #
# La clau es llegeix de la variable d'entorn DEEPSEEK_API_KEY (o del .env).
# Si NO hi ha clau, el projecte segueix funcionant: fa servir l'extracte
# original de cada web (l'anglès quedarà marcat amb [EN]).
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "").strip()
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
USE_LLM = bool(DEEPSEEK_API_KEY)

# --------------------------------------------------------------------------- #
# Integració amb make.com                                                      #
# --------------------------------------------------------------------------- #
# Si defineixes MAKE_WEBHOOK_URL, en acabar s'envia el JSON estructurat del
# post a aquest webhook perquè make.com faci la revisió/publicació a Reddit.
MAKE_WEBHOOK_URL = os.getenv("MAKE_WEBHOOK_URL", "").strip()

# El mòdul "Submit a Post" de make.com envia el text dins la URL i peta amb
# 414 si supera el límit del servidor (~8 KB un cop codificat). Limitem el cos
# del post a aquest nombre de bytes (codificats) i, si cal, es retallen notícies
# (queden igualment al JSON i al comentari). Si tot i així peta, baixa'l a ~3800.
MAKE_BODY_MAX_ENCODED = 7500

# --------------------------------------------------------------------------- #
# Reddit (publicació directa amb PRAW)                                          #
# --------------------------------------------------------------------------- #
# Credencials d'una "script app" creada a https://www.reddit.com/prefs/apps
# Publicar des de Python evita el límit de llargada del mòdul de make.com.
REDDIT_CLIENT_ID = os.getenv("REDDIT_CLIENT_ID", "").strip()
REDDIT_CLIENT_SECRET = os.getenv("REDDIT_CLIENT_SECRET", "").strip()
REDDIT_USERNAME = os.getenv("REDDIT_USERNAME", "").strip()
REDDIT_PASSWORD = os.getenv("REDDIT_PASSWORD", "").strip()
REDDIT_USER_AGENT = os.getenv(
    "REDDIT_USER_AGENT", f"anime-catala-bot/1.0 by u/{REDDIT_USERNAME or 'unknown'}"
)
# True només si hi ha les 4 credencials necessàries
CAN_PUBLISH = all([
    REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET, REDDIT_USERNAME, REDDIT_PASSWORD,
])

# --------------------------------------------------------------------------- #
# Comunitat / sortida                                                          #
# --------------------------------------------------------------------------- #
SUBREDDIT = "AnimeCatala"   # Sense prefix "r/"
POST_TIMEZONE = "Europe/Madrid"

# --------------------------------------------------------------------------- #
# Peu de promoció (opcional, DESACTIVAT per defecte)                           #
# --------------------------------------------------------------------------- #
# Quan vulguis promocionar un projecte propi al final del post, posa
# FOOTER_PROMO_ENABLED = True i revisa el text/enllaç de sota.
# Recomanació: deixa passar unes setmanes perquè el recull s'estableixi abans
# d'activar-ho, i mantén un to honest i subtil (evita "patrocinat" en producte propi).
FOOTER_PROMO_ENABLED = False
FOOTER_PROMO_TEXT = (
    "✍️ Fet per algú que també construeix "
    "[Mail2Follow](https://www.zinkforge.com/mail2follow/?utm_source=reddit"
    "&utm_medium=post&utm_campaign=animecatala&utm_content=recull-setmanal"
    "&utm_term=anime) — si vius enganxat al Gmail, "
    "fes-hi una ullada 👀"
)
