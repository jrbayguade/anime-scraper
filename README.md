# 📺 Anime Scraper — Recull setmanal d'anime i manga en català

Scraper que recull les **novetats d'anime i manga de la darrera setmana** de 3
fonts, en genera un resum en català (amb DeepSeek) i munta un **post per a
r/AnimeCatala** amb bon format. Pensat per orquestrar-se amb **make.com** (post
setmanal automàtic amb revisió abans de publicar).

## ✨ Què fa

1. **Scrapeja** 3 fonts (últims 7 dies):
   - [El Racó del Manga](https://elracodelmanga.cat/noticies/) — català (HTML)
   - [Fansubs.cat](https://noticies.fansubs.cat/) — català (HTML)
   - [Anime Corner](https://animecorner.me/category/news/) — anglès (RSS, s'enriqueix amb imatge/resum de l'article)
2. **Extreu** de cada notícia: títol, data, enllaç, categoria, resum i imatge (URL).
3. **Resumeix i tradueix** al català amb **DeepSeek** (l'anglès d'Anime Corner es tradueix).
4. **Genera** un post Markdown atractiu (`output/posts/AAAA-MM-DD-anime-catala.md`)
   i un **`latest.json`** estructurat per a make.com.
5. **Recorda** què ja s'ha publicat (`output/history.json`) per no repetir notícies.
6. (Opcional) **Envia** el JSON a un webhook de make.com perquè el revisis i el publiquis a Reddit.

## 🧩 Requisits

- **Python 3.10+** (provat amb 3.12)
- WSL Ubuntu, Linux o Mac (a Windows natiu, fes servir `run.bat`)

## 🚀 Instal·lació i ús (WSL Ubuntu)

```bash
cd ~/code/anime-scraper

# Opció A — Script tot-en-un (crea .venv, instal·la i executa)
chmod +x run.sh
./run.sh

# Opció B — Manual
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python main.py
```

El post setmanal apareixerà a `output/posts/`. **Funciona sense cap clau d'API**
(faria servir els extractes originals; l'anglès quedaria marcat amb `[EN]`).

### Opcions de `main.py`

| Opció | Què fa |
|-------|--------|
| _(cap)_ | Recull normal: omet notícies ja publicades en setmanes anteriors. |
| `--ignore-history` | Inclou-hi també notícies ja publicades (útil per a la 1a prova). |
| `--no-llm` | No fa servir DeepSeek (resums en brut). |
| `--quiet` | Menys missatges a la consola. |

## 🔑 Activar DeepSeek (resum + traducció al català)

1. Crea una clau a <https://platform.deepseek.com>.
2. Copia la plantilla i posa-hi la clau:
   ```bash
   cp .env.example .env
   nano .env          # omple DEEPSEEK_API_KEY=...
   ```
3. Torna a executar `python main.py`. Ara els resums seran en català natural i
   les notícies d'Anime Corner es traduiran automàticament.

## 🟠 Publicar a Reddit (PRAW)

La publicació es fa **directament des de Python amb PRAW** (l'API oficial de
Reddit). Es va descartar publicar amb el mòdul de Reddit de make.com perquè
**envia el text dins de la URL i peta amb `414 URI Too Long`** quan el post és
llarg (un recull setmanal ho és). PRAW envia el text al cos de la petició, sense
límit pràctic.

**1. Crea una "script app"** a <https://www.reddit.com/prefs/apps> → *create app*
→ tipus **script** → redirect uri `http://localhost:8080`. Apunta el **client ID**
(sota el nom de l'app) i el **secret**.

**2. Posa les credencials al `.env`:**
```
REDDIT_CLIENT_ID=...
REDDIT_CLIENT_SECRET=...
REDDIT_USERNAME=elteuusuari
REDDIT_PASSWORD=lateuacontrasenya
```

**3. Flux amb aprovació** (recomanat):
```bash
python main.py        # 1) genera l'esborrany a output/posts/*.md
                      # 2) revisa el .md
python publish.py     # 3) mostra un resum, demana confirmació i publica
```
- `python publish.py --yes` publica sense preguntar (per a automatització).
- `python main.py --publish` genera i publica de cop (també demana confirmació).

Publica un **post de text** amb el recull i, com a **primer comentari**, la
galeria d'imatges (`comment_markdown`).

### Alternativa sense API: publicació manual assistida

Si Reddit no et deixa crear la "script app" (els comptes nous o sense email
verificat sovint es bloquegen), tens `publish_manual.py` (pensat per a WSL):

```bash
python main.py             # genera l'esborrany
python publish_manual.py   # copia el cos al porta-retalls i obre Reddit al navegador
```
Després només has d'enganxar (**Ctrl+V**) al quadre de text, posar el títol i
clicar **Post**. Per a la galeria d'imatges al primer comentari:
```bash
python publish_manual.py --comment   # copia la galeria; enganxa-la com a 1r comentari
```
L'editor web de Reddit accepta posts llargs sense problema (el límit del `414`
era només del mòdul de make.com, no de Reddit).

## 🔗 Integració amb make.com (opcional: avís d'esborrany)

make.com ja **no cal per publicar** (ho fa PRAW), però és útil si vols rebre
l'esborrany a Telegram/email abans d'aprovar-lo. Si defineixes `MAKE_WEBHOOK_URL`,
en acabar `main.py` s'hi envia el JSON del post:

```
  Python (scraping + DeepSeek + PRAW)      make.com (avís opcional)
  ──────────────────────────────────      ────────────────────────
  scrapeja · resumeix · munta el post  ──POST──▶ rep el JSON (Custom webhook)
  publica a Reddit amb publish.py               t'envia l'esborrany a Telegram
```

**Per configurar-ho:**

1. A make.com, crea un escenari amb un mòdul **Webhooks → Custom webhook** i
   copia la URL que et dona.
2. Posa-la al `.env`: `MAKE_WEBHOOK_URL=https://hook.eu2.make.com/xxxxx`
3. Quan executis `python main.py`, en acabar s'enviarà aquest JSON al webhook:
   ```json
   {
     "title": "📺 Novetats d'anime...",
     "markdown": "<el post sencer en Markdown>",
     "lead_image_url": "https://...jpg",
     "item_count": 15,
     "items": [ { "title": "...", "url": "...", "image_url": "...", ... } ],
     "comment_markdown": "<galeria d'imatges per al primer comentari>",
     "subreddit": "AnimeCatala"
   }
   ```
4. A make.com, afegeix un mòdul de **missatgeria** (Telegram/Gmail/Discord) que
   t'enviï `title` + `markdown` per revisar l'esborrany al mòbil. La publicació
   real la fas amb `python publish.py` (PRAW).

> 💡 **Resum/traducció a make.com en comptes de Python?** Si ho prefereixes,
> deixa el `.env` sense `DEEPSEEK_API_KEY` i afegeix un mòdul HTTP/IA a make.com
> que cridi DeepSeek amb el camp `markdown` o amb cada `items[].summary`. Aquí
> es fa a Python perquè és més fàcil de provar i iterar localment.

### 🖼️ Nota sobre les imatges a Reddit

Reddit **no incrusta imatges externes** ni en un post de text ni en un comentari
(sempre es veuen com a enllaç, mai com a foto). Per això el post és **text-first**:
cada notícia enllaça a la seva font, on el lector ja veu les imatges. El JSON
segueix exposant `lead_image_url` i `items[].image_url` per si en el futur es
fan servir en altres canals (Discord/Telegram **sí** que renderitzen les URLs).

## ⏰ Automatitzar-ho cada setmana (GitHub Actions)

El cron viu a `.github/workflows/weekly-roundup.yml` i s'executa **cada dilluns a
les 08:00 UTC** (al núvol, sense el teu PC engegat). El flux complet:

```
Dilluns 08:00 UTC → GitHub Actions executa main.py
   → scrapeja + DeepSeek (resums en català) → POST al webhook de make
        → make publica el post a r/AnimeCatala
```

**Configuració (un sol cop):**
1. **Secrets** del repo (Settings → Secrets and variables → Actions):
   - `DEEPSEEK_API_KEY` i `MAKE_WEBHOOK_URL`. (El `.env` NO es puja.)
2. A **make**, deixa l'escenari **actiu** (toggle ON), no en mode "Run once".
3. Puja-ho tot: `git add -A && git commit -m "cron setmanal" && git push`.
4. Prova'l a mà des de la pestanya **Actions → Run workflow**.

L'històric (`output/history.json`) es desa automàticament al repo cada setmana
perquè no es repeteixin notícies. Per canviar el dia/hora, edita la línia `cron:`
del workflow (format: <https://crontab.guru>).

> **Alternativa (cron a WSL):** `crontab -e` i afegir
> `0 9 * * 1 /home/jbosch/code/anime-scraper/run.sh` — però depèn que el teu PC
> estigui engegat i que WSL tingui cron actiu (`sudo service cron start`).

## ➕ Afegir una font nova

1. Afegeix un diccionari a `SOURCES` dins de `config.py`:
   ```python
   {"name": "Nova Font", "key": "novafont", "type": "html",
    "url": "https://...", "emoji": "🆕", "lang": "ca", "enabled": True},
   ```
2. Escriu `parse_novafont(html, source, session)` a `scraper.py` que retorni una
   llista de `NewsItem` (mira els parsers existents com a exemple).
3. Registra-la a `PARSERS = { ..., "novafont": parse_novafont }`.

No cal tocar res més: el filtre de dates, la dedup, el resum i el post ja
funcionen per a totes les fonts.

## 🔞 Filtre de contingut

Fansubs.cat agrega **totes** les publicacions dels fansubs, incloent contingut
+18. A `config.py`, la llista `SKIP_KEYWORDS` omet qualsevol notícia que contingui
aquestes paraules. Edita-la segons el criteri de la comunitat.

## 📁 Estructura

```
anime-scraper/
├── main.py            # Orquestra tot el procés
├── scraper.py         # Descàrrega i parsing de les fonts
├── processor.py       # Resum (DeepSeek), post Markdown, JSON, històric
├── config.py          # Configuració (fonts, delays, claus, filtres)
├── requirements.txt
├── run.sh / run.bat   # Executors per a Linux-Mac / Windows
├── .env.example       # Plantilla de variables d'entorn
├── output/
│   ├── posts/         # Posts .md + latest.json
│   └── history.json   # Notícies ja publicades
└── logs/              # Logs per dia
```
