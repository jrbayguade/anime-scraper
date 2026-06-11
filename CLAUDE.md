# CLAUDE.md

Guia per treballar en aquest repositori. Documenta els **fonaments no obvis**:
sobretot, com es publica a Reddit (que NO és per l'API). Llegeix-ho abans de
tocar res relacionat amb la publicació.

## Què és

Bot que prepara contingut d'anime en català i el publica a **r/AnimeCatala**.
Té **tres sortides independents**, cadascuna amb el seu cron de GitHub Actions:

1. **Recull setmanal** (dilluns) — `main.py`. Fa webscraping de notícies d'anime
   (El Racó del Manga, Fansubs.cat, Anime Corner), les resumeix/tradueix amb
   DeepSeek i en munta un post.
2. **Graella de SX3** (divendres) — `sx3_schedule.py`. Agafa la graella del canal
   SX3 de 3Cat per al cap de setmana (divendres→dijous) i en munta un post amb
   els animes, una intro i una pregunta per generar comentaris.
3. **Novetats de manga** (dilluns) — `bluesky_manga.py`. Un cop al mes, agafa el
   post de "Llançaments de manga en català" del compte de Bluesky @samfainavisual
   i el publica com a **post d'imatge** (títol + URL). Detecció determinista; amb
   una **xarxa de seguretat DeepSeek acotada** si el filtre per frase falla.

Totes tres acaben enviant un JSON al **mateix webhook de make.com**, que és qui
publica a Reddit.

## ⚠️ Com es publica a Reddit (FONAMENTAL)

**No publiquem per l'API de Reddit.** No hi ha cap "script app" aprovada ni API
key. La publicació la fa **make.com** amb la seva **connexió nativa de Reddit**
(un compte de Reddit autoritzat dins de make).

Implicacions importants:

- **PRAW NO funciona.** Existeix `publisher.py` (publicació directa amb PRAW) i
  les variables `REDDIT_*` a `.env.example`, però són **codi heretat sense ús**:
  no hi ha credencials vàlides ni app aprovada. No proposis publicar per PRAW.
- **make limita a 2 escenaris actius**, i ja estan tots dos ocupats (l'scraper
  d'anime i un altre de newsletters). **No es pot crear un tercer escenari.**
- **Un sol webhook per a tot el canal.** L'escenari de make que rep el webhook
  només té **un pas després del webhook: publicar a r/AnimeCatala**. Per tant
  **qualsevol post per a aquell canal pot reutilitzar el mateix webhook**
  (`MAKE_WEBHOOK_URL`), sense escenaris nous, sense routers i sense marcadors de
  tipus. El recull setmanal, la graella de SX3 i les novetats de manga hi envien.

### Contracte del webhook

Envia un JSON a `MAKE_WEBHOOK_URL` amb (com a mínim) aquestes claus, que és el
que l'escenari mapeja:

```json
{ "subreddit": "AnimeCatala", "tipus": "text", "title": "…", "markdown": "… (cos del post) …" }
```

El camp **`tipus`** governa un **router** dins de make (el mòdul de Reddit no
accepta posts d'imatge amb URL+text barrejats, així que cal encaminar):
- `"text"` → post de text (recull setmanal i graella de SX3): mapeja `markdown`.
- `"imatge"` → post d'imatge (novetats de manga): mapeja `url` (sense `markdown`,
  només `title` + `url`).

`markdown` és el cos complet del post (ja muntat). Hi pot haver més claus; make
ignora les que no mapeja.

### Límit de llargada

El mòdul de make envia el text **dins de la URL** i peta amb **HTTP 414** si
supera ~7500 bytes un cop codificat (`config.MAKE_BODY_MAX_ENCODED`). Mantingues
els posts compactes. La graella de SX3 ho té present: agrupa per sèrie amb blocs
horaris (no una línia per episodi) i queda molt per sota del límit (~3 KB).

## Fitxers clau

| Fitxer | Rol |
|---|---|
| `config.py` | Configuració central: fonts, claus d'API, `MAKE_WEBHOOK_URL`, subreddit, límits. Tot des de `.env`. |
| `scraper.py` | Recull setmanal: descàrrega i parsing de fonts (un parser per font, registrat a `PARSERS`). |
| `processor.py` | Recull setmanal: resum/traducció amb DeepSeek, munta el Markdown, envia a make, històric. |
| `publisher.py` | Publicació directa amb PRAW. **Heretat / sense ús** (vegeu secció de Reddit). |
| `main.py` | Punt d'entrada del recull setmanal. |
| `sx3_schedule.py` | Graella d'anime de SX3 (autònom): API de 3Cat → post Markdown + DeepSeek → `--push` a make. |
| `bluesky_manga.py` | Novetats mensuals de manga (autònom): feed de Bluesky → selecció determinista (+xarxa DeepSeek acotada) → `--push` a make com a post d'imatge. |

## Font de dades de SX3

La pàgina `3cat.cat/tv3/programacio/canal-sx3/` (Next.js) pinta la graella amb
aquesta API JSON pública:

```
https://api.3cat.cat/graellatvfutur?_format=json&canal=CAD_SX3
    &data_emissio=AVUI&pagina=1&sdom=img&version=2.0&cache=90&master=yes
```

- `data_emissio` accepta offsets RELATIUS de jornada: `AVUI`, `AVUI+1` … `AVUI+9`.
  El divendres, `AVUI` ja és divendres, així que el cron no necessita calcular dates.
- Cada jornada va de ~06:00 d'un dia a ~06:00 de l'endemà (dues dates de calendari).
- Camps per ítem: `titol`, `capitols[].desc` (episodi), `entradeta`,
  `data_emissio` (`DD/MM/YYYY HH:MM:SS`), `programes[].nom_bonic` (slug de l'enllaç).
- No cal navegador: és `requests` pur. (Detalls i exemples al docstring de `sx3_schedule.py`.)

El filtre d'anime és per paraules clau (`ANIME_KEYWORDS` a `sx3_schedule.py`),
calibrable. Avui detecta sobretot Bola de drac, Viatges Pokémon i El detectiu Conan.

## Font de dades de novetats de manga (Bluesky)

API pública de Bluesky, sense autenticació (`requests` pur):

```
https://public.api.bsky.app/xrpc/app.bsky.feed.getAuthorFeed
    ?actor=samfainavisual.bsky.social&limit=50&filter=posts_no_replies
```

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

## DeepSeek (opcional)

S'usa per als resums/traduccions del recull setmanal i per a la intro + pregunta
de la graella de SX3 (API compatible amb OpenAI; client a `processor._deepseek_chat`).
Si no hi ha `DEEPSEEK_API_KEY`, tot funciona igual amb un fallback estàtic.

- La pregunta final de SX3 rota de tema per número de setmana (`_QUESTION_ANGLES`),
  així cada setmana toca un angle diferent.
- Nota: alguns entorns/IPs bloquegen `api.deepseek.com` (TLS reset). No és un bug
  del codi; a GitHub Actions funciona.

## Automatització (GitHub Actions)

| Workflow | Quan | Què fa |
|---|---|---|
| `.github/workflows/weekly-roundup.yml` | dilluns 08:00 UTC | `python main.py` (recull setmanal) |
| `.github/workflows/sx3-graella.yml` | divendres 08:50 UTC | `python sx3_schedule.py --push --quiet` |
| `.github/workflows/manga-novetats.yml` | dilluns 09:00 UTC | `python bluesky_manga.py --push --quiet` (novetats de manga) |

Tots tres tenen `workflow_dispatch` (botó **Run workflow** per provar-los a mà).
**Secrets necessaris** (Settings ▸ Secrets ▸ Actions): `DEEPSEEK_API_KEY` i
`MAKE_WEBHOOK_URL` (compartits pels tres fluxos).

> Nota DST: el cron de GitHub és sempre UTC. 08:50 UTC = 09:50 a l'hivern /
> 10:50 a l'estiu a Catalunya. No es pot fixar l'hora local tot l'any amb un sol cron.

## Com executar-ho en local

```bash
# Recull setmanal
python main.py                         # genera i envia a make
python main.py --no-llm                # sense DeepSeek (extractes en brut)

# Graella de SX3
python sx3_schedule.py --from-next-friday   # finestra real dv→dj (CSV + preview)
python sx3_schedule.py --post               # imprimeix el post Markdown (no publica)
python sx3_schedule.py --push               # envia el post a make (→ Reddit)
python sx3_schedule.py --debug              # estadístiques crues de l'API

# Novetats de manga (Bluesky)
python bluesky_manga.py --debug             # estadístiques crues del feed
python bluesky_manga.py --post              # preview (títol + URL d'imatge)
python bluesky_manga.py --push              # publica a make si hi ha post nou
python bluesky_manga.py --no-llm --post     # només filtre determinista
```

## Convencions

- **Català** a tot el codi (comentaris, logs, sortida d'usuari) i als commits.
- **Robustesa**: una font o crida que falla no ha de tombar tot el procés; es
  registra al log i es continua (vegeu `scrape_source`, fallbacks de DeepSeek).
- **Secrets only via `.env`** (carregat a `config.py`); mai hardcodejats ni
  commitejats. `.env` està al `.gitignore`; `.env.example` documenta les claus.
- Quan toquis la publicació, recorda: **un sol webhook de make per al canal**.
