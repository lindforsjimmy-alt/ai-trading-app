import yfinance as yf
import os
import requests

os.makedirs("stock_data", exist_ok=True)

# ✅ AKTIER (Avanza-kompatibla)
tickers = []
with open("stock_data/global_tickers.txt") as f:
    for line in f:
        t = line.strip()
        if t:
            tickers.append(t)

# ✅ CRYPTO (Safello – riktig mapping)
crypto_map = {
    "BTC-USD": "Bitcoin",
    "ETH-USD": "Ethereum"
}

# ✅ HÄMTA CRYPTO LIVE
def get_crypto_list():
    try:
        url = "https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd"
        data = requests.get(url, timeout=10).json()

        coins = []
        for c in data[:20]:
            symbol = c["symbol"].upper() + "-USD"
            if symbol in crypto_map:
                coins.append(symbol)

        # fallback om API returnerar tomt
        return list(set(coins)) if coins else list(crypto_map.keys())

    except:
        print("⚠️ API fel – använder fallback")
        return list(crypto_map.keys())

crypto = get_crypto_list()

assets = []

# ✅ HÄMTA PRIS + GROWTH
def get_data(t):
    try:
        s = yf.Ticker(t)
        hist = s.history(period="6mo")

        if hist.empty:
            return None

        old = hist["Close"].iloc[0]
        new = hist["Close"].iloc[-1]

        if old == 0:
            return None

        growth = (new - old) / old

        return (t, round(new, 2), round(growth, 3))

    except:
        return None


# ✅ AKTIER
for t in tickers:
    d = get_data(t)
    if d:
        # (ticker, namn, pris, growth, plattform)
        assets.append((t, t, d[1], d[2], "AVANZA"))

# ✅ KRYPTO
for t in crypto:
    d = get_data(t)
    if d:
        assets.append((t, crypto_map.get(t, t), d[1], d[2], "SAFELLO"))

# ✅ SORTERA (HIGH → LOW)
assets = sorted(assets, key=lambda x: x[3], reverse=True)

# ✅ SPLIT TOP 15
stocks = [x for x in assets if x[4] == "AVANZA"][:15]
cryptos = [x for x in assets if x[4] == "SAFELLO"][:15]

# ✅ SPARA (VIKTIG STRUKTUR)
with open("stock_data/stocks.txt", "w") as f:

    f.write("=== STOCKS ===\n")
    for t, name, p, g, plat in stocks:
        f.write(f"{t}|{name}|{p}|0|{g}|{plat}\n")

    f.write("\n=== CRYPTO ===\n")
    for t, name, p, g, plat in cryptos:
        f.write(f"{t}|{name}|{p}|0|{g}|{plat}\n")

print("✅ Avanza + Safello data uppdaterad")
