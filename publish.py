#!/usr/bin/env python3
"""
publish.py — Publica a Reddit l'últim post generat (pas d'aprovació).

Flux recomanat:
    1. python main.py        # genera l'esborrany (output/posts/*.md)
    2. (revises el .md)       # control de qualitat
    3. python publish.py      # mostra un resum, demana confirmació i publica

Opcions:
    python publish.py --yes   # publica sense demanar confirmació (automatització)
"""

from __future__ import annotations

import json
import logging
import sys

import config
import publisher


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if not config.LATEST_JSON.exists():
        print("⚠️  No hi ha cap post generat encara. Executa primer:  python main.py")
        return 1

    structured = json.loads(config.LATEST_JSON.read_text(encoding="utf-8"))
    skip_confirm = "--yes" in sys.argv or "-y" in sys.argv

    try:
        submission = publisher.publish(structured, skip_confirm=skip_confirm)
    except RuntimeError as exc:
        print(f"❌ {exc}")
        return 1

    if submission is not None:
        print(f"\n✅ Publicat a r/{structured['subreddit']}:")
        print(f"   https://www.reddit.com{submission.permalink}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
