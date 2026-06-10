#!/usr/bin/env python3
"""
publish_manual.py — Publicació manual assistida (sense API de Reddit).

Per si no pots/no vols crear una "script app" de Reddit. Copia el cos del post
al porta-retalls de Windows i obre la pàgina de crear post de r/AnimeCatala.
Tu només has d'enganxar (Ctrl+V) i clicar Post.

Ús (a WSL):
    python main.py                  # 1) genera l'esborrany
    python publish_manual.py        # 2) copia el cos + obre Reddit al navegador
    python publish_manual.py --comment   # (després de publicar) copia la galeria
                                         # d'imatges per al primer comentari
"""

from __future__ import annotations

import json
import subprocess
import sys

import config


def _to_windows_clipboard(text: str) -> bool:
    """Posa el text al porta-retalls de Windows (robust amb accents i emojis)."""
    try:
        tmp = config.OUTPUT_DIR / ".clipboard.tmp"
        tmp.write_text(text, encoding="utf-8")
        win_path = subprocess.check_output(["wslpath", "-w", str(tmp)]).decode().strip()
        subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command",
             f"Set-Clipboard -Value (Get-Content -Raw -Encoding UTF8 -LiteralPath '{win_path}')"],
            check=True, stderr=subprocess.DEVNULL,
        )
        tmp.unlink(missing_ok=True)
        return True
    except Exception:
        return False


def _open_browser(url: str) -> None:
    try:
        subprocess.run(["explorer.exe", url], stderr=subprocess.DEVNULL)
    except Exception:
        print(f"   (obre manualment: {url})")


def _md_path(structured: dict):
    stamp = structured.get("generated_at", "")[:10]
    return config.POSTS_DIR / f"{stamp}-anime-catala.md"


def main() -> int:
    if not config.LATEST_JSON.exists():
        print("⚠️  No hi ha cap post generat. Executa primer:  python main.py")
        return 1
    d = json.loads(config.LATEST_JSON.read_text(encoding="utf-8"))

    # Mode comentari: copia només la galeria d'imatges
    if "--comment" in sys.argv:
        text = d.get("comment_markdown", "")
        if not text:
            print("No hi ha galeria d'imatges per copiar.")
            return 0
        ok = _to_windows_clipboard(text)
        print("💬 Galeria d'imatges " +
              ("copiada ✅ — enganxa-la com a PRIMER COMENTARI del post."
               if ok else "NO s'ha pogut copiar ❌ (mira el final del .md)."))
        return 0

    # Mode post: copia el cos i obre el navegador
    title = d["title"]
    body = d["markdown"]
    lines = body.split("\n")
    if lines and lines[0].startswith("# "):   # treu el "# títol" (va al seu camp)
        body = "\n".join(lines[1:]).lstrip("\n")

    ok = _to_windows_clipboard(body)
    _open_browser(f"https://www.reddit.com/r/{d['subreddit']}/submit")

    print("=" * 66)
    print("📋  TÍTOL  (copia'l al camp «Title»):\n")
    print("    " + title + "\n")
    if ok:
        print("✅  El COS del post ja és al porta-retalls → Ctrl+V al quadre de text.")
    else:
        print("⚠️  No s'ha pogut copiar sol. Copia el cos d'aquest fitxer:")
    print("    " + str(_md_path(d)))
    print()
    print("Passos a Reddit (s'ha obert al navegador):")
    print("   1) Tria  Type = Text")
    print("   2) Title → enganxa el títol d'aquí dalt")
    print("   3) Body  → Ctrl+V")
    print("   4) Clica  Post")
    print("   5) (opcional) galeria al 1r comentari:  python publish_manual.py --comment")
    print("=" * 66)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
