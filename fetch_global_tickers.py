
import os

os.makedirs("stock_data", exist_ok=True)

# ✅ USA
us_tickers = [
    "AAPL","MSFT","NVDA","AMZN","GOOGL","META","TSLA",
    "JPM","BAC","V","MA","XOM","CVX","UNH","HD","PG",
    "KO","PEP","PFE","MRK","AMD","INTC","CSCO","ORCL",
    "ADBE","CRM","PYPL","NFLX","SQ","UBER","PLTR"
]

# ✅ EUROPA
eu_tickers = [
    "NESN.SW","NOVN.SW","ASML.AS","SAP.DE","SIE.DE","AIR.PA",
    "OR.PA","MC.PA","TTE.PA","BNP.PA","SAN.PA","VOW.DE"
]

# ✅ SVERIGE
se_tickers = [
    "VOLV-B.ST","ABB.ST","ERIC-B.ST","SAND.ST","SKF-B.ST",
    "SSAB-A.ST","SEB-A.ST","SHB-A.ST","SWED-A.ST",
    "ATCO-A.ST","ATCO-B.ST","ALFA.ST","NDA-SE.ST"
]

# ✅ KOMBINERA
all_tickers = list(set(us_tickers + eu_tickers + se_tickers))

# ✅ SPARA
with open("stock_data/global_tickers.txt", "w", encoding="utf-8") as f:
    for t in all_tickers:
        f.write(t + "\n")

print(f"✅ Sparade {len(all_tickers)} globala tickers")
