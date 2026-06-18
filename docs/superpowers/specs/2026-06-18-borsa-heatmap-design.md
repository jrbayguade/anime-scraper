# Heatmap diari de la borsa (yfinance → R2 → cua del Worker → Reddit)

**Data:** 2026-06-18 · **Estat:** aprovat

## Objectiu

Cinquena sortida del repo: publicar **cada dia de mercat** a **r/lapelaeslapela**
(comunitat catalana de diners/economia) un **heatmap del tancament de l'S&P 500**
desglossat pels 11 sectors GICS, acompanyat d'un **comentari en català** generat
per DeepSeek a partir de les dades reals. El heatmap es genera amb matplotlib,
s'hostatja a **Cloudflare R2** (per tenir-ne URL pública) i es publica com a
**post d'imatge** + **primer comentari** (el relat) via la cua del Worker
(`queue_store`), igual que la resta de sortides.

## Context i decisions clau

- **DeepSeek no sap què va passar a la borsa.** El patró és sempre: yfinance
  calcula els números reals → es passen a DeepSeek → DeepSeek escriu el relat.
  Mai a l'inrevés.
- **Imatge generada → cal hostatjar-la.** La cua espera una `url` per als posts
  d'imatge; un PNG de matplotlib no en té. Per això s'hi afegeix `r2_upload.py`,
  que puja a R2 i retorna la URL pública. (El flux de manga no ho necessita perquè
  la imatge ja viu al CDN de Bluesky.)
- **El relat va al primer comentari** (`comment_markdown`), perquè un post
  d'imatge de Reddit no té cos de text. **Dependència:** cal que l'extensió de
  Chrome publiqui aquest comentari als posts d'imatge; el seu codi viu en un altre
  repo i s'ha de verificar/afegir. Si l'extensió encara no ho fa, sortiria només
  la imatge (degradació acceptable; no bloqueja aquesta feina).
- **Fallback de DeepSeek NO fatal** (a diferència d'`endevina_anime.py`): la
  imatge és el contingut principal, així que si DeepSeek cau o falta la clau, es
  publica igualment amb un **comentari determinista** (millor/pitjor sector + S&P).
- **Idempotència i festius:** `output/borsa_history.json` guarda l'última sessió
  publicada (`{"last_session": "YYYY-MM-DD"}`). Si la data de l'última sessió que
  retorna yfinance no és nova (cap de setmana, festiu de borsa o doble execució),
  **no es publica res**. Així el cron pot disparar-se sense por a duplicar.
- **Robustesa:** yfinance de tant en tant peta o limita a CI. S'aplica la
  convenció del projecte: es registra al log i s'acaba net (codi d'error amb
  `--push` perquè es vegi el workflow vermell, però sense tombar res més).

## Components

### 1. `borsa.py` (script nou, autònom)

Mirall de l'estil d'`endevina_anime.py` / `sx3_schedule.py`. CLI:

```bash
python borsa.py --post   # preview: imprimeix dades + desa el PNG a output/, no encua
python borsa.py --push   # genera, puja a R2 i encua a la cua del Worker
python borsa.py --no-llm  # comentari determinista (sense DeepSeek)
python borsa.py --debug  # imprimeix la taula crua de % per sector
```

Pipeline intern:

1. **Dades** (`fetch_sectors`): descarrega amb yfinance els 11 ETFs SPDR de sector
   + `SPY`, i calcula el % de l'última sessió tancada (close vs close anterior).
   Retorna la data de la sessió i una llista ordenada `(sector_ca, ticker, pct)`.
   - Sectors → etiqueta catalana:
     `XLK` Tecnologia · `XLF` Finances · `XLE` Energia · `XLV` Salut ·
     `XLY` Consum discrecional · `XLP` Consum bàsic · `XLI` Indústria ·
     `XLB` Materials · `XLU` Utilities (Serveis públics) · `XLRE` Immobiliari ·
     `XLC` Comunicacions.
2. **Skip** (`already_published`): si la data de sessió == `last_session` de
   l'històric → retorna sense fer res (festiu/cap de setmana/doble run).
3. **Heatmap** (`render_heatmap`): graella matplotlib (4×3, 11 sectors + casella
   resum S&P), color divergent vermell↔verd centrat a 0% i clampat a ±2% (o
   percentil per evitar que un outlier aplani la resta). Cada casella: nom del
   sector + % amb signe. Títol: `Tancament S&P 500 · DD/MM/YYYY`. Desa PNG a
   `output/borsa-YYYY-MM-DD.png`.
4. **Hostatge** (`r2_upload.upload_bytes`): puja el PNG i obté la URL pública.
5. **Comentari** (`build_comment`): amb DeepSeek (to de marca finances/cat +
   pregunta final per generar comentaris) a partir de la taula de %; si falla,
   comentari determinista.
6. **Publicació** (`build_payload` + `queue_store.enqueue`):
   ```json
   {
     "generated_at": "<ISO>",
     "tipus": "imatge",
     "title": "📊 Tancament de Wall Street · DD/MM/YYYY — S&P 500 +0,8%",
     "subreddit": "lapelaeslapela",
     "url": "<URL del PNG a R2>",
     "comment_markdown": "<relat + pregunta>",
     "source": "borsa",
     "source_label": "Borsa"
   }
   ```
   Si s'encua bé, actualitza `borsa_history.json`.

### 2. `r2_upload.py` (mòdul nou, genèric)

No conté res específic de borsa: reutilitzable per futurs packs amb imatge.

- `upload_bytes(data: bytes, key: str, content_type="image/png") -> str`
  Puja a R2 via API S3-compatible (`boto3`) i retorna `R2_PUBLIC_BASE + "/" + key`.
- Client S3 cap a l'endpoint `https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com`,
  regió `auto`, signatura v4.
- Config via env (a `config.py`): `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`,
  `R2_SECRET_ACCESS_KEY`, `R2_BUCKET`, `R2_PUBLIC_BASE` (URL `*.r2.dev` o domini
  propi del bucket públic).
- Si falten credencials, llança un error clar (amb `--push`); en mode `--post`
  només es desa el PNG a `output/` i no es puja.

### 3. Retoc genèric a `queue_store.py`

`_enqueue_worker` ha de preferir `payload["source"]` / `payload["source_label"]`
quan hi siguin, amb fallback a `config.QUEUE_SOURCE` / `QUEUE_SOURCE_LABEL`. Així
`borsa.py` marca la seva font sense dependre d'una env global compartida (el
recull setmanal i la borsa poden conviure al mateix `.env` local). Canvi mínim i
compatible cap enrere.

### 4. `config.py`

Afegir: `BORSA_SUBREDDIT` (default `"lapelaeslapela"`), els `R2_*`, i les
constants de borsa que convingui (llista de sectors/etiquetes pot viure a
`borsa.py` per mantenir `config.py` net, com fa `sx3_schedule.py` amb les seves).
`borsa_history.json` → `BORSA_HISTORY_FILE = OUTPUT_DIR / "borsa_history.json"`.

### 5. `.github/workflows/borsa.yml`

- Cron `0 7 * * 2-6` (07:00 UTC, dimarts–dissabte): cobreix el tancament dels EUA
  de la sessió anterior (el mercat tanca ~20–21 h UTC). Els festius es gestionen
  en codi (skip per data de sessió).
- `workflow_dispatch` per provar a mà.
- Executa `python borsa.py --push --quiet`.
- Secrets: `DEEPSEEK_API_KEY`, `WORKER_URL`, `WORKER_WRITE_TOKEN`,
  `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET`,
  `R2_PUBLIC_BASE` (i opcionalment `BORSA_SUBREDDIT`).

### 6. `tests/test_borsa.py`

Amb yfinance mockejat (sense xarxa): càlcul de % i ordenació, lògica de skip per
història, mapatge de color (signe → costat de la paleta), i muntatge del payload
(`tipus="imatge"`, `url`, `comment_markdown`, `subreddit`). El comentari
determinista es prova sense DeepSeek (`--no-llm`).

### 7. Dependències i docs

- `requirements.txt`: `yfinance`, `matplotlib`, `boto3`.
- `.env.example`: documentar `R2_*` i `BORSA_SUBREDDIT`.
- `CLAUDE.md`: afegir la 5a sortida a «Què és», a la taula de fitxers, a la taula
  de workflows i una secció breu «Font de dades de la borsa».

## Fora d'abast (YAGNI)

- Treemap estil Finviz (mida per capitalització): es va descartar a favor dels 11
  sectors.
- Altres índexs (Nasdaq, Dow, Europa): es pot afegir més endavant si interessa.
- Hostatge alternatiu (GitHub raw, Imgur): es va triar R2.
- Tornar a make/PRAW: llegat sense ús.

## Riscos

- **Extensió i comentaris d'imatge** (vegeu decisions): cal verificar/afegir.
- **Fragilitat de yfinance** a CI: mitigada amb fail-soft i logs; si es torna
  crònic, caldria una font alternativa (Stooq, etc.) — fora d'abast ara.
- **Bucket R2 públic:** assegurar que `R2_PUBLIC_BASE` serveix els objectes
  públicament (r2.dev activat o domini connectat) i que el `key` no col·lisiona
  (s'usa la data de sessió, única per dia).
