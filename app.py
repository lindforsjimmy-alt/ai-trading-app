
    # ===== IMPORTS =====
from flask import Flask, redirect, session, request, render_template
import os, requests, time, feedparser, math, hashlib
from dotenv import load_dotenv
load_dotenv("api.env")
from datetime import timedelta
import finnhub
import pandas as pd
import threading
from main import signal
from sp500_list import SP500_SYMBOLS

    # ===== CONFIG / APP SETUP =====
BASE_URL = os.environ.get("BASE_URL", "http://localhost:10000")

finnhub_client = finnhub.Client(api_key=os.environ.get("FINNHUB_API_KEY"))
print("FINNHUB KEY:", os.environ.get("FINNHUB_API_KEY"))

app = Flask(__name__)
app.permanent_session_lifetime = timedelta(hours=12)
app.secret_key = "super_secret_trading_key_123"

# ===== DATA FILES & BASIC =====

# ===== FILES =====
DATA_FILE = "stock_data/my_trades.txt"
USERS_FILE = "stock_data/users.txt"

os.makedirs("stock_data", exist_ok=True)
open(DATA_FILE, "a").close()
open(USERS_FILE, "a").close()

# ===== HASH =====

def hash_password(p):
    return hashlib.sha256(p.encode()).hexdigest()

def create_user(email, password_hash):
    with open(USERS_FILE, "a") as f:
        f.write(f"{email}|{password_hash}\n")

def check_user(email, password):
    with open(USERS_FILE) as f:
        for l in f:
            e, p = l.strip().split("|")
            if e == email and p == hash_password(password):
                    return True
        return False

def user_exists(email):
    with open(USERS_FILE) as f:
        for l in f:
            if l.split("|")[0] == email:
                return True
    return False

# ===== CACHE =====
def get_finnhub_usage():
    return finnhub_calls.get("count", 0), FINNHUB_LIMIT

finnhub_calls = {
    "count": 0,
    "last_reset": time.time()
}

FINNHUB_LIMIT = 60  # per minute

market_data_cache = {}
ai_results_cache = {}

def safe_fetch(fn, retries=3):
    for _ in range(retries):
        try:
            data = fn()
            if data is not None:
                return data
        
        except Exception as e:
            print("SAFE_FETCH ERROR:", e)
        time.sleep(2)
    return []

price_cache = {}
CACHE_TIME = 60

market_cache = {}
MARKET_CACHE_TIME = 120

news_cache = {}
NEWS_CACHE_TIME = 3600

ai_cache = {
"last_run": 0,
"data": []
}

alert_cache = {}

AI_REFRESH_TIME = 86400  # 24 timmar (sekunder)

# ===== MARKET =====
def get_price_finnhub(symbol):
    global finnhub_calls

    now = time.time()

    # reset varje minut
    if now - finnhub_calls["last_reset"] > 60:
        finnhub_calls["count"] = 0
        finnhub_calls["last_reset"] = now

    finnhub_calls["count"] += 1

    print(f"📊 Finnhub calls: {finnhub_calls['count']}/{FINNHUB_LIMIT}")

    if finnhub_calls["count"] >= FINNHUB_LIMIT - 2:
        print("⚠️ Rate limit – skipping")
        return None

    try:
        data = finnhub_client.quote(symbol)

        price = data.get("c")
        volume = data.get("v")

        if price and price > 0:
            return {
                "price": price,
                "volume": volume or 0
            }

    except:
        return None

    return None


def get_price_yahoo(symbol):
    try:
        url = f"https://query1.finance.yahoo.com/v7/finance/quote?symbols={symbol}"

        headers = {
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json",
            "Referer": "https://finance.yahoo.com/"
        }

        r = requests.get(url, headers=headers, timeout=5)

        if r.status_code != 200:
            return None

        data = r.json()
        res = data.get("quoteResponse", {}).get("result", [])

        if res:
            return res[0].get("regularMarketPrice")

    except:
        return None

    return None


# ✅ CENTRAL PRIS-FUNKTION (VIKTIG!)
def get_price(symbol):

    data = get_price_finnhub(symbol)

    if data:
        return data

    price = get_price_yahoo(symbol)

    if price:
        return {
            "price": price,
            "volume": 0
        }

    return None

#INDENT (0)
def get_sp500_symbols():
    print("✅ Loaded S&P500 (local)")
    return SP500_SYMBOLS

def get_global_stock_universe():
    base = get_sp500_symbols()

    extra = [
        "SHOP","PLTR","RIVN","COIN","SQ","PYPL",
        "BABA","NIO","XPEV","LI","TSM",
        "SAP","ASML","NOVO-B.CO","VOLV-B.CO"
    ]

    symbols = list(set(base + extra))

    return symbols[:2000]

# ✅ AKTIER (Finnhub + Yahoo fallback)
def get_stock_assets(symbols):

    assets = []

    for sym in symbols:

        price = get_price(sym)

        if not price:
            continue

        # ✅ FIX
        if isinstance(price, dict):
            p = price.get("price", 0)
        else:
            p = price

        if not p or p <= 0:
            continue

        assets.append({
            "t": sym,
            "name": sym,
            "price": p,
            "currency": "USD",
            "type": "stock"
        })

    return assets

# ✅ CRYPTO (CoinGecko)
def get_crypto_assets():
    assets = []

    for page in range(1, 5):
        try:
            data = requests.get(
                f"https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd&per_page=250&page={page}"
            ).json()

            for c in data:
                
                assets.append({
                    "t": c["symbol"].upper(),
                    "name": c["name"],
                    "price": c["current_price"],
                    "volume": c.get("total_volume", 0),
                    "currency": "USD",
                    "type": "crypto"
                })
                
        except:
            continue

    return assets

# ✅ HUVUDFUNKTION (ERSÄTTER DIN GAMLA)
def get_market_assets():

    now = time.time()

    if "market" in market_cache:
        data, t = market_cache["market"]
        if data and now - t < MARKET_CACHE_TIME:
            return data

    print("🔄 Fetching market data...")

    # ✅ SYMBOLER
    symbols = get_sp500_symbols()

    # 🔥 BONUS: inkludera AI toppval
    symbols += [s["t"] for s in ai_cache.get("data", [])[:50]]

    symbols = list(set(symbols))
    symbols = symbols[:80]  

    # ✅ HÄMTA DATA
    stock_assets = get_stock_assets(symbols)
    crypto_assets = get_crypto_assets()

    assets = stock_assets + crypto_assets

    # ✅ fallback om allt failar
    if len(assets) == 0:
        print("⚠️ TOTAL FAIL – fallback aktier")

        fallback = ["AAPL", "MSFT", "NVDA", "TSLA", "AMZN"]

        for sym in fallback:
            assets.append({
                "t": sym,
                "name": sym,
                "price": 100,
                "currency": "USD"
            })

    print("STOCKS:", len(stock_assets))
    print("CRYPTO:", len(crypto_assets))
    print("TOTAL:", len(assets))

    market_cache["market"] = (assets, now)

    return assets

def scan_market_background():
    symbols = get_global_stock_universe()

    BATCH = 60
    all_assets = []
    skipped_count = 0

    for i in range(0, len(symbols), BATCH):

        batch = symbols[i:i+BATCH]

        for sym in batch:

            data = get_price(sym)

            if not data or not isinstance(data, dict):
                print("SKIPPED (no data):", sym, data)
                skipped_count += 1
                continue

            price = data.get("price")
            volume = data.get("volume", 0)

            if not isinstance(price, (int, float)) or price <= 0:
                print("SKIPPED (bad price):", sym, price)
                skipped_count += 1
                continue

            
            asset = {
                "t": sym,
                "price": price,
                "volume": volume,
                "currency": "USD",
                "type": "stock"
            }


            all_assets.append(asset)

        print(f"✅ Batch {i} klar | Finnhub calls: {finnhub_calls['count']} | Skipped: {skipped_count}")

        time.sleep(60)

    crypto_assets = get_crypto_assets()
    all_assets += crypto_assets

    market_data_cache["data"] = all_assets
    
# ===== HISTORICAL DATA =====
def get_historical_data(symbol, period):

    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range={period}&interval=1d"
    headers = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json"
    }

    try:
        r = requests.get(url, headers=headers, timeout=5)

        if r.status_code != 200:
            return None

        return r.json()

    except Exception as e:
        print("HIST ERROR:", e)
        return None

# ===== AI BUILDING BLOCKS =====
# ===== RSI =====
def calculate_rsi(prices, period=14):

    if not prices or len(prices) < period:
        return 50

    gains = []
    losses = []

    for i in range(1, period):
        change = prices[i] - prices[i - 1]

        if change > 0:
            gains.append(change)
        else:
            losses.append(abs(change))

    avg_gain = sum(gains) / period if gains else 0
    avg_loss = sum(losses) / period if losses else 0

    if avg_loss == 0:
        return 100

    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))

    return rsi


def get_rsi_score_from_history(prices):

    rsi = calculate_rsi(prices)

    if rsi < 30:
        return 2
    elif rsi < 45:
        return 1
    elif rsi > 70:
        return -2
    elif rsi > 60:
        return -1

    return 0


# ===== MOVING AVERAGES =====
def calculate_ma(prices, period):

    if not prices or len(prices) < period:
        return None

    return sum(prices[-period:]) / period


def get_ma_score(prices):

    ma50 = calculate_ma(prices, 50)
    ma200 = calculate_ma(prices, 200)

    if not ma50 or not ma200:
        return 0

    if ma50 > ma200:
        return 2
    elif ma50 < ma200:
        return -2

    return 0

# ===== STOP LOSS =====
def get_stop_loss(price, risk):
    if risk == "low":
        return round(price * 0.95, 2)
    elif risk == "high":
        return round(price * 0.85, 2)
    return round(price * 0.90, 2)

# ===== AI =====
def get_news_score(t):

    now = time.time()

    if t in news_cache:
        score, ts = news_cache[t]
        if now - ts < NEWS_CACHE_TIME:
            return score

    try:
        feed = feedparser.parse(f"https://news.google.com/rss/search?q={t}")

        if not hasattr(feed, "entries"):
            return 0

        score = 0

        for e in feed.entries[:3]:
            txt = e.title.lower()

            if "up" in txt or "growth" in txt:
                score += 1
            if "down" in txt or "drop" in txt:
                score -= 1

        news_cache[t] = (score, now)
        return score

    except:
        return 0

def get_signal(price):

    if price < 20:
        return "KÖP"
    elif price < 100:
        return "AVVAKTA KÖP"
    elif price > 500:
        return "SÄLJ"
    
    return "AVVAKTA"

def is_tradeable(s):

    price = s.get("price", 0)
    volume = s.get("volume", 0)
    symbol = s.get("t")

    # ✅ FIX: hantera om price råkar vara dict
    if isinstance(price, dict):
        price = price.get("price", 0)

    # ✅ STOCK LOGIK
    asset_type = s.get("type")

    # ✅ STOCK LOGIK
    if asset_type == "stock":
        if price < 5:
            return False

        if volume < 1_000_000:
            return False

    # ✅ CRYPTO LOGIK
    elif asset_type == "crypto":
        if price < 0.1:
            return False

        if volume < 5_000_000:
            return False

    return True

def get_score(sig, price, t):
    base = 80 if sig == "KÖP" else 60 if sig == "AVVAKTA KÖP" else 30

    val = base \
        + (get_news_score(t) * 3) \
        

    return max(0, min(100, int(val)))

def get_reason(sig, price, t, s=None):

    reasons = []

    # ✅ signal
    if sig == "KÖP":
        reasons.append("📈 Momentum indikerar köpläge")
    elif sig == "SÄLJ":
        reasons.append("📉 Övervärderad / svag trend")
    else:
        reasons.append("➖ Neutral trend")

    # ✅ prisnivå
    if price < 20:
        reasons.append("💸 Låg prisnivå (hög potential)")
    elif price > 200:
        reasons.append("💰 Hög prisnivå")

    # ✅ news
    news_score = get_news_score(t)
    if news_score > 0:
        reasons.append("📰 Positivt nyhetsflöde")
    elif news_score < 0:
        reasons.append("📰 Negativt nyhetsflöde")

    # ✅ volym
    if s:
        vol = s.get("volume", 0)

        if vol > 10_000_000:
            reasons.append("💧 Hög likviditet")
        elif vol < 1_000_000:
            reasons.append("⚠️ Låg likviditet")

    # ✅ crypto vs aktie
    if len(t) > 5:
        reasons.append("⚠️ Crypto – hög volatilitet")

    return "\n".join(reasons)

def get_summary(s):

    sig = s.get("signal", "")
    score = s.get("score", 0)
    symbol = s.get("t", "")
    volume = s.get("volume", 0)

    # ✅ bas
    if sig == "KÖP":
        txt = "📈 Stark momentum‑driven möjlighet"
    elif sig == "SÄLJ":
        txt = "📉 Svag trend eller övervärderad"
    else:
        txt = "➖ Neutral marknadssignal"

    # ✅ förstärk med score
    if score > 85:
        txt += " med hög AI‑confidence"
    elif score > 70:
        txt += " med god potential"

    # ✅ news
    news = get_news_score(symbol)
    if news > 0:
        txt += " och positivt nyhetsflöde"
    elif news < 0:
        txt += " med negativt nyhetsflöde"

    # ✅ volym
    if volume > 10_000_000:
        txt += " samt hög likviditet"

    # ✅ crypto flag
    if s.get("type") == "crypto":
        txt += " (crypto – hög volatilitet)"

    return txt

# ===== TREND FROM HISTORY =====
def get_trend_score_from_history(prices):

    if not prices or len(prices) < 10:
        return 0

    start = prices[0]
    end = prices[-1]

    change_pct = (end - start) / start * 100

    if change_pct > 10:
        return 2
    elif change_pct > 3:
        return 1
    elif change_pct < -10:
        return -2
    elif change_pct < -3:
        return -1

    return 0

# ===== AI ENGINE =====
def get_usd_sek():
    try:
        r = requests.get("https://open.er-api.com/v6/latest/USD", timeout=5)
        data = r.json()
        return data["rates"]["SEK"]
    except:
        return 10.5

# ===== AI DAILY SCAN =====
def run_daily_ai(strategy="short", risk="medium", capital=10000):

    now = time.time()

    # ✅ cache
    if ai_cache["data"] and now - ai_cache["last_run"] < AI_REFRESH_TIME:
        return ai_cache["data"]

    print("🔄 Running AI daily scan...")

    result = []

    assets = market_data_cache.get("data") or safe_fetch(get_market_assets)

    if not assets:
        print("⚠️ No cache – using fallback market fetch")
        assets = safe_fetch(get_market_assets)

    for s in assets:
        
        if "type" not in s:
            s["type"] = "stock"

        if not is_tradeable(s):
            continue

        price = s.get("price", 0)

        if isinstance(price, dict):
            price = price.get("price", 0)

        sig = get_signal(price)
       
        if isinstance(price, dict):
            price = price.get("price", 0)

        hist = None

        prices = []

        if hist:
            try:
                prices = hist["chart"]["result"][0]["indicators"]["quote"][0]["close"]
                prices = [p for p in prices if p]
            except:
                prices = []

        # ✅ indikatorer (MÅSTE KOMMA FÖRST)
        trend_score = get_trend_score_from_history(prices)
        rsi_score = get_rsi_score_from_history(prices)
        news_score = get_news_score(s["t"])
        ma_score = get_ma_score(prices)

        # ✅ Justera vikter baserat på strategi

        if strategy == "short":
            trend_weight = 5
            rsi_weight = 6
            ma_weight = 4
            news_weight = 3

        elif strategy == "long":
            trend_weight = 6
            rsi_weight = 2
            ma_weight = 6
            news_weight = 2

        else:
            trend_weight = 5
            rsi_weight = 5
            ma_weight = 4
            news_weight = 3

        # ✅ base score
        base = 80 if sig == "KÖP" else 60 if sig == "AVVAKTA KÖP" else 30

        total_score = (
            base
            + (trend_score * trend_weight)
            + (rsi_score * rsi_weight)
            + (news_score * news_weight)
            + (ma_score * ma_weight)

        )

        if s.get("volume", 0) > 5_000_000:
            total_score += 3

        # ✅ kapital-filter

        if capital < 15000 and price > 200:
            total_score -= 5

        if capital > 30000 and price < 10:
            total_score -= 3

        # ✅ FILTER beroende på strategi

        if strategy == "short":

            if trend_score < 0:
                total_score -= 4

            if rsi_score < 0:
                total_score -= 5

        elif strategy == "long":

            if trend_score < 0:
                continue  # strikt

            if ma_score < 0:
                continue  # viktigt för långsiktigt


        # ===== FILTER 1: lång trend =====
        long_trend = get_trend_score_from_history(prices)

        if long_trend < 0:
            total_score -= 5


        # ===== FILTER 2: billiga tillgångar =====
        
        # crypto filter
        if s.get("currency") == "USD" and price < 0.1:
            total_score -= 10

        # aktie filter (enkelt)
        if price < 5:
            total_score -= 3


        # ===== FILTER 3: momentum krav =====
        if trend_score <= 0 and ma_score <= 0:
            total_score -= 5

        s["signal"] = sig
        s["score"] = max(0, min(100, int(total_score)))
        s["reason"] = get_reason(sig, price, s["t"], s)
        s["summary"] = get_summary(s)

        # ✅ AI confidence (0–100%)
        s["trigger_score"], s["trigger_reasons"] = get_trigger_score(s)
       
        confidence = (
            s.get("trigger_score", 0) * 20 +
            (s.get("score", 0) / 10)
        )

        s["confidence"] = min(100, int(confidence))


        # ✅ trigger
        s["trigger_score"], s["trigger_reasons"] = get_trigger_score(s)

        result.append(s)

    result = sorted(
        result,
        key=lambda x: (x.get("trigger_score", 0), x.get("score", 0)),
        reverse=True
    )

    result = [s for s in result if s["price"] > 0]

    print("---- DEBUG TOP ASSETS ----")
    for s in result[:15]:
        print(s["t"], "| price:", s.get("price"), "| score:", s.get("score"), "| signal:", s.get("signal"))
    print("--------------------------")

    # ✅ cache
    ai_cache["data"] = result
    ai_cache["last_run"] = now
    ai_results_cache["data"] = result
    return result


# ===== AI DASHBOARD ANALYSIS =====
def dashboard_analysis(s):

    return {
        "Affär": "Starkt bolag inom sektor",
        "Tillväxt": "Moderat tillväxt",
        "Lönsamhet": "Stabil",
        "Risk": "Medel",
        "Värdering": "Neutral",
        "Timing": s["signal"],
        "Marknad": "Växande sektor",
        "Investeringsidé": "Momentum + AI-score",
        "Risknivå": "Medium",
        "Beslut": s["signal"]
    }

# ===== TRIGGER SCORE =====
def get_trigger_score(s):

    score = 0
    reasons = []

    news = get_news_score(s["t"])

    if news > 1:
        score += 2
        reasons.append("Nyheter")

    if s["score"] > 70:
        score += 2
        reasons.append("Stark AI-score")

    if s["signal"] == "KÖP":
        score += 1
        reasons.append("Momentum")

    return score, reasons

# ===== PORTFOLIO AI =====
# ===== PORTFOLIO ANALYSIS =====
def portfolio_analysis(decision, pl_pct):

    return {
        "Fundamenta": "Oförändrat",
        "Hypotes": "Stämmer",
        "Risk": "Medium",
        "Kurs": f"{round(pl_pct, 2)}%",
        "Värdering": "Neutral",
        "Alternativ": "Finns bättre case",
        "Position": "Normal",
        "Sälj": "Vid target",
        "Köp mer": "Vid dip",
        "Tidsram": "Medel"
    }

# ===== PORTFOLIO AI =====
def portfolio_ai_decision(pl_pct, current_price, start_price, t, risk, strategy):

    news = get_news_score(t)
    trend = 0
    
    # ===== Strategy =====

    if strategy == "short":
        take_profit = 10
        stop_loss = -6

    elif strategy == "long":
        take_profit = 20
        stop_loss = -12
    
    else:
        take_profit = 10
        stop_loss = -6

    if pl_pct >= take_profit:
        if news > 0 and trend > 0:
            return "Avvakta", "Strong trend continues"
        return "SÄLJ", "Take profit reached"

    if pl_pct <= stop_loss:
        return "SÄLJ", "Stop-loss triggered"

    if news < -1:
        return "SÄLJ", "Negative news"

    if trend < -1:
        return "SÄLJ", "Weak trend"

    if pl_pct > 0 and news > 1 and trend > 0:
        return "KÖP MER", "Strong trend + positive news"

    if -5 < pl_pct < 5 and news > 0:
        return "KÖP MER", "Possible recovery"

    return "Avvakta", "No strong signal"

# ===== DATA (portfolio & trades) =====
# ===== PORTFOLIO DATA =====

def portfolio(user):
    data = {}

    with open(DATA_FILE) as f:
        for l in f:
            parts = l.strip().split("|")
            if len(parts) < 4:
                continue

            u, t, q, p = parts

            if u != user:
                continue

            q = int(float(q))
            p = float(p)

            if t not in data:
                data[t] = {
                    "qty": 0,
                    "total_cost": 0
                }

            data[t]["qty"] += q
            data[t]["total_cost"] += q * p

    result = []
    
    for t, d in data.items():
        if d["qty"] <= 0:
            continue

        avg_price = round(d["total_cost"] / d["qty"], 2) if d["qty"] else 0

        result.append({
            "t": t,
            "qty": d["qty"],
            "avg_price": avg_price
        })

    return result

# ===== TRADE =====
def buy(user,t,qty,price):
    with open(DATA_FILE,"a") as f:
        f.write(f"{user}|{t}|{qty}|{price}\n")

def sell(user,t,qty):
    lines=open(DATA_FILE).readlines()
    new=[]
    for l in lines:
        u,ticker,q,p=l.strip().split("|")
        q=int(float(q))

        if u==user and ticker==t:
            if qty>=q:
                qty-=q
                if qty <= 0:
                    continue
            else:
                new.append(f"{u}|{ticker}|{q-qty}|{p}\n")
                qty=0
        else:
            new.append(l)

    open(DATA_FILE,"w").writelines(new)

# ===== AUTH (all user) =====
# ===== APPROVAL SYSTEM =====
PENDING_FILE = "stock_data/pending.txt"
open(PENDING_FILE, "a").close()

import smtplib
from email.mime.text import MIMEText

@app.route("/approve")
def approve():

    email = request.args.get("email")

    lines = open(PENDING_FILE).readlines()
    new = []

    for l in lines:
        e, p = l.strip().split("|")

        if e == email:
            create_user(e, p)
        else:
            new.append(l)

    open(PENDING_FILE, "w").writelines(new)

    return "✅ User approved!"

# ===== LOGIN =====
@app.route("/login", methods=["GET", "POST"])
def login():

    msg = ""

    if request.method == "POST":

        # ===== LOGIN =====
        if "login" in request.form:
            email = request.form.get("email")
            password = request.form.get("password")

            if check_user(email, password):
                session["user"] = email
                session.permanent = True
                return redirect("/dashboard")
            else:
                msg = "Fel login"

        # ===== REGISTER (PENDING) =====
        elif "register" in request.form:
            email = request.form.get("email")
            password = request.form.get("password")

            if not user_exists(email):
                hashed = hash_password(password)

                with open(PENDING_FILE, "a") as f:
                    f.write(f"{email}|{hashed}\n")

                send_approval_email(email)

                msg = "✅ Förfrågan skickad för godkännande"
            else:
                msg = "Användare finns redan"

    return f"""
    <html>
    <head>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>

    
    <script>
    function togglePopup(btn) {{
        let popup = btn.nextElementSibling;

        // växla visa/dölj
        if (popup.style.display === "block") {{
            popup.style.display = "none";
        }} else {{
            popup.style.display = "block";
        }}
    }}

    // stäng när man klickar utanför
    document.addEventListener("click", function(e) {{
        document.querySelectorAll(".ai-popup").forEach(p => {{
            if (!p.contains(e.target) && !p.previousElementSibling.contains(e.target)) {{
                p.style.display = "none";
            }}
        }});
    }});
    </script>

    body {{
        font-family: Arial;
        padding: 20px;
    }}


    input {{
        width: 100%;
        padding: 12px;
        margin-top: 5px;
    }}

    button {{
        width: auto;
        padding: 8px;
        margin-top: 5px;
    }}


    .box {{
        max-width: 400px;
        margin: auto;
    }}
    </style>
    </head>

    <body>

    <div class="box">

    <h2>Login</h2>

    <form method="post">
    Email:<br>
    <input type="email" name="email" required>

    Lösenord:<br>
    <input type="password" name="password" required><br><br>

    <button type="submit" name="login">Login</button>
    </form>

    <hr>

    <h3>Create account</h3>

    <form method="post">
    Email:<br>
    <input type="email" name="email" required>

    Lösenord:<br>
    <input type="password" name="password" required><br><br>

    <button type="submit" name="register">Register</button>
    </form>

    <br>
    <a href="/forgot">Forgot password</a>

    <p>{msg}</p>

    </div>

    </body>
    </html>
    """
# ===== LOGOUT =====
@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")

# ✅ ===== CHANGE PASSWORD =====
@app.route("/change_password", methods=["GET", "POST"])
def change_password():

    msg = ""

    user = session.get("user")
    if not user:
        return redirect("/login")

    if request.method == "POST":
        new_password = request.form.get("new_password")

        if new_password:

            new_hash = hash_password(new_password)

            lines = open(USERS_FILE).readlines()
            new_lines = []

            for l in lines:
                email, pwd = l.strip().split("|")

                if email == user:
                    new_lines.append(f"{email}|{new_hash}\n")
                else:
                    new_lines.append(l)

            open(USERS_FILE, "w").writelines(new_lines)

            msg = "✅ Password updated successfully"

    return f"""
    <h2>Change Password</h2>

    <form method="post">
        New password:<br>
        <input type="password" name="new_password" required><br><br>

        <button>Update</button>
    </form>

    <p>{msg}</p>

    <a href="/dashboard">Back</a>
    """

# ===== FORGOT PASSWORD =====
@app.route("/forgot", methods=["GET", "POST"])
def forgot():

    msg = ""

    if request.method == "POST":
        email = request.form.get("email")
        send_reset_email(email)
        msg = f"✅ Mail skickat till {email}"

    return f"""
    <h2>Forgot Password</h2>

    <form method="post">
    Email:<br>
    <input name="email"><br><br>
    <button>Skicka</button>
    </form>

    <p>{msg}</p>

    <a href="/login">Tillbaka</a>
    """
# ===== EMAIL/SYSTEM =====
# ===== APPROVAL EMAIL =====

def send_approval_email(new_user_email):

    sender = os.environ.get("EMAIL_USER")
    password = os.environ.get("EMAIL_PASSWORD")

    if not sender or not password:
        print("⚠️ Email not configured")
        return

    approve_link = f"{BASE_URL}/approve?email={new_user_email}"
    reject_link  = f"http://localhost:10000/reject?email={new_user_email}"

    body = f"""
Ny användare vill registrera:

{new_user_email}

✅ Approve:
{approve_link}

❌ Reject:
{reject_link}
"""

    msg = MIMEText(body)
    msg["Subject"] = "Godkänn användare"
    msg["From"] = sender
    msg["To"] = "lindfors.jimmy@outlook.com"

    try:
        server = smtplib.SMTP("smtp.office365.com", 587)
        server.starttls()
        server.login(sender, password)
        server.send_message(msg)
        server.quit()
    except Exception as e:
        print("Approval mail error:", e)

# ===== ALERT FUNCTION =====
def send_alert(email, message):

    sender = os.environ.get("EMAIL_USER")
    password = os.environ.get("EMAIL_PASSWORD")

    if not sender or not password:
            print("⚠️ Email not configured")
            return

    msg = MIMEText(message)
    msg["Subject"] = "🚨 Trading Alert"
    msg["From"] = sender
    msg["To"] = email

    try:
        msg["Subject"] = "🚨 Trading Alert"
        msg["From"] = sender
        msg["To"] = email

        server = smtplib.SMTP("smtp.office365.com", 587)
        server.starttls()
        server.login(sender, password)
        server.send_message(msg)
        server.quit()

        print("ALERT SENT:", message)

    except Exception as e:
        print("Alert error:", e)
        return

# ===== RESET EMAIL =====
def send_reset_email(email):

    sender = "YOUR_MAIL@outlook.com"
    password = "APP_PASSWORD"

    body = f"Password reset requested for: {email}"

    msg = MIMEText(body)
    msg["Subject"] = "Password Reset"
    msg["From"] = sender
    msg["To"] = email

    try:
        server = smtplib.SMTP("smtp.office365.com", 587)
        server.starttls()
        server.login(sender, password)
        server.send_message(msg)
        server.quit()
        print("RESET MAIL SENT TO:", email)
    except Exception as e:
        print("Reset mail error:", e)

# ===== UI HELPERS =====
def get_buy_link(t):
    if len(t) > 5:
        return f'<"https://safello.com/sv/kop/{t.lower()}" target="_blank">Safello</a>'
    else:
        return f'<"https://www.avanza.se/aktier/sok.html?query={t}" target="_blank">Avanza</a>'



def format_price(price):
    if price is None:
        return "-"

    try:
        price = float(price)

        # skapa lång decimal
        s = f"{price:.10f}"

        # ta bort trailing zeros
        s = s.rstrip("0")

        # säkerställ EN extra nolla
        if "." in s:
            if s.endswith("."):
                s += "0"
            else:
                s += "0"

        return s

    except:
        return str(price)

def get_display_name(s, ranked):

    match = next((x for x in ranked if x["t"] == s["t"]), None)

    if match:
        name = match.get("name") or match.get("shortName")

        if name:
            if f"({s['t']})" in name:
                return name
            else:
                return f"{name} ({s['t']})"

    try:
        r = requests.get(
            f"https://api.coingecko.com/api/v3/search?query={s['t']}",
            timeout=3
        ).json()

        coins = r.get("coins")
        if coins:
            name = coins[0]["name"]

            if f"({s['t']})" in name:
                return name
            else:
                return f"{name} ({s['t']})"

    except:
        pass

    return f"{s['t']} ({s['t']})"

# ===== DASHBOARD HELPERS =====
def render_asset(s, ranked, mode="dashboard"):

    name = get_display_name(s, ranked)
    buy_link = get_buy_link(s["t"])
    signal = s.get("forced_signal", s.get("signal", "AVVAKTA"))

    if signal == "KÖP":
        cls = "signal-buy"
    elif signal == "SÄLJ":
        cls = "signal-sell"
    else:
        cls = "signal-wait"

    raw_price = s.get("price")
    price = format_price(raw_price)
    currency = s.get("currency", "")

    avg_price = s.get("avg_price")

    # ✅ färglogik
    if mode == "portfolio":
        if raw_price is not None and avg_price:
            if raw_price > avg_price:
                price_class = "price-up"
            elif raw_price < avg_price:
                price_class = "price-down"
            else:
                price_class = "price-neutral"
        else:
            price_class = "price-neutral"
    else:
        if signal == "KÖP":
            price_class = "price-up"
        elif signal == "SÄLJ":
            price_class = "price-down"
        else:
            price_class = "price-neutral"

    # ✅ procent
    pct_text = ""
    if mode == "portfolio" and raw_price is not None and avg_price:
        try:
            pct = ((raw_price - avg_price) / avg_price) * 100
            if pct > 0:
                pct_text = f" (+{pct:.2f}%)"
            elif pct < 0:
                pct_text = f" ({pct:.2f}%)"
            else:
                pct_text = " (0.00%)"
        except:
            pct_text = ""

    # ✅ UI
    if mode == "portfolio":
        extra = f"""
        Antal: {s.get('qty','-')}<br>
        Snittpris: {s.get('avg_price','-')}<br>
        Pris: <span class="{price_class}">{price} {currency}{pct_text}</span><br>
        """

        actions = f"""
        <form method="post" style="display:inline;">
            <input name="sellqty_{s['t']}" style="width:60px;">
            <button name="sell_{s['t']}">Sälj</button>
        </form>
        """
    else:
        extra = f'Pris: <span class="{price_class}">{price} {currency}</span><br>'

        actions = f"""
        <form method="post" style="display:inline;">
            <input name="buyqty_{s['t']}" style="width:60px;">
            <button class="buy-btn" name="buy_{s['t']}">Köp</button>
        </form>
        """

    # ✅ ANALYS (FIXAD)
    analysis = f"""
AI: {signal}<br>
Score: {s.get('score', '-')}<br><br>

📊 {s.get("summary", "")}<br><br>

{s.get("reason", "").replace("\n", "<br>")}
"""

    # ✅ RIKTIG LÄNK (FIXAD)
    link = f'<https://news.google.com/search?q={s["t"]}" target="_blank">🔗 Läs mer / nyheter</a>'

    return f"""
    <div style="position:relative; margin-bottom:10px;">

    <b>{name} (Score {s.get('score','-')}) | {buy_link}</b>
    {extra}
    <span class="{cls}">AI: {signal}</span><br>

    <span class="ai-box">
        <button type="button" onclick="togglePopup(this)">AI Analys</button>

        <div class="ai-popup">

            <div style="text-align:right;">
                <button onclick="closePopup(this)" style="background:none;border:none;font-size:16px;">❌</button>
            </div>

            <b>AI Analys</b><br><br>

            {analysis}<br><br>

            {link}
        </div>
    </span>

    {actions}

    </div>
    <hr>
    """
@app.route("/dashboard", methods=["GET", "POST"])
def dashboard():

    user = session.get("user")
    if not user:
        return redirect("/login")

    if request.method == "POST":
        ranked_tmp = ai_results_cache.get("data") or run_daily_ai("short", "medium", 10000)

        for s in ranked_tmp:
            t = s["t"]

            if f"buy_{t}" in request.form:
                qty = request.form.get(f"buyqty_{t}")
                if qty and qty.isdigit():
                    buy(user, t, int(qty), s["price"])

            if f"sell_{t}" in request.form:
                qty = request.form.get(f"sellqty_{t}")
                if qty and qty.isdigit():
                    sell(user, t, int(qty))

    amount = int(request.form.get("amount") or 10000)
    ai_strategy = request.form.get("ai_strategy", "short")
    ai_risk = request.form.get("ai_risk", "medium")
    top_n = int(request.form.get("top_n") or 5)
    priority = request.form.get("priority", "mix")
    min_score = int(request.form.get("min_score") or 70)

    ranked = ai_results_cache.get("data")

    if not ranked:
        return "⏳ Laddar AI... vänta 10–30 sek och refresha sidan"

    # ✅ Top 5 global trades (bästa i världen)
    top_global = [
        s for s in ranked
        if s.get("score", 0) >= 75 and s.get("trigger_score", 0) >= 2
    ][:5]
    
    pf = portfolio(user)

    sell_list = []
    buy_more_list = []
    wait_list = []
    
    for s in pf:
        match = next((x for x in ranked if x["t"] == s["t"]), None)

        if match:
            current_price = match["price"]

            if isinstance(current_price, dict):
                current_price = current_price.get("price", s["avg_price"])
        else:
            current_price = s["avg_price"]

        pl_pct = (
            (current_price - s["avg_price"]) / s["avg_price"] * 100
            if s["avg_price"] else 0
        )

        decision, _ = portfolio_ai_decision(
            pl_pct,
            current_price,
            s["avg_price"],
            s["t"],
            ai_risk,
            ai_strategy
        )

        s["price"] = current_price

        if decision == "SÄLJ":
            sell_list.append(s)
        elif decision == "KÖP MER":
            buy_more_list.append(s)
        else:
            wait_list.append(s)
    
        # ✅ separera aktier och crypto
        stock_candidates = [
            x for x in ranked
            if x.get("type") != "crypto" and x.get("score", 0) >= min_score
        ]

        crypto_candidates = [
            x for x in ranked
            if x.get("type") == "crypto" and x.get("score", 0) >= min_score
        ]

        # ✅ crypto kräver trigger
        crypto_candidates = [
            x for x in crypto_candidates
            if x.get("trigger_score", 0) >= 2
        ]

        # ✅ PRIORITY LOGIK
        if priority == "stocks":
            stocks = stock_candidates[:top_n]

            if len(stocks) < top_n:
                stocks += [
                    x for x in ranked if x.get("type") != "crypto"
                ][:top_n - len(stocks)]

            crypto = crypto_candidates[:top_n]

        elif priority == "crypto":
            crypto = crypto_candidates[:top_n]

            if len(crypto) < top_n:
                crypto += [
                    x for x in ranked if x.get("type") == "crypto"
                ][:top_n - len(crypto)]

            stocks = stock_candidates[:top_n]

        else:  # mix
            stocks = stock_candidates[:top_n]

            if len(stocks) < top_n:
                stocks = [
                    x for x in ranked if x.get("type") != "crypto"
                ][:top_n]

            crypto = crypto_candidates[:top_n]

        wait = ranked[top_n:top_n*2]

    # ✅ usage stats
    usage = finnhub_calls.get("count", 0)
    limit = FINNHUB_LIMIT
    percent = int((usage / limit) * 100) if limit else 0

    return render_template(
        "dashboard.html",
        user=user,
        usage=usage,
        limit=limit,
        percent=percent,
        stocks=stocks,
        crypto=crypto,
        wait=wait,
        sell_list=sell_list,
        buy_more_list=buy_more_list,
        wait_list=wait_list,
        amount=amount,
        ai_strategy=ai_strategy,
        ai_risk=ai_risk,
        top_n=top_n,
        usd_sek=get_usd_sek(),
        top_global=top_global,
    )
    
# ===== PORTFOLIO =====

@app.route("/portfolio", methods=["GET", "POST"])
def portfolio_page():
    user = session.get("user")
    if not user:
        return redirect("/login")

    ranked = run_daily_ai("short", "medium", 10000)

    if request.method == "POST":
        for s in ranked:
            t = s["t"]

            if f"buy_{t}" in request.form:
                qty = request.form.get(f"buyqty_{t}")
                if qty and qty.isdigit():
                    buy(user, t, int(qty), s["price"])

            if f"sell_{t}" in request.form:
                qty = request.form.get(f"sellqty_{t}")
                if qty and qty.isdigit():
                    sell(user, t, int(qty))

    pf = portfolio(user)
    ranked = run_daily_ai("short", "medium", 10000)

    sell_list = []
    buy_more_list = []
    wait_list = []

    for s in pf:
        match = next((x for x in ranked if x["t"] == s["t"]), None)
        if match:
            s["name"] = match.get("name", s["t"])

        current_price = next(
            (x["price"] for x in ranked if x["t"] == s["t"]),
            s["avg_price"]
        )
        
        if isinstance(current_price, dict):
            current_price = current_price.get("price", s["avg_price"])

        pl_pct = (
            (current_price - s["avg_price"]) / s["avg_price"] * 100
            if s["avg_price"] else 0
        )

        decision, reason = portfolio_ai_decision(
            pl_pct,
            current_price,
            s["avg_price"],
            s["t"],
            "medium",
            "short"
        )

        s["price"] = current_price
        s["reason"] = reason
        s["signal"] = decision

        if decision == "SÄLJ":
            sell_list.append(s)
        elif decision == "KÖP MER":
            buy_more_list.append(s)
        else:
            wait_list.append(s)

    # ✅ Smart alerts (ingen spam)
    for s in sell_list:
        key = f"{user}_{s['t']}_SELL"
        if alert_cache.get(key) != "sent":
            send_alert(user, f"🚨 SÄLJ {s['t']} – du äger denna")
            alert_cache[key] = "sent"

    for s in buy_more_list:
        key = f"{user}_{s['t']}_BUYMORE"
        if alert_cache.get(key) != "sent":
            send_alert(user, f"📈 KÖP MER {s['t']} – stark trend")
            alert_cache[key] = "sent"
    
    sell_list = sorted(
        sell_list,
        key=lambda x: x.get("price", 0) - x.get("avg_price", 0)
    )

    buy_more_list = sorted(
        buy_more_list,
        key=lambda x: x.get("price", 0) - x.get("avg_price", 0),
        reverse=True
    )

    wait_list = sorted(
        wait_list,
        key=lambda x: x.get("price", 0) - x.get("avg_price", 0),
        reverse=True
    )

    
    return render_template(
        "portfolio.html",
        sell_list=sell_list,
        buy_more_list=buy_more_list,
        wait_list=wait_list,
        usd_sek=get_usd_sek(),
        user=user
    )


# ===== HOME =====

@app.route("/")
def home():
    return redirect("/login")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))

    # ✅ Auto refresh AI var 10 min
    def auto_refresh_ai():
        while True:
            print("🔄 Auto-refresh AI")
            safe_fetch(lambda: run_daily_ai("short", "medium", 10000))
            time.sleep(600)  # 600 sek = 10 min

    threading.Thread(target=auto_refresh_ai, daemon=True).start()

    # ✅ Kör AI i bakgrunden direkt vid start
    def preload_ai():
        print("🚀 Preloading AI...")
        safe_fetch(lambda: run_daily_ai("short", "medium", 10000))

    threading.Thread(target=preload_ai, daemon=True).start()

    # =====TEMPORARY DISABLED BACKGROUND SCAN =====#
    # t = threading.Thread(target=scan_market_background)
    # t.daemon = True
    # t.start()

    app.run(host="0.0.0.0", port=port)



