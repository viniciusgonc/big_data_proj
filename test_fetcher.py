from app.services.fetcher import fetch_raw_data
import time

print("Primeira chamada (API):")
data1 = fetch_raw_data()
print(len(data1))

print("\nSegunda Chamada (CACHE):")
data2 = fetch_raw_data()
print(len(data2))

time.sleep(31)

print("\nTerceira Chamada (API novamente):")
data3 = fetch_raw_data()
print(len(data3))