# Novetats mensuals de manga en català (Bluesky → make → Reddit)

**Data:** 2026-06-11 · **Estat:** aprovat

## Objectiu

Publicar a r/AnimeCatala, un cop al mes, el post de "Llançaments de manga en
català" del compte de Bluesky **@samfainavisual.bsky.social**, com a **post
d'imatge** (només títol + URL de la imatge), reutilitzant l'únic webhook de
make.com del canal. Sense DeepSeek: la detecció del post mensual és
determinista.

## Context i decisions clau

- El compte publica cada mes (observat: 4 de maig, 3 de juny; pot arribar fins
  al dia ~10) un post amb el format fix
  `🗓️ LLANÇAMENTS MANGA EN CATALÀ DEL MES DE <MES>!` i una imatge amb la llista.
  **Un regex el detecta sense LLM**; DeepSeek queda fora d'aquest flux.
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
- **Selecció del post** (tot determinista):
  1. Descarta reposts: ítems del `feed` amb camp `reason`.
  2. Es queda amb posts el text dels quals (normalitzat a majúscules) conté
     `LLANÇAMENTS MANGA EN CATALÀ`.
  3. Guarda de recència: `createdAt` dins dels últims **35 dies** (evita que la
     primera execució pesqui el mes anterior).
  4. Si hi ha més d'un match, agafa el més recent.
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
{ "subreddit": "AnimeCatala", "title": "…", "kind": "link", "url": "<imatge fullsize>" }
```

- Els fluxos existents afegeixen `"kind": "self"` al seu payload (una línia a
  `processor.py` i una a `sx3_schedule.py`).
- **Edició manual a make (fora del repo, la fa l'usuari):** el mòdul de Reddit
  mapa `kind` i `url` des del webhook, mantenint `title` i `markdown`.
- **Pla de proves:** com que `subreddit` ja viatja al payload, els tres fluxos
  es proven primer contra un subreddit de proves (p. ex. `u_<usuari>`)
  abans de validar-los contra r/AnimeCatala.

### 3. Dedup: `output/bsky_history.json`

- Llista JSON d'URIs de posts de Bluesky ja processats.
- `load`/`update` amb el mateix patró que `processor.py`.
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
- **Secrets:** només `MAKE_WEBHOOK_URL`. Cap dependència de `DEEPSEEK_API_KEY`.

### 5. Configuració (`config.py`)

Noves claus: `BSKY_ACTOR` (amb default `samfainavisual.bsky.social`),
`BSKY_HISTORY_FILE` (`output/bsky_history.json`). Reutilitza
`MAKE_WEBHOOK_URL` i `SUBREDDIT`.

## Gestió d'errors

| Cas | Comportament |
|---|---|
| Cap post mensual al feed | Sortida silenciosa, codi 0 (cas normal la majoria de dilluns). |
| Post trobat però ja a l'històric | Sortida silenciosa, codi 0. |
| Post trobat sense imatge | Avís al log, codi 0, no publica. |
| Error HTTP/xarxa (Bluesky o make) | Log d'error, codi 1 (Actions en vermell). |
| Post reeditat (URI nova el mateix mes) | Risc menor acceptat: es publicaria de nou. |

## Verificació

Sense framework de tests al repo; verificació manual seguint la convenció:

1. `python bluesky_manga.py --debug` → el feed es descarrega i es filtra bé.
2. `python bluesky_manga.py --post` → preview del títol + URL d'imatge.
3. `--push` amb subreddit de proves → el post d'imatge apareix bé via make.
4. Run manual dels workflows del recull i de SX3 contra el subreddit de proves
   per validar que `kind: "self"` no trenca res.
5. Segona execució de `--push` → no publica (dedup).

## Documentació

Actualitzar CLAUDE.md: contracte del webhook (camp `kind` i `url`), fila nova a
"Fitxers clau", fila nova a la taula de workflows, i nota que aquest flux no
usa DeepSeek.

## Fora d'abast

- OCR / transcripció de la imatge (DeepSeek no té visió al client actual).
- Publicació per l'API de Reddit (PRAW segueix sent codi heretat sense ús).
- Tercer escenari de make (impossible: límit de 2 actius).
