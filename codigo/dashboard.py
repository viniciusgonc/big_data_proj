import streamlit as st
import pandas as pd
import folium
from streamlit_folium import st_folium

st.set_page_config(page_title="Dashboard Mobilidade RJ", layout="wide")
st.title("Analise de Mobilidade Urbana - Onibus RJ")
st.markdown("Dashboard interativo construido com os dados da Camada Gold do projeto de Big Data.")

@st.cache_data
def load_data():
    return pd.read_parquet('dados/sppo_amostra_gold.parquet')

try:
    df = load_data()

    st.sidebar.header("Filtros de Pesquisa")
    linhas_disponiveis = df['linha'].dropna().unique().tolist()
    
    linha_selecionada = st.sidebar.multiselect(
        "Selecione as Linhas de Onibus:",
        options=linhas_disponiveis,
        default=linhas_disponiveis[:3] 
    )

    if linha_selecionada:
        df_filtrado = df[df['linha'].isin(linha_selecionada)]
    else:
        df_filtrado = df

    st.subheader("Metricas em Tempo Real")
    col1, col2, col3 = st.columns(3)
    col1.metric("Total de Onibus Ativos", df_filtrado['ordem'].nunique())
    col2.metric("Velocidade Media (km/h)", round(df_filtrado['velocidade'].mean(), 1))
    col3.metric("Registros de GPS", len(df_filtrado))

    st.subheader("Mapa de Posicao e Lentidao")
    st.markdown("Os pontos em vermelho indicam veiculos com velocidade abaixo de 15 km/h (lentidao). Pontos em verde indicam fluxo normal.")
    
    mapa = folium.Map(location=[-22.9068, -43.1729], zoom_start=11)

    for _, row in df_filtrado.head(500).iterrows():
        cor = 'red' if row['velocidade'] < 15 else 'green' 
        
        folium.CircleMarker(
            location=[row['latitude'], row['longitude']],
            radius=4,
            color=cor,
            fill=True,
            popup=f"Linha: {row['linha']} | Velocidade: {row['velocidade']} km/h"
        ).add_to(mapa)

    st_folium(mapa, width=1000, height=500)

except FileNotFoundError:
    st.error("Erro: O arquivo 'sppo_amostra_gold.parquet' nao foi encontrado na pasta 'dados/'. Verifique se o notebook da Camada Gold foi executado corretamente.")
except Exception as e:
    st.error(f"Ocorreu um erro ao carregar o painel: {e}")