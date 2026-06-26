
from flask import Flask, redirect, session, request
import os, requests, time, feedparser, math, hashlib
from datetime import timedelta

BASE_URL = os.environ.get("BASE_URL", "http://localhost:10000")

app = Flask(__name__)
app.permanent_session_lifetime = timedelta(hours=12)
app.secret_key = "super_secret_trading_key_123"

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
def safe_fetch(fn, retries=3):
    for _ in range(retries):
        try:
            data = fn()
            if data:
                return data
        except:
            pass
        time.sleep(2)
    return []

price_cache = {}
CACHE_TIME = 60

market_cache = {}
MARKET_CACHE_TIME = 120

news_cache = {}
NEWS_CACHE_TIME = 300


ai_cache = {
    "last_run": 0,
    "data": []
}

AI_REFRESH_TIME = 86400  # 24 timmar (sekunder)


# ===== STOP LOSS =====
def get_stop_loss(price, risk):
    if risk == "low":
        return round(price * 0.95, 2)
    elif risk == "high":
        return round(price * 0.85, 2)
    return round(price * 0.90, 2)



# ===== MARKET =====

def get_market_assets():
    now = time.time()

    if "market" in market_cache:
        data, t = market_cache["market"]
        if data and now - t < MARKET_CACHE_TIME:
            return data

    assets = []

    symbols = [
        "AAPL","MSFT","GOOGL","AMZN","NVDA","META","TSLA",
        "INTC","AMD","CSCO","ORCL","IBM",
        "JPM","BAC","GS","C",
        "PG","KO","PEP","WMT",
        "UNH","JNJ","PFE",
        "ASML.AS","SAND.ST","ABB.ST","ERIC-B.ST",
        "SWED-A.ST","SSAB-A.ST","BNP.PA","TTE.PA"
    ]

    symbols_str = ",".join(symbols)

    url = f"https://query1.finance.yahoo.com/v7/finance/quote?symbols={symbols_str}"

    headers = {
        "User-Agent": "Mozilla/5.0"
    }

    try:
        r = requests.get(url, headers=headers, timeout=5).json()
        res = r["quoteResponse"]["result"]

        for item in res:
            price = item.get("regularMarketPrice")
            symbol = item.get("symbol")

            if price and not math.isnan(price):
                name = item.get("shortName") or symbol

                assets.append({
    		    "t": symbol,
    		    "name": name,
    		    "price": price,
		    "currency": item.get("currency", "USD")

                })

    except Exception as e:
        print("YAHOO ERROR:", e)

    try:
        data = requests.get(
            "https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd",
            headers=headers,
            timeout=5
        ).json()

        for c in data[:15]:
    	    assets.append({
                "t": c["symbol"].upper(),
            	"name": c["name"],
            	"price": c["current_price"],
            	"currency": "USD"
            })

    except Exception as e:
        print("COINGECKO ERROR:", e)

    print("FINAL ASSETS:", len(assets))  # ✅ debug

    market_cache["market"] = (assets, now)

    return assets

# ===== HISTORICAL DATA =====
def get_historical_data(symbol, period):

    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range={period}&interval=1d"
    headers = {"User-Agent": "Mozilla/5.0"}

    try:
        r = requests.get(url, headers=headers, timeout=5).json()
        return r
    except:
        return None

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
    if price < 300:
        return "KÖP"
    elif price > 1000:
        return "SÄLJ"
    return "AVVAKTA KÖP"

def get_score(sig, price, t):
    base = 80 if sig == "KÖP" else 60 if sig == "AVVAKTA KÖP" else 30

    val = base \
        + (get_news_score(t) * 3) \
        

    return max(0, min(100, int(val)))

def get_reason(sig, price, t):

    reasons = []

    if sig == "KÖP":
        reasons.append("Trend visar köpläge")
    elif sig == "SÄLJ":
        reasons.append("Hög värdering")
    else:
        reasons.append("Neutral trend")

    if price < 100:
        reasons.append("Låg prisnivå (potential)")
    elif price > 500:
        reasons.append("Hög prisnivå")

    news_score = get_news_score(t)

    if news_score > 0:
        reasons.append("Positiva nyheter")
    elif news_score < 0:
        reasons.append("Negativa nyheter")

    return "\n".join(reasons)

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

# ===== AI DAILY SCAN =====
def run_daily_ai(strategy="short", risk="medium", capital=10000):

    now = time.time()

    # ✅ cache
    if ai_cache["data"] and now - ai_cache["last_run"] < AI_REFRESH_TIME:
        return ai_cache["data"]

    print("🔄 Running AI daily scan...")

    assets = safe_fetch(get_market_assets)
    result = []

    for s in assets:
        price = s.get("price", 0)
        sig = get_signal(price)

        # ✅ historik
        hist = get_historical_data(s["t"], "1mo")

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
        s["reason"] = get_reason(sig, price, s["t"])

        # ✅ trigger
        s["trigger_score"], s["trigger_reasons"] = get_trigger_score(s)

        result.append(s)

    # ✅ sortering
    result = sorted(result, key=lambda x: x["score"], reverse=True)
    result = [s for s in result if s["trigger_score"] >= 2]

    # ✅ cache
    ai_cache["data"] = result
    ai_cache["last_run"] = now

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
    trend = get_trend_score_from_history([])

    # ===== Strategy =====

    if strategy == "short":
        take_profit = 8
        stop_loss = -4

    elif strategy == "long":
        take_profit = 20
        stop_loss = -12

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


# ===== PORTFOLIO =====
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


# ===== LINK =====
def get_link(t):
    return f'<a href="https://www.avanza.se/aktier/sok.html?query={t}" target="_blank">Avanza</a>'

# ===== DASHBOARD =====
@app.route("/dashboard", methods=["GET","POST"])
def dashboard():
    
    print("METHOD:", request.method)
    
    user = session.get("user")
    if not user:
        return redirect("/login")

    amount = int(request.form.get("amount") or 10000)
    print("AMOUNT:", amount)

    ai_strategy = request.form.get("ai_strategy", "short")
    ai_risk = request.form.get("ai_risk","medium")
    pf_risk = request.form.get("pf_risk","medium")
    period = request.form.get("period", "3m")


    # ===== AUTO Strategi → PERIOD =====
    if ai_strategy == "short":
        period = "1w"
    elif ai_strategy == "long":
        period = "1y"

    print("PERIOD:", period)
    top_n = int(request.form.get("top_n", 5))
    
    assets = safe_fetch(get_market_assets)

    print("ASSETS COUNT (first):", len(assets))

    if not assets:
        print("⚠️ Using fallback data")
        assets = [
            {"t": "AAPL", "name": "Apple", "price": 180},
            {"t": "MSFT", "name": "Microsoft", "price": 350},
            {"t": "NVDA", "name": "Nvidia", "price": 1200}
        ]

    print("FINAL ASSETS USED:", len(assets)) 
    
    ranked = run_daily_ai(ai_strategy, ai_risk, amount)
    stocks = [s for s in ranked if "." in s["t"] or len(s["t"]) <= 5]
    crypto = [s for s in ranked if len(s["t"]) > 5]


# ===== HANDLE BUY / SELL =====
    if request.method == "POST":

        for s in ranked:
            t = s["t"]

            # ✅ BUY
            if f"buy_{t}" in request.form:
                qty = request.form.get(f"buyqty_{t}")

                if qty and qty.isdigit():
                    buy(user, t, int(qty), s["price"])
                    print("BOUGHT:", t, qty)

            # ✅ SELL
            if f"sell_{t}" in request.form:
                qty = request.form.get(f"sellqty_{t}")

                if qty and qty.isdigit():
                    sell(user, t, int(qty))
                    print("SOLD:", t, qty)
    
    pf = portfolio(user)
    total_pl = 0
    total_start_value = 0
    total_value = 0

    buys = ranked[:top_n]
    
    try:
        per = int(amount) / len(buys) if buys else 0
    except:
        per = 0

    html = f"""
<html>
<head>

<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>

<style>
.popup {{
    display:none;
    position:fixed;
    top:20%;
    left:50%;
    transform:translateX(-50%);
    background:#fff;
    border:1px solid #ccc;
    padding:20px;
    z-index:1000;
    max-width:300px;
}}
</style>

<script>
function showPopup(text){{
    document.getElementById("popupText").innerText = text;
    document.getElementById("popup").style.display = "block";
}}

function closePopup(){{
    document.getElementById("popup").style.display = "none";
}}
</script>

<meta name="viewport" content="width=device-width, initial-scale=1">

<style>
body {{
    font-family: Arial;
    padding: 10px;
}}

input {{
    width: 100%;
    padding: 8px;
}}

button {{
    width: auto;
    padding: 8px;
    margin-top: 5px;
}}

.small-btn {{
    width: auto;
    padding: 5px 8px;
}}

.buy-btn {{
    background: green;
    color: white;
}}

.sell-btn {{
    background: red;
    color: white;
}}

.container {{
    display: flex;
    flex-wrap: wrap;
}}

.box {{
    flex: 1;
    min-width: 300px;
    padding: 5px;
}}
</style>
</head>

<body>
<div id="popup" class="popup">
    <div id="popupText"></div>
    <button type="button" class="small-btn" onclick="closePopup()">Stäng</button>
</div>


<h1>🚀 Trading ({user})</h1>

<p style="color:red; font-size:14px;">
⚠️ Detta är endast AI-rekommendationer. All handel sker på egen risk.
</p>

<a href="/logout">Logout</a> |
<a href="/change_password">Change Password</a>

<hr>


<form method="POST" action="/dashboard">

Kapital:
<input name="amount" value="{amount}" maxlength="15" style="width:120px;">

Strategi:
<select name="ai_strategy">
<option value="short" {"selected" if ai_strategy=="short" else ""}>Kort</option>
<option value="long" {"selected" if ai_strategy=="long" else ""}>Lång</option>
</select>

Risk:
<select name="ai_risk">
<option value="low" {"selected" if ai_risk=="low" else ""}>Låg</option>
<option value="medium" {"selected" if ai_risk=="medium" else ""}>Medel</option>
<option value="high" {"selected" if ai_risk=="high" else ""}>Hög</option>
</select>

Antal AI-rekommendationer:
<select name="top_n">
<option>3</option>
<option>5</option>
<option>7</option>
<option>10</option>
<option>12</option>
<option>15</option>
</select>

<br><br>

<button type="submit" class="small-btn">
Analysera
</button>

</form>


<h2>
AI Rekommenderar
<span style="font-size:12px; color:gray; display:block; margin-top:5px;">

Score 0–100 (AI-betyg baserat på trend, nyheter och risk)

<br><br>

<strong>Skala:</strong><br>
0–20 = Mycket svag ❌<br>
21–40 = Svag ⚠️<br>
41–60 = Neutral ➖<br>
61–80 = Stark ✅<br>
81–100 = Mycket stark 🔥

</span>
</h2>

<div class="container">
"""

    # ===== KÖP =====
    html += "<p style='color:red; font-weight:bold;'>🚨 SÄLJ‑signaler skickas via mail</p>"
    html += "<div class='box'><h3>KÖP</h3>"

    for s in stocks[:top_n]:
        qty = max(1, int(per/s["price"])) if per else "-"
        ai_qty = qty
        sl = get_stop_loss(s["price"], ai_risk)

        html += f"""
<form method="post" style="border:1px solid #ccc; padding:10px; margin-bottom:10px;">

<b>{s.get('name', s['t'])} ({s['t']}) (Score {s['score']})</b>
<button type="button" class="small-btn"
style="margin-left:5px;"
onclick="showPopup(`{s['reason']}`)">
AI 💡
</button>
| Pris: {s['price']} {s.get('currency', '')}<br>
AI: {s['signal']}<br>
Trigger: {s['trigger_score']}<br>
AI föreslår: {ai_qty} st | Stop-loss: {sl} | {get_link(s['t'])}<br><br>
<input name="buyqty_{s['t']}" style="width:60px;">
<button class="small-btn" name="buy_{s['t']}">
Köp
</button>
</form>
<hr>
"""

    html += "</div>"

    html += "<div class='box'><h3>KRYPTO</h3>"

    for s in crypto[:top_n]:
        html += f"""
    <form method="post" style="border:1px solid #ccc; padding:10px; margin-bottom:10px;">
    <b>{s.get('name', s['t'])} ({s['t']}) (Score {s['score']})</b><br>
    Pris: {s['price']} {s.get('currency','')}<br>
    AI: {s['signal']}<br>
    Trigger: {s['trigger_score']}<br>
    </form>
    <hr>
    """

    html += "</div>"

    # ===== AVVAKTA KÖP =====
    html += "<div class='box'><h3>AVVAKTA KÖP</h3>"

    for s in ranked:
        if s["signal"] == "AVVAKTA KÖP":
            qty = max(1, int(per/s["price"])) if per else "-"
            ai_qty = qty

            sl = get_stop_loss(s["price"], ai_risk)

            html += f"""
<form method="post" style="border:1px solid #ccc; padding:10px; margin-bottom:10px;">
<b>{s.get('name', s['t'])} (Score {s['score']})</b>
<button type="button" class="small-btn"
style="margin-left:5px;"
onclick="showPopup(`{s['reason']}`)">
AI 💡
</button>
| Pris: {s['price']} {s.get('currency', '')}
AI: {s['signal']}<br>
AI föreslår: {ai_qty} st | Stop-loss: {sl} | {get_link(s['t'])}<br><br>

<input name="buyqty_{s['t']}" style="width:60px;">
<button class="small-btn" name="buy_{s['t']}">
Köp
</button>
</form>
<hr>
"""

    html += "</div></div>"

    # ===== PORTFÖLJ =====

    html += "<h2>Min portfölj</h2>"        

    html += f"""
<form method="post">
Strategi:
<select name="pf_strategy">
<option value="short">Kort</option>
<option value="long">Lång</option>
</select><br>

Risk:
<select name="pf_risk">
<option value="low">Låg</option>
<option value="medium">Medel</option>
<option value="high">Hög</option>
</select><br>

Current Strategi period: <b>{period}</b><br>

Time Range:
<select name="period">
    <option value="1d">1 dag</option>
    <option value="1w">1 vecka</option>
    <option value="3m">3 månader</option>
    <option value="6m">6 månader</option>
    <option value="1y">1 år</option>
    <option value="3y">3 år</option>
</select>

<button type="submit" class="small-btn">Update</button>
</form><hr>
"""

    html += "<div style='display:flex;'>"

    sell_col = "<div style='width:33%'><h3>SÄLJ</h3>"
    buy_col  = "<div style='width:33%'><h3>KÖP MER</h3>"
    hold_col = "<div style='width:33%'><h3>Avvakta</h3>"

    for s in pf:
        current_price = next((x["price"] for x in ranked if x["t"] == s["t"]), 0)
        name = next((x.get("name", x["t"]) for x in ranked if x["t"] == s["t"]), s["t"])

        position_value = current_price * s["qty"]
        total_value += position_value

        hist = get_historical_data(s["t"], period)

        start_price = current_price

        if hist:
            try:
                prices = hist["chart"]["result"][0]["indicators"]["quote"][0]["close"]
                start_price = prices[0] if prices and prices[0] else current_price
            except:
                pass

        pl_value = (current_price - start_price) * s["qty"]

        pl_pct = ((current_price - start_price) / start_price * 100) if start_price else 0

        decision, reason = portfolio_ai_decision(
            pl_pct,
            current_price,
            start_price,
            s["t"],
            pf_risk,
            ai_strategy
        )

        if decision == "SÄLJ":
            send_alert(user, f"SÄLJ {s['t']} nu! Anledning: {reason}")

        analysis = portfolio_analysis(decision, pl_pct)

        total_pl += pl_value
        total_start_value += start_price * s["qty"]

        sig = get_signal(current_price)
        sl = get_stop_loss(current_price, pf_risk)

        block = f"""

<form method="post" style="border:1px solid #ccc; padding:10px; margin-bottom:10px;">
<b>{name} ({s['t']})</b> ({s['qty']})<br>
Pris: {current_price}<br>

P/L: 
<span style="color:{'green' if pl_value >= 0 else 'red'}">
{round(pl_value, 2)} ({round(pl_pct, 2)}%)
</span><br>

Decision: <b style="color:{'red' if decision=='SÄLJ' else 'green' if decision=='KÖP MER' else 'orange'}">
{decision}</b><br>

Reason: {reason}<br>

Hypotes: {analysis["Hypotes"]}<br>
Risk: {analysis["Risk"]}<br>
Position: {analysis["Position"]}<br>

Stop-loss: {sl}<br>
{get_link(s['t'])}<br>

Köp <input name="buyqty_{s['t']}">
<button class="small-btn" name="buy_{s['t']}">KÖP</button><br>

Sälj <input name="sellqty_{s['t']}">
<button class="small-btn" name="sell_{s['t']}">SÄLJ</button>
</form>
<hr>
"""
        if decision == "SÄLJ":
            sell_col += block
        elif decision == "KÖP MER":
            buy_col += block
        elif decision.lower() in ["avvakta", "hold"]:
            hold_col += block
        else:
            hold_col += block

    total_pct = (total_pl / total_start_value * 100) if total_start_value else 0
    labels = ["Tidigare", "Igår", "Nu"]

    # ✅ graf-data
    values = [round(total_value * x, 2) for x in [0.8, 0.9, 1.0]]
    labels = ["Tidigare", "Igår", "Nu"]

    # ✅ GRAF
    html += f"""
    <canvas id="portfolioChart" style="max-width:100%; margin-top:20px;"></canvas>

    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <script>
    const ctx = document.getElementById('portfolioChart').getContext('2d');

    new Chart(ctx, {{
        type: 'line',
        data: {{
            labels: {labels},
            datasets: [{{
                label: 'Portföljvärde',
                data: {values},
                borderColor: 'green',
                tension: 0.2
            }}]
        }}
    }});
    </script>
    """

# ✅ DIN P/L BLOCK (ska ligga kvar under)

    html += f"""
    <p>
    <b>Totalt värde:</b> {round(total_value, 2)} kr
    </p>
    """

    html += f"""
    <p>
    <b>P/L:</b>
    <span style="color:{'green' if total_pl >= 0 else 'red'}">
    {round(total_pl, 2)} ({round(total_pct, 2)}%)
    </span>
    </p>
    """

    return html

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


# ===== HOME =====
@app.route("/")
def home():
    return redirect("/login")


# ===== LOGOUT =====
@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


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
        server = smtplib.SMTP("smtp.office365.com", 587)
        server.starttls()
        server.login(sender, password)
        server.send_message(msg)
        server.quit()
        print("ALERT SENT:", message)
    except Exception as e:
        print("Alert error:", e)

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

if __name__=="__main__":
        port = int(os.environ.get("PORT", 10000))
        app.run(host="0.0.0.0", port=port)