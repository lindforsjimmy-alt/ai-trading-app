from flask import Flask, redirect, session, request
import os, requests, time, feedparser, math, hashlib

app = Flask(__name__)
app.secret_key = "secret"

# ===== FILES =====
DATA_FILE = "stock_data/my_trades.txt"
USERS_FILE = "stock_data/users.txt"

os.makedirs("stock_data", exist_ok=True)
open(DATA_FILE, "a").close()
open(USERS_FILE, "a").close()

# ===== HASH =====
def hash_password(p):
    return hashlib.sha256(p.encode()).hexdigest()

def create_user(email, password):
    with open(USERS_FILE, "a") as f:
        f.write(f"{email}|{hash_password(password)}\n")

def check_user(email, password):
    with open(USERS_FILE) as f:
        for l in f:
            e,p = l.strip().split("|")
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
price_cache = {}
CACHE_TIME = 60

market_cache = {}
MARKET_CACHE_TIME = 120

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
        data,t = market_cache["market"]
        if now - t < MARKET_CACHE_TIME:
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

    for s in symbols:
        try:
            url = f"https://query1.finance.yahoo.com/v7/finance/quote?symbols={s}"
            r = requests.get(url).json()
            res = r["quoteResponse"]["result"]

            if not res:
                continue

            price = res[0]["regularMarketPrice"]

            if price and not math.isnan(price):
                assets.append({"t": s, "name": s, "price": price})
        except:
            continue

    try:
        data = requests.get("https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd").json()
        for c in data[:15]:
            assets.append({
                "t": c["symbol"].upper(),
                "name": c["name"],
                "price": c["current_price"]
            })
    except:
        pass

    market_cache["market"] = (assets, now)
    return assets


# ===== AI =====
def get_trend_score(price):
    if price < 50: return 2
    elif price < 200: return 1
    elif price > 800: return -2
    return 0

def get_news_score(t):
    try:
        feed = feedparser.parse(f"https://news.google.com/rss/search?q={t}")
        score = 0
        for e in feed.entries[:3]:
            txt = e.title.lower()
            if "up" in txt or "growth" in txt:
                score += 1
            if "down" in txt or "drop" in txt:
                score -= 1
        return score
    except:
        return 0

def get_rsi_score(price):
    if price < 80: return 1
    elif price > 700: return -1
    return 0

def get_signal(price):
    if price < 100:
        return "BUY"
    elif price > 1000:
        return "SELL"
    return "WATCH"

def get_score(sig, price, t):
    base = 80 if sig=="BUY" else 60 if sig=="WATCH" else 30
    val = base + (get_trend_score(price)*5) + (get_news_score(t)*3) + (get_rsi_score(price)*5)
    return max(0, min(100, int(val)))


# ===== PORTFOLIO =====
def portfolio(user):
    data={}
    with open(DATA_FILE) as f:
        for l in f:
            parts=l.strip().split("|")
            if len(parts)<4: continue
            u,t,q,_=parts
            if u!=user: continue
            data[t]=data.get(t,0)+int(float(q))
    return [{"t":t,"qty":q} for t,q in data.items() if q>0]


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
@app.route("/dashboard",methods=["GET","POST"])
def dashboard():

    user=session.get("user")
    if not user:
        return redirect("/login")

    amount=request.form.get("amount","")

    ai_strategy=request.form.get("ai_strategy","short")
    ai_risk=request.form.get("ai_risk","medium")

    pf_strategy=request.form.get("pf_strategy","short")
    pf_risk=request.form.get("pf_risk","medium")

    assets=get_market_assets()
    pf=portfolio(user)

    ranked=[]
    for s in assets:
        sig=get_signal(s["price"])
        if ai_strategy=="short" and ai_risk=="high" and sig=="WATCH":
            sig="BUY"
        s["signal"]=sig
        s["score"]=get_score(sig,s["price"],s["t"])
        ranked.append(s)

    buys=[s for s in ranked if s["signal"]=="BUY"]
    per=int(amount)/len(buys) if amount.isdigit() and buys else 0

    # AUTO AI
    if "auto_ai" in request.form:
        for s in buys:
            qty=max(1,int(per/s["price"]))
            buy(user,s["t"],qty,s["price"])
        return redirect("/dashboard")

    # HANDLERS
    for s in ranked:
        if f"buy_{s['t']}" in request.form:
            q=request.form.get(f"buyqty_{s['t']}")
            if q and q.isdigit():
                buy(user,s["t"],int(q),s["price"])
            return redirect("/dashboard")

    for s in pf:
        if f"sell_{s['t']}" in request.form:
            q=request.form.get(f"sellqty_{s['t']}")
            if q and q.isdigit():
                sell(user,s["t"],int(q))
            return redirect("/dashboard")


    # ===== UI =====
    html=f"<h1>🚀 Trading ({user})</h1>/logoutLogout</a><hr>"

    html+=f"""
<form method="post">
Kapital: <input name="amount" value="{amount}"><br>

Strategi:
<select name="ai_strategy">
<option value="short">Kort</option>
<option value="long">Lång</option>
</select><br>

Risk:
<select name="ai_risk">
<option value="low">Låg</option>
<option value="medium">Medel</option>
<option value="high">Hög</option>
</select><br>

<button>Analysera</button>
<button name="auto_ai">AI Auto‑Invest</button>
</form><hr>
"""

    html+="<h2>AI Rekommenderar</h2><div style='display:flex;'>"

    # BUY
    html+="<div style='width:50%'><h3>BUY</h3>"
    for s in buys:
        qty=max(1,int(per/s["price"])) if per else "-"
        sl=get_stop_loss(s["price"], ai_risk)

        html+=f"""
<form method="post">
<b>{s['name']}</b><br>
Score: {s['score']}<br>
Pris: {s['price']}<br>
Rek: {qty}<br>
Stop‑loss: {sl}<br>
{get_link(s['t'])}<br>

<input name="buyqty_{s['t']}">
<button name="buy_{s['t']}">KÖP</button>
</form><hr>
"""
    html+="</div>"

    # WATCH
    html+="<div style='width:50%'><h3>WATCH</h3>"
    for s in ranked:
        if s["signal"]=="WATCH":
            sl=get_stop_loss(s["price"], ai_risk)

            html+=f"""
<form method="post">
<b>{s['name']}</b><br>
Score: {s['score']}<br>
Pris: {s['price']}<br>
Stop‑loss: {sl}<br>
{get_link(s['t'])}<br>

<input name="buyqty_{s['t']}">
<button name="buy_{s['t']}">KÖP</button>
</form><hr>
"""
    html+="</div></div>"

    # ===== PORTFÖLJ =====
    html+="<h2>Min portfölj</h2>"

    html+=f"""
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

<button>Uppdatera</button>
</form><hr>
"""

    html+="<div style='display:flex;'>"

    sell_col="<div style='width:33%'><h3>SELL</h3>"
    buy_col="<div style='width:33%'><h3>BUY MORE</h3>"
    hold_col="<div style='width:33%'><h3>HOLD</h3>"

    for s in pf:
        price=next((x["price"] for x in ranked if x["t"]==s["t"]),0)
        sig=get_signal(price)
        sl=get_stop_loss(price, pf_risk)

        block=f"""
<form method="post">
<b>{s['t']}</b> ({s['qty']})<br>
Pris: {price}<br>
Stop‑loss: {sl}<br>
{get_link(s['t'])}<br>

Köp <input name="buyqty_{s['t']}">
<button name="buy_{s['t']}">KÖP</button><br>

Sälj <input name="sellqty_{s['t']}">
<button name="sell_{s['t']}">SÄLJ</button>
</form><hr>
"""

        if sig=="SELL": sell_col+=block
        elif sig=="BUY": buy_col+=block
        else: hold_col+=block

    html+=sell_col+"</div>"+buy_col+"</div>"+hold_col+"</div></div>"

    return html


# ===== LOGIN =====
@app.route("/login",methods=["GET","POST"])
def login():

    msg=""

    if request.method=="POST":

        if "login" in request.form:
            email=request.form.get("email")
            password=request.form.get("password")

            if check_user(email,password):
                session["user"]=email
                return redirect("/dashboard")
            else:
                msg="Fel login"

        elif "register" in request.form:
            email=request.form.get("email")
            password=request.form.get("password")

            if not user_exists(email):
                create_user(email,password)
                msg="Konto skapat"
            else:
                msg="Finns redan"

    return f"""
<h2>Login</h2>

<form method="post">
Email:<br>
<input name="email"><br>

Lösenord:<br>
<input type="password" name="password"><br><br>

<button name="login">Logga in</button>
<button name="register">Skapa konto</button>
</form>

<p>{msg}</p>
"""


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


@app.route("/")
def home():
    return redirect("/login")


if __name__=="__main__":
    app.run(debug=True)