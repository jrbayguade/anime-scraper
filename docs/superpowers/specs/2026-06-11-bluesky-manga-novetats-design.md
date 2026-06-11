# Novetats mensuals de manga en català (Bluesky → make → Reddit)

**Data:** 2026-06-11 · **Estat:** aprovat

## Objectiu

Publicar a r/AnimeCatala, un cop al mes, el post de "Llançaments de manga en
català" del compte de Bluesky **@samfainavisual.bsky.social**, com a **post
d'imatge** (només títol + URL de la imatge), reutilitzant l'únic webhook de
make.com del canal. La detecció del post mensual és **determinista** (filtre per
frase fixa); DeepSeek només actua com a **xarxa de seguretat acotada** quan el
filtre no troba res i encara estem a principi de mes sense haver publicat.

## Context i decisions clau

- El compte publica cada mes (observat: 4 de maig, 3 de juny; pot arribar fins
  al dia ~10) un post amb el format fix
  `🗓️ LLANÇAMENTS MANGA EN CATALÀ DEL MES DE <MES>!` i una imatge amb la llista.
  **El filtre per frase fixa el detecta sense LLM** en el cas normal.
- **Risc del filtre exacte:** només tenim 2 mesos de mostra. Si el compte canvia
  la redacció de la capçalera, el filtre falla en silenci. Per cobrir-ho sense
  disparar overhead, hi ha una **xarxa de seguretat amb DeepSeek acotada**: només
  s'invoca si (a) el filtre determinista no troba res, (b) som dins dels primers
  14 dies del mes, i (c) encara no s'ha publicat el post d'aquest mes. Així fa com
  a molt ~1-2 crides/mes (i para en publicar), no una cada dilluns. Se li envia
  només un **fragment petit** (els textos dels posts recents, sense imatges) amb
  el prompt que retorna `{"target_id": <id>}` o `null`. Prioritzem precisió: el
  filtre exacte mana; l'LLM només omple el buit.
- El post de Reddit serà **d'imatge** (sense cos). Això obliga a una **edició
  manual única de l'escenari de make** (enfocament A, aprovat): el mòdul de
  Reddit passa a mapar el *kind* del post i l'URL des del payload del webhook.
- El dedup reutilitza el patró existent d'històric commitejat des del workflow
  amb `[skip ci]`.

## Components

### 1. `bluesky_manga.py` (script nou, autònom)

Mirall de l'estil de `sx3_schedule.py`:

- **Font:** `GET https://public.api.bsky.app/xrpc/app.bsky.feed.getAuthorFeed`
  amb `actor=samfainavisual.bsky.social`, `limit=50`,
  `filter=posts_no_replies`. API pública, sense autenticació, `requests` pur.
- **Selecció del post (determinista, primari):**
  1. Descarta reposts: ítems del `feed` amb camp `reason`.
  2. Es queda amb posts el text dels quals (normalitzat a majúscules) conté
     `LLANÇAMENTS MANGA EN CATALÀ`.
  3. Guarda de recència: `createdAt` dins dels últims **35 dies** (evita que la
     primera execució pesqui el mes anterior).
  4. Si hi ha més d'un match, agafa el més recent.
- **Xarxa de seguretat (LLM, secundària i acotada):** si la selecció determinista
  retorna `None`, s'invoca DeepSeek **només si** `dia_del_mes ≤ 14` **i** el mes
  actual (`YYYY-MM`) no consta com a publicat a l'històric. Se li passa la llista
  de posts recents no-repost com a `{id, text}` i retorna `{"target_id": <id>}` o
  `null`. Es mapeja l'id a l'`uri` del post. Si no hi ha `DEEPSEEK_API_KEY` o
  falla, retorna `None` i el flux queda en determinista pur. `--no-llm` la
  desactiva.
- **Extracció:**
  - `uri` del post → clau de dedup.
  - Mes: regex `DEL MES DE (\w+)` sobre el text; fallback al mes de
    `createdAt`. Any: de `createdAt`.
  - Imatge: `post.embed.images[0].fullsize`. **Sense imatge → avís al log i
    sortida amb codi 0** (es publica a mà si cal).
- **Títol (plantilla fixa):**
  `📚 Llançaments de manga en català — juny 2026 (via Samfaina Visual)`
- **CLI:** per defecte i `--post` → preview per pantalla (no publica);
  `--push` → envia a make i actualitza l'històric; `--debug` → estadístiques
  crues del feed; `--quiet` → només warnings (per al cron).

### 2. Contracte del webhook (canvi compartit)

Payload nou d'aquest flux:

```json
{ "subreddit": "AnimeCatala", "title": "…", "kind": "image", "url": "<imatge fullsize>" }
```

(`kind: "image"` perquè make publica un post d'imatge natiu a partir de la URL;
els fluxos de text envien `kind: "self"`.)

- Els fluxos existents afegeixen `"kind": "self"` al seu payload (una línia a
  `processor.py` i una a `sx3_schedule.py`). El flux de manga envia
  `"kind": "image"`.
- **Edició manual a make (fora del repo, la fa l'usuari):** el mòdul de Reddit
  mapa `kind` i `url` des del webhook, mantenint `title` i `markdown`.
- **Pla de proves:** com que `subreddit` ja viatja al payload, els tres fluxos
  es proven primer contra un subreddit de proves (p. ex. `u_<usuari>`)
  abans de validar-los contra r/AnimeCatala.

### 3. Dedup i estat: `output/bsky_history.json`

- Objecte JSON amb dues llistes:
  ```json
  { "uris": ["at://…/JUNY"], "months": ["2026-06"] }
  ```
  - `uris`: posts de Bluesky ja publicats → **dedup**.
  - `months`: mesos (`YYYY-MM`, segons el mes del post) ja publicats → **gating
    de la xarxa de seguretat LLM** (no la cridem si el mes ja s'ha cobert).
- El carregador tolera el format antic (llista plana d'URIs) per robustesa.
- S'actualitza **només** després que make respongui 2xx.
- Execucions posteriors del mateix mes: el script veu l'URI a l'històric i surt
  netament amb codi 0. La idempotència fa innecessari "aturar" res.

### 4. Workflow: `.github/workflows/manga-novetats.yml`

- **Cron:** `0 9 * * 1` (cada dilluns 09:00 UTC, una hora després del recull
  setmanal per no col·lidir amb el seu `git push` a main).
- `workflow_dispatch` per a proves manuals.
- Executa `python bluesky_manga.py --push --quiet`.
- Commiteja `output/bsky_history.json` amb `[skip ci]`, fent
  `git pull --rebase` abans del push.
- **Secrets:** `MAKE_WEBHOOK_URL` (sempre) i `DEEPSEEK_API_KEY` (opcional, només
  per a la xarxa de seguretat; tots dos ja existeixen, compartits amb els altres
  fluxos).

### 5. Configuració (`config.py`)

Noves claus: `BSKY_ACTOR` (amb default `samfainavisual.bsky.social`),
`BSKY_HISTORY_FILE` (`output/bsky_history.json`). Reutilitza `MAKE_WEBHOOK_URL`,
`SUBREDDIT` i la config de DeepSeek ja existent (`DEEPSEEK_*`, via el client
`processor._deepseek_chat`) per a la xarxa de seguretat.

## Gestió d'errors

| Cas | Comportament |
|---|---|
| Cap post mensual al feed | Sortida silenciosa, codi 0 (cas normal la majoria de dilluns). |
| Post trobat però ja a l'històric | Sortida silenciosa, codi 0. |
| Post trobat sense imatge | Avís al log, codi 0, no publica. |
| Error HTTP/xarxa (Bluesky o make) | Log d'error, codi 1 (Actions en vermell). |
| DeepSeek sense clau o falla | La xarxa de seguretat retorna `None`; queda determinista pur. Codi 0. |
| Post reeditat (URI nova el mateix mes) | Risc menor acceptat: es publicaria de nou. |

## Verificació

Les funcions pures (selecció determinista, extracció, gating LLM, mapatge de
l'id) es proven amb **pytest** sobre un fixture del feed (única infraestructura
de test nova; pytest no entra a `requirements.txt`, és només per a dev). La
integració (xarxa, make) es verifica manualment:

1. `python -m pytest tests/` → totes les funcions pures verdes.
2. `python bluesky_manga.py --debug` → el feed es descarrega i es filtra bé.
3. `python bluesky_manga.py --post` → preview del títol + URL d'imatge.
4. `--push` amb subreddit de proves → el post d'imatge apareix bé via make.
5. Run manual dels workflows del recull i de SX3 contra el subreddit de proves
   per validar que `kind: "self"` no trenca res.
6. Segona execució de `--push` → no publica (dedup).

## Documentació

- **CLAUDE.md:** contracte del webhook (camp `kind` i `url`), fila nova a
  "Fitxers clau", fila nova a la taula de workflows, secció de la font de
  Bluesky i nota de la xarxa de seguretat LLM acotada.
- **README.md:** secció nova del flux de novetats de manga (ús, cron, secrets) i
  arbre d'estructura actualitzat.

## Fora d'abast

- OCR / transcripció de la imatge (DeepSeek no té visió al client actual).
- Publicació per l'API de Reddit (PRAW segueix sent codi heretat sense ús).
- Tercer escenari de make (impossible: límit de 2 actius).
