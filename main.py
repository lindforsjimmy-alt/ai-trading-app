import sys
import os
import yfinance as yf
from datetime import datetime

os.makedirs("stock_data", exist_ok=True)
open("stock_data/my_trades.txt","a").MTA PRISopen("stock_data/my_trades.txt","a").close()
def price(t):
    try:
        return yf.Ticker(t).info.get("currentPrice", 0)
    except:
        return 0


# ✅ BUY
def buy(user, t):

    p = price(t)

    if not p:
        return "Kunde inte hämta pris"

    stop = round(p * 0.92, 2)

    with open("stock_data/my_trades.txt","a") as f:
        f.write(f"{user}|{t}|{p}|{stop}|{datetime.now().date()}\n")

    return f"Köpt {t} @ {p} | Stop-loss: {stop}"


# ✅ SELL
def sell(user, t):

    remain = []
    sold = False

    for l in open("stock_data/my_trades.txt"):
        parts = l.strip().split("|")

        if len(parts) >= 2:
            u, ticker = parts[0], parts[1]

            if u == user and ticker == t:
                sold = True
            else:
                remain.append(l)

    open("stock_data/my_trades.txt","w").writelines(remain)

    return f"Sålt {t}" if sold else "Trade hittades ej"


# ✅ PORTFÖLJ MED PnL
def portfolio(user):

    output = ""

    for l in open("stock_data/my_trades.txt"):
        parts = l.strip().split("|")

        if len(parts) < 5:
            continue

        u, ticker, entry, stop, date = parts

        if u != user:
            continue

        curr = price(ticker)

        # ✅ PnL beräkning
        if curr and float(entry) != 0:
            pnl = round((curr - float(entry)) / float(entry) * 100, 1)
        else:
            pnl = 0

        # ✅ status
        status = "OK"
        if curr < float(stop):
            status = "STOP-LOSS HIT"

        output += f"{ticker}\n"
        output += f"Entry: {entry} | Now: {curr}\n"
        output += f"PnL: {pnl}% | Stop: {stop}\n"
        output += f"Status: {status}\n\n"

    return output if output else "Ingen portfölj"


# ✅ HANDLER
def handle(cmd):

    parts = cmd.split("|")

    action = parts[0]
    user = parts[1] if len(parts) > 1 else ""
    ticker = parts[2] if len(parts) > 2 else ""

    if action == "buy":
        return buy(user, ticker)

    if action == "sell":
        return sell(user, ticker)

    if action == "portfolio":
        return portfolio(user)

    return "OK"


# ✅ WEB MODE
if len(sys.argv) > 1:
    print(handle(sys.argv[1]))
    sys.exit()


# ✅ CMD TEST MODE (valfri)
while True:
    cmd = input("cmd (buy|user|ticker): ")
    print(handle(cmd))

