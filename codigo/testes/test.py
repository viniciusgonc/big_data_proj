import requests
from app.services.processor import processar_snapshot

url = "https://dados.mobilidade.rio/gps/sppo"
raw = requests.get(url).json()

df = processar_snapshot(raw)

print(df.head())
print(len(df))
