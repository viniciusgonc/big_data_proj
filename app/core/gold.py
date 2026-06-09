import pandas as pd
import numpy as np
import re

def classificar_movimento(velocidade):
    if velocidade == 0:
        return 'Parado/Garagem'
    elif velocidade <= 15:
        return 'Lentidão/Trânsito'
    else:
        return 'Flúido'
    
def is_linha_comercial(linha):
    return not bool(re.search(
        r'SN|MANUTENCAO|TREINO|VISTORIA|SP|FORA DE OP',
        str(linha)
    ))

def aplicar_regras_gold(df: pd.DataFrame) -> pd.DataFrame:

    df['status_movimento'] = df['velocidade'].apply(classificar_movimento)

    df['tipo_linha'] = df['linha'].apply(
        lambda x: 'comercial' if is_linha_comercial(x) else 'tecnica'
    )

    df = df[df['tipo_linha'] == 'comercial']

    return df

def gerar_metricas(df: pd.DataFrame) -> dict:
    total = df['ordem'].nunique()

    return {
        "total_onibus": total,
        "velocidade_media": float(df['velocidade'].mean()) if total > 0 else 0,
        "percentual_parados": float(
            (df['status_movimento'] == 'Parado/Garagem').mean() * 100
        ) if total > 0 else 0
    }