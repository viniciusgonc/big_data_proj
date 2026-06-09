"""
processor.py
------------
Lógica da camada Gold portada do AV2_gold.ipynb para funções puras.
Recebe o JSON bruto da API SPPO e devolve DataFrames já tratados,
com as mesmas regras de qualidade e categorização do notebook.
"""

import math
import re
import pandas as pd
import numpy as np

# ── Constantes (mesmas do notebook) ─────────────────────────────────────────

PATTERN_TECNICA = r"SN|MANUTENCAO|TREINO|VISTORIA|SP|FORA DE OP"


# ── Funções Gold ─────────────────────────────────────────────────────────────

def classificar_movimento(v: float) -> str:
    """Categorização semântica de velocidade — Gold notebook, célula 4."""
    if v == 0:
        return "Parado/Garagem"
    elif v <= 15:
        return "Lentidão/Trânsito"
    else:
        return "Fluído"


def is_linha_comercial(linha: str) -> bool:
    """Segmentação Comercial vs Operação Técnica — Gold notebook, célula 5."""
    return not bool(re.search(PATTERN_TECNICA, str(linha), re.IGNORECASE))


def processar_snapshot(raw_json: list[dict]) -> pd.DataFrame:
    """
    Recebe a lista de registros da API SPPO e aplica todo o pipeline Gold:
      1. Parse e tipagem
      2. Filtro de qualidade (coordenadas zeradas, velocidade nula)
      3. Deduplicação: mantém apenas o registro mais recente por veículo (ordem)
      4. Classificação de movimento
      5. Segmentação comercial/técnica

    Retorna df_operacao — equivalente ao DataFrame final do notebook.
    """
    if not raw_json:
        return pd.DataFrame()

    df = pd.DataFrame(raw_json)

    # ── 1. Tipagem ────────────────────────────────────────────────────────────
    for col in ["datahora", "datahoraenvio", "datahoraservidor"]:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], format='mixed', utc=True, errors="coerce")

    if "datahora" in df.columns:
        df["hora"] = df["datahora"].dt.hour
    else:
        df["hora"] = None

    if "linha" in df.columns:
        df["linha"] = df["linha"].astype(str).str.strip()

    for col in ["latitude", "longitude", "velocidade"]:
        if col in df.columns:
            df[col] = (
                df[col]
                .astype(str)
                .str.replace(",", ".", regex=False)
            )
            df[col] = pd.to_numeric(df[col], errors="coerce")

    # ── 2. Filtro de qualidade (Gold notebook, célula 3) ─────────────────────
    df = df[(df["latitude"] != 0) & (df["longitude"] != 0)]
    df = df.dropna(subset=["velocidade", "linha"])
    if "ordem" in df.columns:
        df = df.dropna(subset=["ordem"])

    # ── 3. Deduplicação por veículo — mantém apenas o registro mais recente ──
    # Garante uma única ocorrência por `ordem` (ID do veículo),
    # preservando o ponto com a `datahora` mais recente.
    if "ordem" in df.columns and "datahora" in df.columns:
        df = (
            df.sort_values("datahora", ascending=True, na_position="first")
              .drop_duplicates(subset=["ordem"], keep="last")
        )
    elif "ordem" in df.columns:
        df = df.drop_duplicates(subset=["ordem"], keep="last")

    # ── 4. Classificação de movimento (Gold notebook, célula 4) ──────────────
    df["status_movimento"] = df["velocidade"].apply(classificar_movimento)
    df["status_movimento"] = pd.Categorical(
        df["status_movimento"],
        categories=["Parado/Garagem", "Lentidão/Trânsito", "Fluído"],
    )

    # ── 5. Segmentação comercial (Gold notebook, célula 5) ───────────────────
    df["tipo_linha"] = np.where(
        df["linha"].str.contains(PATTERN_TECNICA, na=False, case=False),
        "tecnica",
        "comercial",
    )
    df_operacao = df[df["tipo_linha"] == "comercial"].copy()

    return df_operacao


# ── Funções de agregação para as rotas da API ────────────────────────────────

def snapshot_metricas(df: pd.DataFrame) -> dict:
    """Snapshot da Operação — Gold notebook, célula 6."""
    if df.empty:
        return {}

    onibus_ativos = int(df["ordem"].nunique()) if "ordem" in df.columns else 0
    linhas_ativas = int(df["linha"].nunique())
    vel_media = round(float(df["velocidade"].mean()), 2)
    pct_parados = round(
        float((df["status_movimento"] == "Parado/Garagem").mean() * 100), 2
    )

    return {
        "onibus_ativos": onibus_ativos,
        "linhas_ativas": linhas_ativas,
        "vel_media_kmh": vel_media,
        "pct_parados": pct_parados,
    }


def filtrar_por_linha(df: pd.DataFrame, linha: str) -> pd.DataFrame:
    """Retorna somente os veículos de uma linha específica."""
    return df[df["linha"].str.upper() == linha.upper()].copy()


def filtrar_proximos(df: pd.DataFrame, lat: float, lon: float, raio_m: float = 500) -> pd.DataFrame:
    """
    Retorna veículos dentro de `raio_m` metros da posição do usuário.
    Usa fórmula de Haversine para precisão real em coordenadas geográficas.
    """
    R = 6_371_000  # raio da Terra em metros

    lat_r = math.radians(lat)
    lon_r = math.radians(lon)

    def haversine(row):
        dlat = math.radians(row["latitude"]) - lat_r
        dlon = math.radians(row["longitude"]) - lon_r
        a = (
            math.sin(dlat / 2) ** 2
            + math.cos(lat_r)
            * math.cos(math.radians(row["latitude"]))
            * math.sin(dlon / 2) ** 2
        )
        return R * 2 * math.asin(math.sqrt(a))

    df = df.copy()
    df["distancia_m"] = df.apply(haversine, axis=1)
    return df[df["distancia_m"] <= raio_m].sort_values("distancia_m")


def gargalos_para_heatmap(df: pd.DataFrame, max_pontos: int = 10_000) -> list[list[float]]:
    """
    Ônibus em lentidão (1–15 km/h) para o heatmap — Gold notebook, célula 10.
    Retorna lista de [lat, lon] para o Leaflet.heat.
    """
    df_gargalos = df[(df["velocidade"] > 0) & (df["velocidade"] <= 15)]
    amostra = df_gargalos.sample(n=min(max_pontos, len(df_gargalos)))
    return amostra[["latitude", "longitude"]].values.tolist()


def df_para_geojson(df: pd.DataFrame) -> dict:
    """
    Converte o DataFrame tratado em GeoJSON FeatureCollection
    para consumo direto pelo Leaflet no frontend.
    Garante uma única feature por veículo (ordem) — deduplicação defensiva.
    """
    # Deduplicação defensiva: caso df ainda tenha duplicatas de `ordem`
    if "ordem" in df.columns:
        if "datahora" in df.columns:
            df = (
                df.sort_values("datahora", ascending=True, na_position="first")
                  .drop_duplicates(subset=["ordem"], keep="last")
            )
        else:
            df = df.drop_duplicates(subset=["ordem"], keep="last")

    features = []
    for _, row in df.iterrows():
        features.append({
            "type": "Feature",
            "geometry": {
                "type": "Point",
                "coordinates": [float(row["longitude"]), float(row["latitude"])],
            },
            "properties": {
                "linha": str(row.get("linha", "")),
                "ordem": str(row.get("ordem", "")),
                "velocidade": float(row.get("velocidade", 0)),
                "status": str(row.get("status_movimento", "")),
                "distancia_m": round(float(row["distancia_m"]), 0) if "distancia_m" in row else None,
            },
        })
    return {"type": "FeatureCollection", "features": features}