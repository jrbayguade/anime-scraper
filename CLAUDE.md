# CLAUDE.md

Guia per treballar en aquest repositori. Documenta els **fonaments no obvis**:
sobretot, com es publica a Reddit (que NO és per l'API). Llegeix-ho abans de
tocar res relacionat amb la publicació.

## Què és

Bot que prepara contingut en català i el publica a Reddit (sobretot
**r/AnimeCatala**; vegeu la 5a sortida, que va a un altre subreddit).
Té **sis sortides independents**, cadascuna amb el seu cron de GitHub Actions:

1. **Recull setmanal** (dilluns) — `main.py`. Fa webscraping de notícies d'anime
   (El Racó del Manga, Fansubs.cat, Anime Corner), les resumeix/tradueix amb
   DeepSeek i en munta un post.
2. **Graella de SX3** (divendres) — `sx3_schedule.py`. Agafa la graella del canal
   SX3 de 3Cat per al cap de setmana (divendres→dijous) i en munta un post amb
   els animes, una intro i una pregunta per generar comentaris.
3. **Novetats de manga** (dimarts) — `bluesky_manga.py`. Un cop al mes, agafa el
   post de "Llançaments de manga en català" del compte de Bluesky @samfainavisual
   i el publica com a **post d'imatge** (títol + URL). Detecció determinista; amb
   una **xarxa de seguretat DeepSeek acotada** si el filtre per frase falla.
4. **Endevina-ho, otaku!** (dimecres i dissabte) — `endevina_anime.py`. Joc per
   endevinar coses d'anime/manga: demana a DeepSeek una endevinalla d'una
   **categoria rotativa** (personatge, sèrie, autor/a, estudi, opening, cita,
   època, trivia) i la munta com a post de text amb la solució amagada amb el
   **spoiler de Reddit** (`>!resposta!<`). Encua a la cua del Worker via
   `queue_store` (com la graella i les novetats). Sense fallback estàtic: si
   DeepSeek no respon, el procés acaba amb error (workflow vermell).

5. **Heatmap de la borsa** (dies de mercat) — `borsa.py`. Cada matí (dt–ds) agafa
   amb yfinance el tancament de l'S&P 500, en pinta una imatge amb un **heatmap
   de sectors + un treemap estil Finviz** dels valors principals (matplotlib +
   squarify), el puja a **Cloudflare R2** (`r2_upload.py`) per
   tenir-ne URL, i publica un **post d'imatge a r/lapelaeslapela** amb un comentari
   en català generat per DeepSeek (fallback determinista si DeepSeek cau). No
   publica en festius de borsa (idempotència per data de sessió).

6. **Explorant Catalunya** (diari) — `explorant.py`. Pack multi-font (9 webs
   catalanes d'activitats/escapades en família, cadascuna amb el seu calendari).
   Un sol cron diari; el script decideix quines fonts «toquen» avui (per dia de
   setmana / setmana del mes), en tria UNA fitxa nova, la resumeix amb DeepSeek,
   **re-allotja la foto a R2** i la publica a **r/ExplorantCatalunya** com a post
   d'imatge + primer comentari (resum + enllaç a la font en cursiva). Dedup amb
   `output/explorant_history.json`. Fonts implementades: 7/9 (vegeu la secció de
   fonts).

Totes les sortides acaben **encuant un JSON a la cua del Cloudflare Worker**
(`queue_store.enqueue`), i una **extensió de Chrome** llegeix la cua i publica a
Reddit. Cada ítem porta el seu `subreddit`, així que no totes van a r/AnimeCatala.

## ⚠️ Com es publica a Reddit (FONAMENTAL)

**No publiquem per l'API de Reddit.** No hi ha cap "script app" aprovada ni API
key. El camí actual és:

```
scraper → queue_store.enqueue() → POST /enqueue al Cloudflare Worker privat
        → l'extensió de Chrome llegeix la cua → publicació (assistida) a Reddit
```

- **PRAW NO funciona.** Existeix `publisher.py` (publicació directa amb PRAW) i
  les variables `REDDIT_*` a `.env.example`, però són **codi heretat sense ús**:
  no hi ha credencials vàlides ni app aprovada. No proposis publicar per PRAW.
- **make.com és LLEGAT.** Hi ha encara `MAKE_WEBHOOK_URL`, `push_to_make()` (a
  `sx3_schedule.py` i `bluesky_manga.py`) i `config.MAKE_BODY_MAX_ENCODED`, però
  **cap camí `--push` els fa servir**: tots criden `queue_store.enqueue()`. Es va
  abandonar make (límit de 2 escenaris i el seu mòdul de Reddit ficava el text
  dins la URL i petava amb HTTP 414 si passava de ~7,5 KB). No proposis tornar-hi.
- **La cua NO està lligada a r/AnimeCatala.** Cada ítem porta el seu propi camp
  `subreddit`, i un `source`/`source_label` perquè l'extensió agrupi per pack
  (`config.QUEUE_SOURCE` / `QUEUE_SOURCE_LABEL`). Així una sortida nova pot
  publicar a **un altre subreddit** sense canviar la canonada (només cal tenir
  permís per postejar-hi des del compte que fa servir l'extensió).

### El Worker és genèric i reutilitzable

`queue_store.py` **no conté res específic d'anime**: és copiable tal qual a futurs
packs. Si `WORKER_URL` + `WORKER_WRITE_TOKEN` estan definits, `enqueue()` publica
al Worker i prou (no toca `queue/` ni GitHub). Si no, escriu fitxers a `queue/`
(fallback local / tests offline).

### Contracte de la cua (`queue_store.enqueue`)

El `payload` mínim que rep `enqueue()`:

```json
{
  "generated_at": "2026-06-18T08:53:34",
  "tipus": "text",
  "title": "…",
  "subreddit": "AnimeCatala",
  "markdown": "… (cos del post, només si tipus=text) …"
}
```

- **`tipus: "text"`** → post de text: s'usa `markdown` (cos complet ja muntat).
- **`tipus: "imatge"`** → post d'imatge: s'usa `url` (la imatge). Un post d'imatge
  de Reddit **no té cos de text**.
- **`comment_markdown`** (opcional, qualsevol `tipus`) → text per a un **primer
  comentari** al post. Aquest és el mecanisme per acompanyar una imatge amb text:
  post d'imatge + comentari. L'índex en porta el flag `has_comment`. *(Pendent de
  confirmar que l'extensió publica aquest comentari; el seu codi viu en un altre
  repo.)*

`enqueue()` mapeja `generated_at` → `created_at` del Worker i hi afegeix
`source`/`source_label`.

> **Imatges generades (p.ex. un heatmap):** la cua espera una `url` per als posts
> d'imatge, no un fitxer. El flux de manga funciona perquè la imatge ja viu al CDN
> de Bluesky. Una imatge generada localment (matplotlib, etc.) **s'ha d'hostatjar
> primer** (Cloudflare R2 seria el camí natural) per tenir-ne una URL pública.

## Fitxers clau

| Fitxer | Rol |
|---|---|
| `config.py` | Configuració central: fonts, claus d'API, `WORKER_URL`/`WORKER_WRITE_TOKEN`, `QUEUE_SOURCE`, subreddit, límits. Tot des de `.env`. |
| `queue_store.py` | **Cua de publicació (genèric).** `enqueue()` → POST al Worker (o fitxers a `queue/` com a fallback). Reutilitzable per altres packs. |
| `scraper.py` | Recull setmanal: descàrrega i parsing de fonts (un parser per font, registrat a `PARSERS`). |
| `processor.py` | Recull setmanal: resum/traducció amb DeepSeek, munta el Markdown, històric. (`push_to_make` hi és com a llegat sense ús.) |
| `publisher.py` | Publicació directa amb PRAW. **Heretat / sense ús** (vegeu secció de Reddit). |
| `main.py` | Punt d'entrada del recull setmanal → `queue_store.enqueue()`. |
| `sx3_schedule.py` | Graella d'anime de SX3 (autònom): API de 3Cat → post Markdown + DeepSeek → `--push` encua al Worker. |
| `bluesky_manga.py` | Novetats mensuals de manga (autònom): feed de Bluesky → selecció determinista (+xarxa DeepSeek acotada) → `--push` encua post d'imatge al Worker. |
| `endevina_anime.py` | Joc «Endevina-ho, otaku!» (autònom): categoria rotativa + DeepSeek → post de text amb solució amb spoiler → cua del Worker. |
| `explorant.py` | Pack «Explorant Catalunya» (autònom): 9 fonts d'activitats en família amb calendaris diferents → DeepSeek + foto a R2 → r/ExplorantCatalunya. |
| `borsa.py` | Heatmap diari de la borsa (autònom): yfinance (11 sectors S&P) → matplotlib → R2 → DeepSeek → post d'imatge a r/lapelaeslapela. |
| `r2_upload.py` | **Pujada d'imatges a R2 (genèric).** Per a posts d'imatge amb una imatge GENERADA (no una URL externa). Reutilitzable. |

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

## Font de dades de la borsa (yfinance)

`borsa.py` baixa amb **yfinance** els tancaments diaris de l'ETF `SPY` (titular) i
els 11 ETFs SPDR de sector GICS (`XLK`, `XLC`, `XLY`, `XLP`, `XLE`, `XLF`, `XLV`,
`XLI`, `XLB`, `XLRE`, `XLU`) i en calcula el % (close vs close anterior).

- **Separació I/O vs lògica:** `fetch_closes()` és l'única part que toca xarxa
  (i la que mockegen els tests); `compute_changes()` i `build_rows()` són pures.
- **Idempotència/festius:** `output/borsa_history.json` (`{"last_session"}`) evita
  duplicar; si la data de l'última sessió no és nova, `--push` no encua res.
- **Imatge (2 parts):** un sol PNG amb el heatmap dels 11 sectors a dalt (clamp
  ±2%) i, a sota, un **treemap estil Finviz** dels ≈40 valors principals
  (`CONSTITUENTS`): mida per capitalització (`fetch_market_caps`, yfinance
  `fast_info`, en viu) i color pel % del dia (clamp ±3%, via `squarify`).
  `render_image()` és fail-soft: sense capitalitzacions dibuixa només els sectors.
  matplotlib (backend `Agg`). El PNG es puja a R2 (`r2_upload.upload_bytes`).
- **Comentari:** DeepSeek rep els números i els relata (mai inventa dades); si
  falla, comentari determinista. Va al `comment_markdown` (primer comentari).
- yfinance pot petar/limitar a CI: s'aplica la convenció de robustesa (fail-soft).

## Font de dades d'Explorant Catalunya (multi-font)

`explorant.py` té un registre `SOURCES` (clau → nom, web, parser) i un calendari
`sources_due(date)` que decideix què publicar avui. **Un sol cron diari** n'hi ha
prou. Cada parser retorna `list[Fitxa]`; es tria la primera no publicada (dedup per
`source_key|url` a `explorant_history.json`). El resum prim s'enriqueix amb
l'`og:description` de l'article abans de passar-lo a DeepSeek.

| Font | Quan | Mètode | Estat |
|---|---|---|---|
| escapadaambnens (festes/fires del mes que ve) | dia 1 de mes | HTML (`a.item_festival`) | ✅ |
| elmonensespera | dimarts | wp-json (RSS bloquejat) | ✅ |
| sortirambnens | dimecres | RSS de categoria | ✅ |
| surtdecasa | dijous | HTML (`.views-row`) | ✅ |
| femturisme | divendres | — | ⏳ JS (incompatible amb CI) |
| barcelona_nens | dissabte | HTML (`article`) | ✅ |
| senders_feec | 1r dilluns | — | ⏳ mapa JS / sense API |
| dexcursio | 2n dilluns | RSS + og:image | ✅ |
| timeout | 3r dilluns | HTML (`article`) | ✅ |

> **Per què 2 pendents:** femturisme i senders_feec pinten el contingut amb
> JavaScript (o un mapa extern sense API pública), i el pipeline de CI fa servir
> `requests` sense navegador, així que no es poden scrapejar tal com estan. El seu
> parser és un stub que registra l'avís i no publica res (no peta). Per activar-los
> caldria trobar-ne l'API interna o renderitzar amb un navegador (fora del CI).

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
| `.github/workflows/manga-novetats.yml` | dimarts 09:00 UTC | `python bluesky_manga.py --push --quiet` (novetats de manga) |
| `.github/workflows/endevina-anime.yml` | dimecres i dissabte 18:00 UTC | `python endevina_anime.py --push --quiet` (joc otaku) |
| `.github/workflows/borsa.yml` | dt–ds 04:00 UTC (05:00/06:00 CAT) | `python borsa.py --push --quiet` (heatmap de la borsa) |
| `.github/workflows/explorant.yml` | diari 06:00 UTC | `python explorant.py --push --quiet` (el script tria què toca avui) |

Tots tenen `workflow_dispatch` (botó **Run workflow** per provar-los a mà).
**Secrets necessaris** (Settings ▸ Secrets ▸ Actions): `DEEPSEEK_API_KEY`,
`WORKER_URL` i `WORKER_WRITE_TOKEN` (compartits per les cinc sortides). La borsa
necessita, a més, els secrets de R2 (`R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`,
`R2_SECRET_ACCESS_KEY`, `R2_BUCKET`, `R2_PUBLIC_BASE`) i opcionalment
`BORSA_SUBREDDIT`.

> Nota DST: el cron de GitHub és sempre UTC. 08:50 UTC = 09:50 a l'hivern /
> 10:50 a l'estiu a Catalunya. No es pot fixar l'hora local tot l'any amb un sol cron.

## Com executar-ho en local

```bash
# Recull setmanal
python main.py                         # genera i encua al Worker
python main.py --no-llm                # sense DeepSeek (extractes en brut)

# Graella de SX3
python sx3_schedule.py --from-next-friday   # finestra real dv→dj (CSV + preview)
python sx3_schedule.py --post               # imprimeix el post Markdown (no publica)
python sx3_schedule.py --push               # encua el post al Worker (→ Reddit)
python sx3_schedule.py --from-next-friday --manual  # publicació manual assistida (sense extensió)
python sx3_schedule.py --debug              # estadístiques crues de l'API

# Novetats de manga (Bluesky)
python bluesky_manga.py --debug             # estadístiques crues del feed
python bluesky_manga.py --post              # preview (títol + URL d'imatge)
python bluesky_manga.py --push              # encua al Worker si hi ha post nou
python bluesky_manga.py --manual            # publicació manual assistida (sense extensió)
python bluesky_manga.py --no-llm --post     # només filtre determinista

# Joc «Endevina-ho, otaku!»
python endevina_anime.py --post             # preview del joc (no encua res)
python endevina_anime.py --push             # genera i encua a la cua del Worker

# Heatmap de la borsa
python borsa.py --debug                     # taula crua de % per sector
python borsa.py --post                      # desa el PNG a output/ + comentari (no puja ni encua)
python borsa.py --push                      # genera, puja a R2 i encua al Worker
python borsa.py --no-llm --post             # comentari determinista (sense DeepSeek)
```

> **Publicació manual (`--manual`):** alternativa a l'extensió (publicar del tot
> a mà). `bluesky_manga.py --manual` copia el títol al porta-retalls, baixa la
> imatge a `output/manga-<mes>.<ext>` i obre Reddit; tu enganxes el títol, puges
> la imatge i cliques Post. Equivalent a `publish_manual.py` del recull setmanal.
> (La imatge de Bluesky sol venir en **WebP**; si Reddit el rebutja, cal
> convertir-la a PNG/JPG.)

## Convencions

- **Català** a tot el codi (comentaris, logs, sortida d'usuari) i als commits.
- **Robustesa**: una font o crida que falla no ha de tombar tot el procés; es
  registra al log i es continua (vegeu `scrape_source`, fallbacks de DeepSeek).
- **Secrets only via `.env`** (carregat a `config.py`); mai hardcodejats ni
  commitejats. `.env` està al `.gitignore`; `.env.example` documenta les claus.
- Quan toquis la publicació, recorda: **tot passa per `queue_store.enqueue()`**
  (cua del Worker + extensió). Cada ítem porta el seu `subreddit`, així que el
  destí no està fixat a r/AnimeCatala. make/PRAW són llegat sense ús.
