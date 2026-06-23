from flask import Flask, redirect, session, request
import os, requests, time, feedparser, math, hashlib

app = Flask(__name__)
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
price_cache = {}
CACHE_TIME = 60

market_cache = {}
MARKET_CACHE_TIME = 120

news_cache = {}
NEWS_CACHE_TIME = 300

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
                assets.append({
                    "t": symbol,
                    "name": symbol,
                    "price": price
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
                "price": c["current_price"]
            })
    except Exception as e:
        print("COINGECKO ERROR:", e)

    print("FINAL ASSETS:", len(assets))  # ✅ debug

    market_cache["market"] = (assets, now)

    return assets

# ===== AI =====
def get_trend_score(price):
    if price < 50: return 2
    elif price < 200: return 1
    elif price > 800: return -2
    return 0

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


def get_rsi_score(price):
    if price < 80: return 1
    elif price > 700: return -1
    return 0

def get_signal(price):
    if price < 300:
        return "BUY"
    elif price > 1000:
        return "SELL"
    return "WATCH"

def get_score(sig, price, t):
    base = 80 if sig == "BUY" else 60 if sig == "WATCH" else 30

    val = base \
        + (get_trend_score(price) * 5) \
        + (get_news_score(t) * 3) \
        + (get_rsi_score(price) * 5)

    return max(0, min(100, int(val)))

def get_reason(sig, price, t):

    reasons = []

    if sig == "BUY":
        reasons.append("Trend visar köpläge")
    elif sig == "SELL":
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

    reasons.append(f"Se nyheter: https://news.google.com/rss/search?q={t}")

    return "\\n".join(reasons)

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

    amount = request.form.get("amount") or "10000"
    print("AMOUNT:", amount)

    ai_strategy = request.form.get("ai_strategy","short")
    ai_risk = request.form.get("ai_risk","medium")
    pf_risk = request.form.get("pf_risk","medium")
    top_n = int(request.form.get("top_n", 5))
    assets = get_market_assets()
    print("ASSETS COUNT:", len(assets))
    
    pf = portfolio(user)

    ranked = []
    for s in assets:
        price = s.get("price", 0)
        sig = get_signal(price)

        s["signal"] = sig
        s["score"] = get_score(sig, price, s["t"])
        s["reason"] = get_reason(sig, price, s["t"])
        ranked.append(s)

    ranked = sorted(ranked, key=lambda x: x["score"], reverse=True)

    buys = ranked[:top_n]
    
    try:
        per = int(amount) / len(buys) if buys else 0
    except:
        per = 0



    html = f"""
<html>
<head>

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
<option value="short">Kort</option>
<option value="long">Lång</option>
</select>

Risk:
<select name="ai_risk">
<option value="low">Låg</option>
<option value="medium">Medel</option>
<option value="high">Hög</option>
</select>

Antal:
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
Analyze
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

    # ===== BUY =====
    html += "<div class='box'><h3>BUY</h3>"

    for s in buys:
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
| Pris: {s['price']}<br>
AI: {s['signal']}<br>
AI föreslår: {ai_qty} st | Stop-loss: {sl} | {get_link(s['t'])}<br><br>
<input name="buyqty_{s['t']}" style="width:60px;">
<button class="small-btn" name="buy_{s['t']}">
BUY
</button>
</form>
<hr>
"""

    html += "</div>"

    # ===== WATCH =====
    html += "<div class='box'><h3>WATCH</h3>"

    for s in ranked:
        if s["signal"] == "WATCH":
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
| Pris: {s['price']}<br>
AI: {s['signal']}<br>
AI föreslår: {ai_qty} st | Stop-loss: {sl} | {get_link(s['t'])}<br><br>

<input name="buyqty_{s['t']}" style="width:60px;">
<button class="small-btn" name="buy_{s['t']}">
BUY
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

<button type="submit" class="small-btn">Update</button>
</form><hr>
"""

    html += "<div style='display:flex;'>"

    sell_col = "<div style='width:33%'><h3>SELL</h3>"
    buy_col  = "<div style='width:33%'><h3>BUY MORE</h3>"
    hold_col = "<div style='width:33%'><h3>HOLD</h3>"

    for s in pf:
        price = next((x["price"] for x in ranked if x["t"] == s["t"]), 0)
        sig = get_signal(price)
        sl = get_stop_loss(price, pf_risk)

        block = f"""
<form method="post" style="border:1px solid #ccc; padding:10px; margin-bottom:10px;">
<b>{s['t']}</b> ({s['qty']})<br>
Pris: {price}<br>
Stop-loss: {sl}<br>
{get_link(s['t'])}<br>

Köp <input name="buyqty_{s['t']}">
<button class="small-btn" name="buy_{s['t']}">BUY</button><br>

Sälj <input name="sellqty_{s['t']}">
<button class="small-btn" name="sell_{s['t']}">SELL</button>
</form>
<hr>
"""

        if sig == "SELL":
            sell_col += block
        elif sig == "BUY":
            buy_col += block
        else:
            hold_col += block

    html += sell_col + "</div>" + buy_col + "</div>" + hold_col + "</div></div>"

    html += """
</body>
</html>
"""

    return html

# ===== APPROVAL SYSTEM =====
PENDING_FILE = "stock_data/pending.txt"
open(PENDING_FILE, "a").close()

import smtplib
from email.mime.text import MIMEText


def send_approval_email(new_user_email):

    sender = "YOUR_MAIL@outlook.com"
    password = "APP_PASSWORD"   # använd app password!

    link = f"http://localhost:10000/approve?email={new_user_email}"

    body = f"Godkänn ny användare:\n{new_user_email}\n\n{link}"

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
        print("Mail error:", e)


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
# ===== FORGOT PASSWORD =====# ===== FORGET", "POST"])
def forgot():

    msg = ""

    if request.method == "POST":
        email = request.form.get("email")
        msg = f"Återställning skickad till {email}"

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