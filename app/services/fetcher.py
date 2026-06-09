"""
fetcher.py
----------
Responsável por buscar os dados brutos da API SPPO e
passar pelo pipeline Gold do processor.py.

O cache TTLCache (30 s) garante que múltiplas requisições
ao app não sobrecarreguem a API da Mobilidade Rio.
"""

import logging
from datetime import datetime, timezone

import requests
from cachetools import TTLCache, cached

from services.processor import processar_snapshot

logger = logging.getLogger(__name__)

SPPO_URL = "https://dados.mobilidade.rio/gps/sppo"
CACHE_TTL = 30          # segundos — mesmo intervalo de atualização da API
CACHE_MAX = 1           # só precisamos do snapshot mais recente

_cache: TTLCache = TTLCache(maxsize=CACHE_MAX, ttl=CACHE_TTL)


def _fetch_raw() -> list[dict]:
    """Chama a API SPPO e retorna a lista bruta de registros."""
    try:
        resp = requests.get(SPPO_URL, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        # A API pode retornar {"veiculos": [...]} ou diretamente [...]
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ("veiculos", "data", "result", "results"):
                if key in data and isinstance(data[key], list):
                    return data[key]
        logger.warning("Formato inesperado da API SPPO: %s", list(data.keys()) if isinstance(data, dict) else type(data))
        return []
    except requests.RequestException as e:
        logger.error("Erro ao buscar dados SPPO: %s", e)
        return []


def get_snapshot():
    """
    Retorna o DataFrame Gold tratado.
    Usa cache de 30 s — todas as rotas do app consomem esta função.
    """
    cached_result = _cache.get("snapshot")
    if cached_result is not None:
        return cached_result

    logger.info("Cache miss — buscando dados da API SPPO...")
    raw = _fetch_raw()
    df = processar_snapshot(raw)

    meta = {
        "df": df,
        "total_bruto": len(raw),
        "atualizado_em": datetime.now(timezone.utc).isoformat(),
    }
    _cache["snapshot"] = meta
    logger.info("Snapshot atualizado: %d registros brutos → %d operacionais", len(raw), len(df))
    return meta