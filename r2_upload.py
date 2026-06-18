"""
r2_upload.py — Pujada d'imatges a Cloudflare R2 (genèric, reutilitzable).

Per publicar un post d'IMATGE amb una imatge GENERADA localment (un heatmap de
matplotlib, un gràfic...), la cua necessita una URL pública. Aquest mòdul puja
els bytes a un bucket R2 via l'API S3-compatible (`boto3`) i en retorna la URL.

No conté res específic de cap pack: només depèn de les variables R2_* de
`config.py`. Es pot copiar tal qual a futurs scrapers amb imatge.

Contracte:
    url = upload_bytes(png_bytes, "borsa/2026-06-18.png")
    # -> "https://<base-pública>/borsa/2026-06-18.png"
"""
from __future__ import annotations

import logging

import config

log = logging.getLogger(__name__)


def is_configured() -> bool:
    """True si hi ha prou config R2 per pujar (credencials + bucket + base)."""
    return all([
        config.R2_ACCOUNT_ID,
        config.R2_ACCESS_KEY_ID,
        config.R2_SECRET_ACCESS_KEY,
        config.R2_BUCKET,
        config.R2_PUBLIC_BASE,
    ])


def _client():
    """Crea un client S3 apuntant a l'endpoint de R2.

    Import diferit de boto3: així el projecte (i la resta de sortides) no depèn
    de boto3 si no es fa servir R2.
    """
    try:
        import boto3  # type: ignore
        from botocore.config import Config  # type: ignore
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "Falta boto3 per pujar a R2 (afegeix-lo a requirements.txt)."
        ) from exc

    return boto3.client(
        "s3",
        endpoint_url=f"https://{config.R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
        aws_access_key_id=config.R2_ACCESS_KEY_ID,
        aws_secret_access_key=config.R2_SECRET_ACCESS_KEY,
        region_name="auto",
        config=Config(signature_version="s3v4"),
    )


def upload_bytes(data: bytes, key: str, content_type: str = "image/png") -> str:
    """Puja `data` a R2 amb la clau `key` i retorna la URL pública.

    Llança RuntimeError si falta config o si la pujada falla (perquè el flux
    `--push` ho vegi i no encui un post amb una URL invàlida).
    """
    if not is_configured():
        raise RuntimeError(
            "R2 no configurat: calen R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, "
            "R2_SECRET_ACCESS_KEY, R2_BUCKET i R2_PUBLIC_BASE al .env."
        )

    key = key.lstrip("/")
    try:
        _client().put_object(
            Bucket=config.R2_BUCKET,
            Key=key,
            Body=data,
            ContentType=content_type,
        )
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"Pujada a R2 fallida ({key}): {exc}") from exc

    url = f"{config.R2_PUBLIC_BASE}/{key}"
    log.info("Imatge pujada a R2: %s (%d bytes)", url, len(data))
    return url
