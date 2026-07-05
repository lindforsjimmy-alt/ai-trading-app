# Delad trading-logik för CLI och webbapp
# Kommentarer på svenska enligt projektregler

import os

try:
    import psycopg
except Exception:
    psycopg = None

import yfinance as yf

FILE = "stock_data/my_trades.txt"
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()


def db_enabled():
    return bool(DATABASE_URL and psycopg is not None)


def db_connect():
    return psycopg.connect(DATABASE_URL)


def get_user_id(email):
    target = (email or "").strip().lower()
    if not target:
        return None
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM users WHERE LOWER(email) = LOWER(%s) LIMIT 1",
                (target,),
            )
            row = cur.fetchone()
            return row[0] if row else None


def price(t):
    """Hämtar senast stängningspris för ticker via yfinance."""
    try:
        data = yf.Ticker(t)
        hist = data.history(period="1d")

        if hist.empty:
            return 0

        return round(float(hist["Close"].iloc[-1]), 2)
    except Exception:
        return 0


def get_indicators(t):
    """Beräknar enkla indikatorer (SMA, RSI, MACD) från 3 månader historik."""
    try:
        data = yf.Ticker(t).history(period="3mo")

        if data.empty:
            return None

        close = data["Close"]

        sma20 = close.rolling(20).mean().iloc[-1]
        sma50 = close.rolling(50).mean().iloc[-1]

        delta = close.diff()
        gain = delta.clip(lower=0).rolling(14).mean()
        loss = -delta.clip(upper=0).rolling(14).mean()
        rs = gain / loss
        rsi = 100 - (100 / (1 + rs))
        rsi_val = rsi.iloc[-1]

        ema12 = close.ewm(span=12).mean()
        ema26 = close.ewm(span=26).mean()
        macd = ema12 - ema26
        signal_line = macd.ewm(span=9).mean()

        macd_val = macd.iloc[-1]
        signal_val = signal_line.iloc[-1]

        return {
            "price": close.iloc[-1],
            "sma20": sma20,
            "sma50": sma50,
            "rsi": rsi_val,
            "macd": macd_val,
            "macd_signal": signal_val
        }
    except Exception:
        return None


def signal(t):
    """Returnerar enkel signal BUY/HOLD/SELL baserat på indikatorer."""
    ind = get_indicators(t)
    if not ind:
        return "HOLD"

    score = 0

    if ind["price"] > ind["sma20"]:
        score += 1
    if ind["price"] > ind["sma50"]:
        score += 1

    if ind["rsi"] < 30:
        score += 2
    elif ind["rsi"] > 70:
        score -= 2

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


def buy(user, t, qty, price_val):
    """Registrerar köp i `stock_data/my_trades.txt`."""
    if db_enabled():
        user_id = get_user_id(user)
        if user_id is not None:
            with db_connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO trades (user_id, ticker, side, qty, price)
                        VALUES (%s, %s, 'BUY', %s, %s)
                        """,
                        (user_id, str(t).upper(), float(qty), float(price_val)),
                    )
                conn.commit()
            return

    with open(FILE, "a") as f:
        f.write(f"{user}|{t}|{qty}|{price_val}\n")


def sell(user, t, qty):
    """Utför en enkel sell genom att uppdatera poster i trades-filen."""
    if db_enabled():
        user_id = get_user_id(user)
        if user_id is not None:
            remaining = int(float(qty))
            if remaining <= 0:
                return

            with db_connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT id, qty
                        FROM trades
                        WHERE user_id = %s AND ticker = %s AND side = 'BUY'
                        ORDER BY id
                        """,
                        (user_id, str(t).upper()),
                    )
                    rows = cur.fetchall()

                    for trade_id, trade_qty in rows:
                        if remaining <= 0:
                            break
                        current_qty = int(float(trade_qty))
                        if current_qty <= remaining:
                            cur.execute("DELETE FROM trades WHERE id = %s", (trade_id,))
                            remaining -= current_qty
                        else:
                            cur.execute(
                                "UPDATE trades SET qty = %s WHERE id = %s",
                                (current_qty - remaining, trade_id),
                            )
                            remaining = 0
                conn.commit()
            return

    lines = open(FILE).readlines()
    new = []
    for l in lines:
        parts = l.strip().split("|")
        if len(parts) < 4:
            continue
        u, ticker, q, p = parts
        q = int(float(q))

        if u == user and ticker == t:
            new_q = q - int(qty)
            if new_q > 0:
                new.append(f"{u}|{ticker}|{new_q}|{p}\n")
        else:
            new.append(l)

    open(FILE, "w").writelines(new)


def handle(cmd):
    """Enkel CLI-hanterare som accepterar kommandosträngar som tidigare."""
    parts = cmd.split("|")
    action = parts[0]

    if action == "price":
        return str(price(parts[1]))

    if action == "signal":
        return signal(parts[1])

    user = parts[1]
    t = parts[2]
    qty = int(parts[3])
    p = float(parts[4])

    if action == "buy":
        buy(user, t, qty, p)
    if action == "sell":
        sell(user, t, qty)

    return "OK"
