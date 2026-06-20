from flask import Flask, render_template_string, redirect, session, request
import subprocess
import hashlib
import os

app = Flask(__name__)
app.secret_key = "secret"

USERS_FILE = "users.txt"
open(USERS_FILE, "a").close()
open("stock_data/my_trades.txt", "a").close()


# ✅ HASH
def hash_pw(pw):
    return hashlib.sha256(pw.encode()).hexdigest()


# ✅ USERS
def load_users():
    users = {}
    for l in open(USERS_FILE):
        if "|" in l:
            u,p = l.strip().split("|")
            users[u] = p
    return users


def create_user(u,pw):
    if u in load_users():
        return False
    with open(USERS_FILE,"a") as f:
        f.write(f"{u}|{hash_pw(pw)}\n")
    return True


# ✅ DATA + AI
def load_data():

    assets=[]

    for l in open("stock_data/stocks.txt"):
        if "|" in l:
            t,name,p,_,g,plat = l.strip().split("|")

            try:
                res = subprocess.run(
                    ["python","main.py",f"signal {t}"],
                    capture_output=True,text=True
                ).stdout.strip()

                sig,risk,move = res.split("|")
            except:
                sig,risk,move="HOLD","MEDIUM","NORMAL"

            alert = "🔥 BUY SPIKE" if move=="BUY_SPIKE" else \
                    "⚠️ SELL DROP" if move=="SELL_DROP" else ""

            score = 3 if sig=="BUY" else 2 if sig=="HOLD" else 1

            assets.append({
                "t":t,"name":name,
                "p":float(p),
                "g":round(float(g)*100,1),
                "s":sig,
                "r":risk,
                "alert":alert,
                "plat":plat,
                "score":score
            })

    assets = sorted(assets, key=lambda x:(x["score"],x["g"]), reverse=True)

    stocks=[x for x in assets if x["plat"]=="AVANZA"]
    crypto=[x for x in assets if x["plat"]=="SAFELLO"]

    return stocks, crypto


# ✅ PORTFÖLJ + AI
def portfolio(user):

    items=[]

    for l in open("stock_data/my_trades.txt"):
        p = l.strip().split("|")

        if len(p) >= 4:
            u,t,entry,stop = p[0],p[1],float(p[2]),float(p[3])

            if u != user:
                continue

            try:
                curr = subprocess.run(
                    ["python","main.py",f"signal {t}"],
                    capture_output=True,text=True
                ).stdout.strip()

                sig,_,_ = curr.split("|")
            except:
                sig="HOLD"

            rec = "KÖP MER" if sig=="BUY" else "SÄLJ" if sig=="SELL" else "BEHÅLL"

            items.append({
                "t":t,
                "entry":entry,
                "stop":stop,
                "rec":rec
            })

    return items


# ✅ INVEST PLAN (EXAKT ANTAL)
def investment_plan(amount):

    stocks,_ = load_data()

