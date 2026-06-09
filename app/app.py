"""
app.py
------
Monolito FastAPI. Serve a página HTML e todas as rotas da API.

Rotas:
  GET  /                          → mapa principal
  GET  /api/snapshot              → métricas gerais da operação
  GET  /api/linha/{numero}        → ônibus de uma linha específica (tempo real)
  GET  /api/linhas                → lista de todas as linhas ativas
  GET  /api/rota/{numero}         → nuvem de pontos históricos de uma linha (do parquet)
  GET  /api/proximos              → ônibus próximos (lat, lon, raio_m)
  GET  /api/mapa-calor            → pontos de gargalo para heatmap
  GET  /api/stream                → SSE — posições em tempo real de todos os ônibus
  GET  /api/status                → health check + info do cache

Execução (dentro da pasta app/):
  uvicorn app:app --reload
"""

import os
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.requests import Request

from services.fetcher import get_snapshot
from services.processor import (
    snapshot_metricas,
    filtrar_por_linha,
    filtrar_proximos,
    gargalos_para_heatmap,
    df_para_geojson,
)

app = FastAPI(
    title="Mobilidade Rio — Onde está meu ônibus?",
    description="App de monitoramento em tempo real da frota SPPO do Rio de Janeiro.",
    version="2.1.0",
)

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# ── Caminho do parquet histórico (silver) ────────────────────────────────────
SILVER_PARQUET = Path(os.getenv("SILVER_PATH", "../dados/silver/sppo_silver.parquet"))
SILVER_CSV     = Path(os.getenv("SILVER_CSV",  "../dados/silver/sppo_silver.csv"))

# Limite máximo de pontos retornados na rota histórica para não sobrecarregar o frontend
ROTA_MAX_PONTOS = int(os.getenv("ROTA_MAX_PONTOS", 20_000))


def _carregar_historico() -> pd.DataFrame:
    """Carrega o DataFrame histórico da camada silver."""
    if SILVER_PARQUET.exists():
        return pd.read_parquet(SILVER_PARQUET, engine="pyarrow")
    if SILVER_CSV.exists():
        return pd.read_csv(SILVER_CSV, parse_dates=["datahora"])
    return pd.DataFrame()


# ── Página principal ─────────────────────────────────────────────────────────

@app.get("/", include_in_schema=False)
async def index(request: Request):
    return templates.TemplateResponse(request=request, name="index.html")


# ── API ──────────────────────────────────────────────────────────────────────

@app.get("/api/snapshot", summary="Métricas gerais da operação")
async def api_snapshot():
    meta = get_snapshot()
    metricas = snapshot_metricas(meta["df"])
    return {
        "atualizado_em": meta["atualizado_em"],
        "total_bruto": meta["total_bruto"],
        "operacionais": len(meta["df"]),
        **metricas,
    }


@app.get("/api/linhas", summary="Lista de linhas ativas")
async def api_linhas():
    meta = get_snapshot()
    linhas = sorted(meta["df"]["linha"].unique().tolist())
    return {"linhas": linhas}


@app.get("/api/linha/{numero}", summary="Posição atual dos ônibus de uma linha (tempo real)")
async def api_linha(numero: str):
    meta = get_snapshot()
    df = filtrar_por_linha(meta["df"], numero)

    if df.empty:
        raise HTTPException(
            status_code=404,
            detail=f"Nenhum veículo ativo encontrado para a linha '{numero}'.",
        )

    return {
        "linha": numero.upper(),
        "total_veiculos": len(df),
        "atualizado_em": meta["atualizado_em"],
        "geojson": df_para_geojson(df),
    }


@app.get("/api/rota/{numero}", summary="Nuvem de pontos históricos de uma linha (silver)")
async def api_rota(numero: str):
    """
    Lê o parquet/CSV da camada silver e retorna TODOS os pontos registrados
    para aquela linha como uma nuvem densa de coordenadas.

    O frontend plota cada ponto individualmente com L.circleMarker de baixa
    opacidade: a sobreposição de milhares de pontos forma o "desenho" da rota
    naturalmente, sem precisar de uma polilinha conectada.

    Estrutura de resposta:
      {
        "linha": "485",
        "total_pontos": 18432,
        "pontos": [[lat, lon], ...]   // toda a nuvem, sem deduplicação
      }
    """
    df_hist = _carregar_historico()

    if df_hist.empty:
        raise HTTPException(
            status_code=503,
            detail="Arquivo histórico (silver) não encontrado. Execute a pipeline silver primeiro.",
        )

    # Normaliza e filtra
    df_hist["linha"] = df_hist["linha"].astype(str).str.strip()
    df_linha = df_hist[df_hist["linha"].str.upper() == numero.upper()].copy()

    if df_linha.empty:
        raise HTTPException(
            status_code=404,
            detail=f"Nenhum registro histórico encontrado para a linha '{numero}'.",
        )

    # Converte lat/lon para float
    for col in ["latitude", "longitude"]:
        df_linha[col] = (
            df_linha[col].astype(str).str.replace(",", ".", regex=False)
        )
        df_linha[col] = pd.to_numeric(df_linha[col], errors="coerce")

    df_linha = df_linha.dropna(subset=["latitude", "longitude"])

    # Remove pontos com coordenadas zeradas
    df_linha = df_linha[(df_linha["latitude"] != 0) & (df_linha["longitude"] != 0)]

    if df_linha.empty:
        raise HTTPException(
            status_code=404,
            detail=f"Nenhum ponto válido encontrado para a linha '{numero}'.",
        )

    # Amostragem se exceder o limite — preserva a distribuição espacial
    if len(df_linha) > ROTA_MAX_PONTOS:
        df_linha = df_linha.sample(n=ROTA_MAX_PONTOS, random_state=42)

    # Retorna todos os pontos como nuvem (sem deduplicação de coordenadas,
    # pois a densidade é o que forma visualmente o trajeto da linha)
    pontos = df_linha[["latitude", "longitude"]].values.tolist()

    return {
        "linha": numero.upper(),
        "total_pontos": len(pontos),
        "pontos": pontos,   # [[lat, lon], ...]
    }


@app.get("/api/proximos", summary="Ônibus mais próximos de mim")
async def api_proximos(
    lat: float = Query(..., description="Latitude do usuário"),
    lon: float = Query(..., description="Longitude do usuário"),
    raio_m: float = Query(500, ge=100, le=5000, description="Raio de busca em metros"),
):
    meta = get_snapshot()
    df = filtrar_proximos(meta["df"], lat, lon, raio_m)

    return {
        "lat_usuario": lat,
        "lon_usuario": lon,
        "raio_m": raio_m,
        "total_encontrados": len(df),
        "atualizado_em": meta["atualizado_em"],
        "geojson": df_para_geojson(df),
    }


@app.get("/api/mapa-calor", summary="Mapa de gargalos urbanos")
async def api_mapa_calor(
    max_pontos: int = Query(10_000, ge=100, le=50_000),
):
    meta = get_snapshot()
    pontos = gargalos_para_heatmap(meta["df"], max_pontos)

    return {
        "total_pontos": len(pontos),
        "atualizado_em": meta["atualizado_em"],
        "pontos": pontos,
    }


@app.get("/api/stream", summary="SSE — posições em tempo real de todos os ônibus únicos")
async def api_stream():
    async def event_generator():
        ultimo_timestamp = None
        while True:
            try:
                # CRÍTICO: Executa de forma assíncrona para não travar o FastAPI
                meta = await asyncio.to_thread(get_snapshot)
                ts   = meta["atualizado_em"]

                if ts != ultimo_timestamp:
                    ultimo_timestamp = ts
                    df = meta["df"]

                    if not df.empty:
                        # Deduplicação garantida (apenas a última recorrência do veículo)
                        if "datahora" in df.columns:
                            df_ultimo = df.sort_values("datahora").groupby("ordem", as_index=False).last()
                        else:
                            df_ultimo = df.groupby("ordem", as_index=False).last()

                        payload = df_para_geojson(df_ultimo)
                        payload["atualizado_em"] = ts

                        yield f"data: {json.dumps(payload)}\n\n"

                await asyncio.sleep(5)
            except asyncio.CancelledError:
                break
            except Exception as e:
                await asyncio.sleep(5)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )


@app.get("/api/status", summary="Health check")
async def api_status():
    meta = get_snapshot()
    return {
        "status": "ok",
        "registros_operacionais": len(meta["df"]),
        "atualizado_em": meta["atualizado_em"],
        "silver_disponivel": SILVER_PARQUET.exists() or SILVER_CSV.exists(),
    }