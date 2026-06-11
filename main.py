#!/usr/bin/env python3
"""
main.py — Punt d'entrada. Executa tot el procés setmanal:

    scraping  →  resum/traducció (DeepSeek)  →  post Markdown  →  desa  →  make.com

Ús bàsic:
    python main.py

Opcions:
    python main.py --ignore-history   # no filtra notícies de setmanes anteriors
    python main.py --no-llm           # força no fer servir DeepSeek (extractes)
    python main.py --quiet            # menys soroll a la consola
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime

import config
import processor
import queue_store
from scraper import scrape_all


def setup_logging(quiet: bool = False) -> None:
    config.LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log_file = config.LOGS_DIR / f"scraper_{datetime.now():%Y%m%d}.log"
    handlers: list[logging.Handler] = [logging.FileHandler(log_file, encoding="utf-8")]
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.WARNING if quiet else logging.INFO)
    handlers.append(console)
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        handlers=handlers,
    )


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Recull setmanal d'anime i manga en català.")
    p.add_argument("--ignore-history", action="store_true",
                   help="Inclou també notícies ja publicades en setmanes anteriors.")
    p.add_argument("--no-llm", action="store_true",
                   help="No fer servir DeepSeek (usa els extractes originals).")
    p.add_argument("--publish", action="store_true",
                   help="Després de generar, publica a Reddit (demana confirmació).")
    p.add_argument("--yes", "-y", action="store_true",
                   help="Amb --publish, publica sense demanar confirmació.")
    p.add_argument("--quiet", action="store_true", help="Menys missatges a la consola.")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    if args.no_llm:
        config.USE_LLM = False
    setup_logging(args.quiet)
    log = logging.getLogger("anime-scraper.main")

    log.info("=== Inici del recull setmanal (%s) ===", datetime.now())
    log.info("DeepSeek actiu: %s | make.com: %s",
             config.USE_LLM, bool(config.MAKE_WEBHOOK_URL))

    # 1) Scraping
    items = scrape_all()

    # 2) Filtre d'històric (no repetir notícies de setmanes anteriors)
    if not args.ignore_history:
        history = processor.load_history()
        before = len(items)
        items = [it for it in items if it.dedupe_key() not in history]
        if before != len(items):
            log.info("S'han omès %d notícies ja publicades anteriorment.",
                     before - len(items))

    log.info("Total de notícies per al post: %d", len(items))

    # 3) Resum + traducció al català
    processor.summarize_items(items)

    # 4) Construcció i desat del post
    structured = processor.build_post(items)
    md_path, json_path = processor.save_outputs(structured)

    # 5) Encua el post perquè l'extensió de Chrome el publiqui
    queue_store.enqueue(structured)

    # 6) Actualitza l'històric
    if items:
        processor.update_history(items)

    print("\n" + "=" * 60)
    print(f"✅ Post generat: {md_path}")
    print(f"   Notícies incloses: {structured['item_count']}")
    print(f"   Encuat per a l'extensió: queue/index.json (font: {json_path})")
    if not config.USE_LLM:
        print("   ⚠️  Sense DeepSeek: resums en brut (afegeix DEEPSEEK_API_KEY al .env).")
    print("=" * 60)

    # 7) Publicació opcional a Reddit
    if args.publish:
        import publisher
        try:
            submission = publisher.publish(structured, skip_confirm=args.yes)
            if submission is not None:
                print(f"✅ Publicat: https://www.reddit.com{submission.permalink}")
        except RuntimeError as exc:
            print(f"❌ {exc}")
            return 1
    else:
        print("ℹ️  Per publicar-ho a Reddit: revisa el .md i executa  python publish.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
