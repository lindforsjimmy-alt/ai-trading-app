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
    users = load_users()
    if u in users:
        return False

    with open(USERS_FILE,"a") as f:
        f.write(f"{u}|{hash_pw(pw)}\n")
    return True


# ✅ LOAD DATA
def load_data():

    stocks=[]
    crypto=[]

    for l in open("stock_data/stocks.txt"):
        if "|" in l:
            t,name,p,_,g,plat = l.strip().split("|")

            item={
                "t":t,
                "name":name,
                "p":p,
                "g":round(float(g)*100,1),
                "plat":plat
            }

            if plat=="SAFELLO":
                crypto.append(item)
            else:
                stocks.append(item)

    return stocks, crypto


# ✅ PORTFÖLJ PER USER
def portfolio(user):

    result=[]

    for l in open("stock_data/my_trades.txt"):
        parts = l.strip().split("|")

        if len(parts) >= 2:
            u,ticker = parts[0], parts[1]

            if u == user:
                result.append(ticker)

    return result


# ✅ LOGIN PAGE
LOGIN_HTML = """
<h2>Login</h2>

<form method="post">
<input name="username" placeholder="Username"><br>
<input name="password" type="password"><br>

<button name="action" value="login">Login</button>
<button name="action" value="create">Create</button>
</form>

<p>{{msg}}</p>
"""


# ✅ DASHBOARD
HTML = """
<h1>Trading Dashboard</h1>

<h2>Aktier</h2>
{% for s in stocks %}
{{s.name}}

<form method="post">
<input type="hidden" name="cmd" value="buy|{{user}}|{{s.t}}">
<button>Köp</button>
</form>

{% endfor %}

<h2>Krypto</h2>
{% for s in crypto %}
{{s.name}}

<form method="post">
<input type="hidden" name="cmd" value="buy|{{user}}|{{s.t}}">
<button>Köp</button>
</form>

{% endfor %}

<h2>Din portfölj</h2>
{% for s in owned %}
{{s}}

<form method="post">
<input type="hidden" name="cmd" value="sell|{{user}}|{{s}}">
<button>Sälj</button>
</form>

{% endfor %}

a href="/logout">Logout</a>
"""


@app.route("/", methods=["GET","POST"])
def login():

    msg=""

    if request.method=="POST":
        u = request.form.get("username")
        pw = request.form.get("password")
        action = request.form.get("action")

        users = load_users()

        if action=="create":
            if create_user(u,pw):
                msg="Account created"
            else:
                msg="User exists"

        elif action=="login":
            if users.get(u)==hash_pw(pw):
                session["user"]=u
                return redirect("/dashboard")
            else:
                msg="Wrong login"

    return render_template_string(LOGIN_HTML,msg=msg)


@app.route("/dashboard")
def dashboard():

    if "user" not in session:
        return redirect("/")

    s,c = load_data()
    o = portfolio(session["user"])

    return render_template_string(
        HTML,
        stocks=s,
        crypto=c,
        owned=o,
        user=session["user"]
    )


@app.route("/", methods=["POST"])
def run():

    cmd = request.form.get("cmd")

    subprocess.run(["python","main.py",cmd])

    return redirect("/dashboard")


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")
    

app.run(host="0.0.0.0", port=10000)