import sys
import yfinance as yf
import pandas as pd

FILE = "stock_data/my_trades.txt"


# ===== PRICE =====
def price(t):
    try:
        data = yf.Ticker(t)
        hist = data.history(period="1d")

        if hist.empty:
            return 0

        return round(float(hist["Close"].iloc[-1]), 2)
    except:
        return 0


# ===== INDICATORS =====
def get_indicators(t):

    data = yf.Ticker(t).history(period="3mo")

    if data.empty:
        return None

    close = data["Close"]

    # ✅ SMAs
    sma20 = close.rolling(20).mean().iloc[-1]
    sma50 = close.rolling(50).mean().iloc[-1]

    # ✅ RSI
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = -delta.clip(upper=0).rolling(14).mean()
    rs = gain / loss
    rsi = 100 - (100 / (1 + rs))
    rsi_val = rsi.iloc[-1]

    # ✅ MACD
    ema12 = close.ewm(span=12).mean()
    ema26 = close.ewm(span=26).mean()
    macd = ema12 - ema26
    signal = macd.ewm(span=9).mean()

    macd_val = macd.iloc[-1]
    signal_val = signal.iloc[-1]

    return {
        "price": close.iloc[-1],
        "sma20": sma20,
        "sma50": sma50,
        "rsi": rsi_val,
        "macd": macd_val,
        "macd_signal": signal_val
    }


# ===== AI SIGNAL =====
def signal(t):

    ind = get_indicators(t)
    if not ind:
        return "HOLD"

    score = 0

    # Trend
    if ind["price"] > ind["sma20"]:
        score += 1
    if ind["price"] > ind["sma50"]:
        score += 1

    # RSI
    if ind["rsi"] < 30:
        score += 2  # oversold
    elif ind["rsi"] > 70:
        score -= 2  # overbought

    # MACD
    if ind["macd"] > ind["macd_signal"]:
        score += 1
    else:
        score -= 1

    if score >= 2:
        return "BUY"
    elif score <= -2:
        return "SELL"
    else:
        return "HOLD"


# ===== BUY / SELL =====
def buy(user, t, qty, price_val):

    lines=[]
    found=False

    for l in open(FILE):
        parts=l.strip().split("|")
        if len(parts)<4:
            continue

        u,tick,q,e=parts
        q=int(q)
        e=float(e)

        if u==user and tick==t:
            new_qty = q + int(qty)
            new_entry = ((q*e)+(int(qty)*price_val))/new_qty
            lines.append(f"{u}|{tick}|{new_qty}|{round(new_entry,2)}\n")
            found=True
        else:
            lines.append(l)

    if not found:
        lines.append(f"{user}|{t}|{qty}|{price_val}\n")

    open(FILE,"w").writelines(lines)


def sell(user,t,qty):

    lines=[]

    for l in open(FILE):
        parts=l.strip().split("|")
        if len(parts)<4:
            continue

        u,tick,q,e=parts
        q=int(q)

        if u==user and tick==t:
            new_q=q-int(qty)
            if new_q>0:
                lines.append(f"{u}|{tick}|{new_q}|{e}\n")
        else:
            lines.append(l)

    open(FILE,"w").writelines(lines)


# ===== HANDLER =====
def handle(cmd):

    parts=cmd.split("|")
    action=parts[0]

    if action=="price":
        return str(price(parts[1]))

    if action=="signal":
        return signal(parts[1])

    user=parts[1]
    t=parts[2]
    qty=int(parts[3])
    p=float(parts[4])

    if action=="buy":
        buy(user,t,qty,p)
    if action=="sell":
        sell(user,t,qty)

    return "OK"


if __name__=="__main__":
    print(handle(sys.argv[1]))
