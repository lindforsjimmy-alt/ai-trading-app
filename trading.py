# Delad trading-logik för CLI och webbapp
# Kommentarer på svenska enligt projektregler

import os
from datetime import datetime

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


def _fallback_sold_trades_file():
    return "stock_data/sold_trades.txt"


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


def _record_sale_event(user, t, sold_qty, avg_buy_price, sell_price):
    if sold_qty <= 0 or sell_price <= 0:
        return

    ticker = str(t).upper()
    avg_buy_price = float(avg_buy_price or 0)
    sell_price = float(sell_price or 0)
    realized_pnl_pct = 0.0
    if avg_buy_price > 0:
        realized_pnl_pct = ((sell_price - avg_buy_price) / avg_buy_price) * 100.0
    sold_with_loss = realized_pnl_pct < 0

    if db_enabled():
        user_id = get_user_id(user)
        if user_id is not None:
            with db_connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO trade_sales (
                            user_id, ticker, qty, avg_buy_price, sell_price, realized_pnl_pct, sold_with_loss
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            user_id,
                            ticker,
                            float(sold_qty),
                            avg_buy_price,
                            sell_price,
                            realized_pnl_pct,
                            sold_with_loss,
                        ),
                    )
                conn.commit()
            return

    try:
        with open(_fallback_sold_trades_file(), "a", encoding="utf-8") as f:
            f.write(
                f"{(user or '').strip().lower()}|{ticker}|{float(sold_qty)}|{avg_buy_price}|{sell_price}|{realized_pnl_pct}|{int(sold_with_loss)}|{datetime.utcnow().isoformat()}\n"
            )
    except Exception:
        pass


def sell(user, t, qty, price_val=None):
    """Utför en enkel sell genom att uppdatera poster i trades-filen."""
    sell_price = float(price_val or 0)
    if sell_price <= 0:
        sell_price = float(price(t) or 0)
    requested_qty = int(float(qty))

    if db_enabled():
        user_id = get_user_id(user)
        if user_id is not None:
            remaining = requested_qty
            if remaining <= 0:
                return

            with db_connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT COALESCE(SUM(qty), 0), COALESCE(SUM(qty * price), 0)
                        FROM trades
                        WHERE user_id = %s AND ticker = %s AND side = 'BUY'
                        """,
                        (user_id, str(t).upper()),
                    )
                    basis_row = cur.fetchone() or (0, 0)
                    held_qty = int(float(basis_row[0] or 0))
                    held_cost = float(basis_row[1] or 0)
                    avg_buy_price = (held_cost / held_qty) if held_qty > 0 else 0

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
            sold_qty = requested_qty - remaining
            if sold_qty > 0:
                _record_sale_event(user, t, sold_qty, avg_buy_price, sell_price)
            return

    lines = open(FILE).readlines()
    new = []
    held_qty = 0
    held_cost = 0.0
    remaining = requested_qty
    for l in lines:
        parts = l.strip().split("|")
        if len(parts) < 4:
            continue
        u, ticker, q, p = parts
        q = int(float(q))
        p = float(p)

        if u == user and ticker == t:
            held_qty += q
            held_cost += q * p

        if u == user and ticker == t and remaining > 0:
            new_q = q - remaining
            if new_q > 0:
                new.append(f"{u}|{ticker}|{new_q}|{p}\n")
                remaining = 0
            else:
                remaining -= q
        else:
            new.append(l)

    open(FILE, "w").writelines(new)

    sold_qty = requested_qty - max(0, remaining)
    avg_buy_price = (held_cost / held_qty) if held_qty > 0 else 0
    if sold_qty > 0:
        _record_sale_event(user, t, sold_qty, avg_buy_price, sell_price)


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
