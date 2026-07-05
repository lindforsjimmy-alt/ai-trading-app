# -*- coding: utf-8 -*-

# ===== IMPORTS =====
import builtins as _builtins
import json
import re

from flask import Flask, redirect, session, request, render_template, send_file, jsonify
import os, requests, time, feedparser, math, hashlib, logging, secrets, string
try:
    import psycopg
except Exception:
    psycopg = None
try:
    from dotenv import load_dotenv
    load_dotenv("api.env")
except:
    print("⚠️ dotenv not available (Render OK)")
from datetime import timedelta, datetime
import finnhub
import threading
from sp500_list import SP500_SYMBOLS
from trading import buy, sell

# Make logging/prints resilient on Windows terminals using cp1252.
_ORIGINAL_PRINT = _builtins.print

def safe_print(*args, **kwargs):
    try:
        _ORIGINAL_PRINT(*args, **kwargs)
    except UnicodeEncodeError:
        sanitized = [str(a).encode("ascii", "ignore").decode("ascii") for a in args]
        _ORIGINAL_PRINT(*sanitized, **kwargs)

print = safe_print

# ===== LOGGING =====
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

# ===== CONFIG / APP SETUP =====
BASE_URL = os.environ.get("BASE_URL", "http://localhost:10000")

def _is_configured(name):
    return bool((os.environ.get(name) or "").strip())


def _email_hint(value):
    txt = (value or "").strip()
    if not txt or "@" not in txt:
        return "not-set"
    local, domain = txt.split("@", 1)
    if len(local) <= 2:
        masked_local = "*" * len(local)
    else:
        masked_local = local[:2] + ("*" * (len(local) - 2))
    return f"{masked_local}@{domain}"


def is_email_enabled():
    raw = (os.environ.get("EMAIL_ENABLED") or "1").strip().lower()
    return raw in ("1", "true", "yes", "on")


def is_alerts_enabled():
    raw = (os.environ.get("ALERTS_ENABLED") or "1").strip().lower()
    return raw in ("1", "true", "yes", "on")


finnhub_client = finnhub.Client(api_key=os.environ.get("FINNHUB_API_KEY"))
logger.info("Config status | FINNHUB_API_KEY configured: %s", _is_configured("FINNHUB_API_KEY"))
logger.info("Config status | OPENAI_API_KEY configured: %s", _is_configured("OPENAI_API_KEY"))
logger.info("Config status | DATABASE_URL configured: %s", _is_configured("DATABASE_URL"))
logger.info("Config status | BREVO_API_KEY configured: %s", _is_configured("BREVO_API_KEY"))
logger.info("Config status | BREVO_SENDER_EMAIL configured: %s", _is_configured("BREVO_SENDER_EMAIL"))
logger.info("Config status | EMAIL_ENABLED: %s", is_email_enabled())
logger.info("Config status | ALERTS_ENABLED: %s", is_alerts_enabled())
logger.info(
    "Config status | SMTP host=%s port=%s email_user=%s",
    (os.environ.get("EMAIL_HOST") or os.environ.get("SMTP_HOST") or "smtp.office365.com").strip(),
    (os.environ.get("EMAIL_PORT") or os.environ.get("SMTP_PORT") or "587").strip(),
    _email_hint(os.environ.get("EMAIL_USER") or os.environ.get("SMTP_USER") or ""),
)

app = Flask(__name__, template_folder="Templates")
app.permanent_session_lifetime = timedelta(hours=12)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "super_secret_trading_key_123")
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.jinja_env.auto_reload = True

ENABLE_BACKGROUND = os.environ.get("ENABLE_BACKGROUND", "false").lower() in ("1", "true", "yes")
ALERT_LOG_FILE = os.environ.get("ALERT_LOG_FILE", "stock_data/alerts.log")
IS_RENDER = os.environ.get("RENDER", "").strip().lower() in ("1", "true", "yes")
LEAN_MODE = os.environ.get("LEAN_MODE", "1" if IS_RENDER else "0").strip().lower() in ("1", "true", "yes")

# Starter-friendly caps to keep memory usage stable on Render.
MARKET_SYMBOL_LIMIT = 40 if LEAN_MODE else 80
SCAN_CANDIDATE_LIMIT = 80 if LEAN_MODE else 150
COINGECKO_PAGES = 1 if LEAN_MODE else 4
AI_CRYPTO_LIMIT = 12 if LEAN_MODE else 40
MAX_NEWS_CACHE_ITEMS = 200 if LEAN_MODE else 1000
MAX_COMPANY_CACHE_ITEMS = 400 if LEAN_MODE else 2000
MAX_FUNDAMENTAL_CACHE_ITEMS = 200 if LEAN_MODE else 1000
MAX_INDEX_HISTORY_CACHE_ITEMS = 24 if LEAN_MODE else 120

FUNDAMENTAL_CACHE = {}
FUNDAMENTAL_CACHE_TIME = 86400
COMPANY_NAME_CACHE = {}
FX_RATE_CACHE = {
    "fetched_at": 0.0,
    "date": "",
    "source": "",
    "rates": {},
}
FX_RATE_CACHE_TTL = 3600

# ===== DATA FILES & BASIC =====
# ===== FILES =====
DATA_FILE = "stock_data/my_trades.txt"
USERS_FILE = "stock_data/users.txt"
ADMIN_EMAILS = {"lindfors.jimmy@outlook.com"}
ADMINS_FILE = "stock_data/admins.txt"
USER_SETTINGS_FILE = "stock_data/user_settings.json"
SOLD_TRADES_FILE = "stock_data/sold_trades.txt"
USER_SETTINGS_LOCK = threading.Lock()
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()


def db_enabled():
    return bool(DATABASE_URL and psycopg is not None)


def db_connect():
    return psycopg.connect(DATABASE_URL)


def db_find_user(email):
    if not db_enabled():
        return None
    target = (email or "").strip().lower()
    if not target:
        return None
    try:
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, email, password_hash, role, status
                    FROM users
                    WHERE LOWER(email) = LOWER(%s)
                    LIMIT 1
                    """,
                    (target,),
                )
                row = cur.fetchone()
        if not row:
            return None
        return {
            "id": row[0],
            "email": row[1],
            "password_hash": row[2],
            "role": row[3],
            "status": row[4],
        }
    except Exception as ex:
        logger.warning("DB user lookup failed for %s: %s", target, ex)
        return None


def db_upsert_user(email, password_hash, status="active", role="user", platforms=None):
    if not db_enabled():
        return None
    target = (email or "").strip().lower()
    if not target:
        return None

    normalized_platforms = normalize_trading_platform_selection(platforms)
    try:
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO users (email, password_hash, role, status, approved_at)
                    VALUES (%s, %s, %s, %s, CASE WHEN %s = 'active' THEN NOW() ELSE NULL END)
                    ON CONFLICT ((LOWER(email))) DO UPDATE
                    SET password_hash = EXCLUDED.password_hash,
                        role = CASE
                            WHEN users.role = 'admin' OR EXCLUDED.role = 'admin' THEN 'admin'
                            ELSE EXCLUDED.role
                        END,
                        status = EXCLUDED.status,
                        approved_at = CASE
                            WHEN EXCLUDED.status = 'active' THEN COALESCE(users.approved_at, NOW())
                            ELSE users.approved_at
                        END
                    RETURNING id
                    """,
                    (target, password_hash, role, status, status),
                )
                user_id = cur.fetchone()[0]
                if normalized_platforms:
                    cur.execute("DELETE FROM user_trading_platforms WHERE user_id = %s", (user_id,))
                    for platform_key in normalized_platforms:
                        cur.execute(
                            """
                            INSERT INTO user_trading_platforms (user_id, platform_key)
                            VALUES (%s, %s)
                            ON CONFLICT DO NOTHING
                            """,
                            (user_id, platform_key),
                        )
            conn.commit()
        return user_id
    except Exception as ex:
        logger.warning("DB upsert user failed for %s: %s", target, ex)
        return None


def db_update_password(email, new_hash):
    if not db_enabled():
        return False
    target = (email or "").strip().lower()
    try:
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE users
                    SET password_hash = %s
                    WHERE LOWER(email) = LOWER(%s) AND status = 'active'
                    """,
                    (new_hash, target),
                )
                changed = cur.rowcount > 0
            conn.commit()
        return changed
    except Exception as ex:
        logger.warning("DB password update failed for %s: %s", target, ex)
        return False


def db_update_platforms(email, selected_platforms):
    if not db_enabled():
        return False
    target = (email or "").strip().lower()
    normalized = normalize_trading_platform_selection(selected_platforms)
    try:
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id FROM users
                    WHERE LOWER(email) = LOWER(%s) AND status = 'active'
                    LIMIT 1
                    """,
                    (target,),
                )
                row = cur.fetchone()
                if not row:
                    return False
                user_id = row[0]
                cur.execute("DELETE FROM user_trading_platforms WHERE user_id = %s", (user_id,))
                for platform_key in normalized:
                    cur.execute(
                        """
                        INSERT INTO user_trading_platforms (user_id, platform_key)
                        VALUES (%s, %s)
                        ON CONFLICT DO NOTHING
                        """,
                        (user_id, platform_key),
                    )
            conn.commit()
        return True
    except Exception as ex:
        logger.warning("DB platform update failed for %s: %s", target, ex)
        return False


def db_load_settings(email):
    if not db_enabled():
        return {}
    target = (email or "").strip().lower()
    if not target:
        return {}
    try:
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        s.amount,
                        s.capital_currency,
                        s.ai_strategy,
                        s.ai_risk,
                        s.top_n,
                        s.priority,
                        s.send_buy_alerts,
                        s.send_sell_alerts,
                        s.block_loss_sells,
                        s.pf_strategy,
                        s.pf_risk,
                        s.mintrend_index_total,
                        s.mintrend_index_recent,
                        s.mintrend_index_pl,
                        s.mintrend_index_range,
                        s.mintrend_range,
                        s.mintrend_currency
                    FROM user_settings s
                    JOIN users u ON u.id = s.user_id
                    WHERE LOWER(u.email) = LOWER(%s)
                    LIMIT 1
                    """,
                    (target,),
                )
                row = cur.fetchone()
        if not row:
            return {}
        return {
            "amount": float(row[0]),
            "capital_currency": row[1],
            "ai_strategy": row[2],
            "ai_risk": row[3],
            "top_n": int(row[4]),
            "priority": row[5],
            "send_buy_alerts": bool(row[6]),
            "send_sell_alerts": bool(row[7]),
            "block_loss_sells": bool(row[8]),
            "pf_strategy": row[9],
            "pf_risk": row[10],
            "mintrend_index_total": row[11],
            "mintrend_index_recent": row[12],
            "mintrend_index_pl": row[13],
            "mintrend_index_range": row[14],
            "mintrend_range": row[15],
            "mintrend_currency": row[16],
        }
    except Exception as ex:
        logger.warning("DB load settings failed for %s: %s", target, ex)
        return {}


def db_save_settings(email, updates):
    if not db_enabled():
        return False
    target = (email or "").strip().lower()
    if not target:
        return False

    merged = {
        "amount": 10000,
        "capital_currency": "SEK",
        "ai_strategy": "short",
        "ai_risk": "medium",
        "top_n": 5,
        "priority": "mix",
        "send_buy_alerts": False,
        "send_sell_alerts": False,
        "block_loss_sells": False,
        "pf_strategy": "short",
        "pf_risk": "medium",
        "mintrend_index_total": "STANDARD",
        "mintrend_index_recent": "STANDARD",
        "mintrend_index_pl": "STANDARD",
        "mintrend_index_range": "STANDARD",
        "mintrend_range": "1Y",
        "mintrend_currency": "USD",
    }
    merged.update(db_load_settings(target))
    merged.update(updates or {})

    currency = str(merged.get("capital_currency", "SEK")).upper()
    if currency not in {"SEK", "USD", "EUR"}:
        currency = "SEK"
    mt_currency = str(merged.get("mintrend_currency", "USD")).upper()
    if mt_currency not in {"SEK", "USD", "EUR"}:
        mt_currency = "USD"

    try:
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id FROM users WHERE LOWER(email)=LOWER(%s) LIMIT 1",
                    (target,),
                )
                row = cur.fetchone()
                if not row:
                    return False
                user_id = row[0]
                cur.execute(
                    """
                    INSERT INTO user_settings (
                        user_id, amount, capital_currency, ai_strategy, ai_risk, top_n, priority,
                        send_buy_alerts, send_sell_alerts, block_loss_sells, pf_strategy, pf_risk,
                        mintrend_index_total, mintrend_index_recent, mintrend_index_pl, mintrend_index_range,
                        mintrend_range, mintrend_currency, updated_at
                    )
                    VALUES (
                        %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s,
                        %s, %s, %s, %s,
                        %s, %s, NOW()
                    )
                    ON CONFLICT (user_id) DO UPDATE
                    SET amount = EXCLUDED.amount,
                        capital_currency = EXCLUDED.capital_currency,
                        ai_strategy = EXCLUDED.ai_strategy,
                        ai_risk = EXCLUDED.ai_risk,
                        top_n = EXCLUDED.top_n,
                        priority = EXCLUDED.priority,
                        send_buy_alerts = EXCLUDED.send_buy_alerts,
                        send_sell_alerts = EXCLUDED.send_sell_alerts,
                        block_loss_sells = EXCLUDED.block_loss_sells,
                        pf_strategy = EXCLUDED.pf_strategy,
                        pf_risk = EXCLUDED.pf_risk,
                        mintrend_index_total = EXCLUDED.mintrend_index_total,
                        mintrend_index_recent = EXCLUDED.mintrend_index_recent,
                        mintrend_index_pl = EXCLUDED.mintrend_index_pl,
                        mintrend_index_range = EXCLUDED.mintrend_index_range,
                        mintrend_range = EXCLUDED.mintrend_range,
                        mintrend_currency = EXCLUDED.mintrend_currency,
                        updated_at = NOW()
                    """,
                    (
                        user_id,
                        float(merged.get("amount", 10000) or 10000),
                        currency,
                        str(merged.get("ai_strategy", "short")),
                        str(merged.get("ai_risk", "medium")),
                        int(merged.get("top_n", 5) or 5),
                        str(merged.get("priority", "mix")),
                        bool(merged.get("send_buy_alerts", False)),
                        bool(merged.get("send_sell_alerts", False)),
                        bool(merged.get("block_loss_sells", False)),
                        str(merged.get("pf_strategy", "short")),
                        str(merged.get("pf_risk", "medium")),
                        str(merged.get("mintrend_index_total", "STANDARD")).upper(),
                        str(merged.get("mintrend_index_recent", "STANDARD")).upper(),
                        str(merged.get("mintrend_index_pl", "STANDARD")).upper(),
                        str(merged.get("mintrend_index_range", "STANDARD")).upper(),
                        str(merged.get("mintrend_range", "1Y")).upper(),
                        mt_currency,
                    ),
                )
            conn.commit()
        return True
    except Exception as ex:
        logger.warning("DB save settings failed for %s: %s", target, ex)
        return False


def ensure_runtime_schema():
    if not db_enabled():
        return

    try:
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    ALTER TABLE user_settings
                    ADD COLUMN IF NOT EXISTS block_loss_sells BOOLEAN NOT NULL DEFAULT FALSE
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS trade_sales (
                        id BIGSERIAL PRIMARY KEY,
                        user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                        ticker TEXT NOT NULL,
                        qty NUMERIC(20,8) NOT NULL CHECK (qty > 0),
                        avg_buy_price NUMERIC(20,8) NOT NULL CHECK (avg_buy_price >= 0),
                        sell_price NUMERIC(20,8) NOT NULL CHECK (sell_price >= 0),
                        realized_pnl_pct NUMERIC(12,4) NOT NULL,
                        sold_with_loss BOOLEAN NOT NULL DEFAULT FALSE,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                    """
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS ix_trade_sales_user_ticker ON trade_sales (user_id, ticker)"
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS ix_trade_sales_user_loss ON trade_sales (user_id, sold_with_loss)"
                )
            conn.commit()
    except Exception as ex:
        logger.warning("DB runtime schema ensure failed: %s", ex)


def _truthy_text(value):
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def get_loss_blocked_tickers(email):
    target = (email or "").strip().lower()
    blocked = set()

    if db_enabled() and target:
        try:
            with db_connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT DISTINCT ts.ticker
                        FROM trade_sales ts
                        JOIN users u ON u.id = ts.user_id
                        WHERE LOWER(u.email) = LOWER(%s)
                          AND ts.sold_with_loss = TRUE
                        """,
                        (target,),
                    )
                    for row in cur.fetchall():
                        ticker = (row[0] or "").strip().upper()
                        if ticker:
                            blocked.add(ticker)
        except Exception as ex:
            logger.warning("DB loss-block lookup failed for %s: %s", target, ex)

    try:
        with open(SOLD_TRADES_FILE, encoding="utf-8") as f:
            for line in f:
                parts = line.strip().split("|")
                if len(parts) < 7:
                    continue
                file_user = (parts[0] or "").strip().lower()
                ticker = (parts[1] or "").strip().upper()
                sold_with_loss = _truthy_text(parts[6])
                if ticker and sold_with_loss and file_user == target:
                    blocked.add(ticker)
    except Exception:
        pass

    return blocked


ensure_runtime_schema()

MIN_TREND_INDEX_OPTIONS = {
    "OMX": {"symbol": "^OMX", "name": "OMX Stockholm"},
    "STANDARD": {"symbol": "^GSPC", "name": "S&P 500"},
    "NASDAQ": {"symbol": "^IXIC", "name": "NASDAQ Composite"},
    "DOW": {"symbol": "^DJI", "name": "Dow Jones"},
    "RUSSELL": {"symbol": "^RUT", "name": "Russell 2000"},
}

MIN_TREND_RANGE_OPTIONS = {
    "1D": {"yahoo_range": "1d", "interval": "5m", "label": "1 dag"},
    "1W": {"yahoo_range": "5d", "interval": "30m", "label": "1 vecka"},
    "1M": {"yahoo_range": "1mo", "interval": "1d", "label": "1 månad"},
    "3M": {"yahoo_range": "3mo", "interval": "1d", "label": "3 månader"},
    "6M": {"yahoo_range": "6mo", "interval": "1d", "label": "6 månader"},
    "1Y": {"yahoo_range": "1y", "interval": "1wk", "label": "1 år"},
    "2Y": {"yahoo_range": "2y", "interval": "1wk", "label": "2 år"},
    "3Y": {"yahoo_range": "3y", "interval": "1mo", "label": "3 år"},
    "5Y": {"yahoo_range": "5y", "interval": "1mo", "label": "5 år"},
    "10Y": {"yahoo_range": "10y", "interval": "1mo", "label": "10 år"},
}

MIN_TREND_CHART_KEYS = ("total", "recent", "pl", "range")
INDEX_HISTORY_CACHE = {}
INDEX_HISTORY_CACHE_TTL = {
    "1D": 45,
    "1W": 120,
    "1M": 300,
    "3M": 600,
    "6M": 900,
    "1Y": 1800,
    "2Y": 1800,
    "3Y": 1800,
    "5Y": 3600,
    "10Y": 3600,
}

TRADING_PLATFORM_LOGIN_URLS = {
    "Avanza": "https://www.avanza.se/",
    "Nordnet": "https://www.nordnet.se/",
    "Interactive Brokers": "https://www.interactivebrokers.com/",
    "DEGIRO": "https://www.degiro.com/",
    "eToro": "https://www.etoro.com/login",
    "XTB": "https://www.xtb.com/se/logga-in",
    "Saxo Bank": "https://www.home.saxo/sv-se/login",
    "CMC Markets": "https://www.cmcmarkets.com/sv-se/logga-in",
    "IG": "https://www.ig.com/se/login",
    "Plus500": "https://www.plus500.com/sv/login",
    "Trading 212": "https://www.trading212.com/en/login",
    "Robinhood": "https://robinhood.com/login",
    "Charles Schwab": "https://client.schwab.com/",
    "Fidelity": "https://digital.fidelity.com/prgw/digital/login/full-page",
    "TD Ameritrade": "https://www.tdameritrade.com/log-in.html",
    "E*TRADE": "https://us.etrade.com/login",
    "Kraken": "https://www.kraken.com/sign-in",
    "Coinbase": "https://www.coinbase.com/signin",
    "Binance": "https://accounts.binance.com/en/login",
    "Safello": "https://safello.com/sv/",
}
DEFAULT_TRADING_PLATFORMS = ["Avanza", "Safello"]
_TRADING_PLATFORM_LOOKUP = {name.lower(): name for name in TRADING_PLATFORM_LOGIN_URLS}
CUSTOM_PLATFORM_PREFIX = "CUSTOM:"

os.makedirs("stock_data", exist_ok=True)
open(DATA_FILE, "a").close()
open(USERS_FILE, "a").close()
open(ADMINS_FILE, "a").close()
open(SOLD_TRADES_FILE, "a").close()
if not os.path.exists(USER_SETTINGS_FILE):
    with open(USER_SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump({}, f)


def load_user_settings(email):
    if db_enabled():
        db_data = db_load_settings(email)
        if db_data:
            return db_data

    target = (email or "").strip().lower()
    if not target:
        return {}

    try:
        raw = open(USER_SETTINGS_FILE, encoding="utf-8").read().strip()
        data = json.loads(raw) if raw else {}
    except Exception:
        return {}

    if not isinstance(data, dict):
        return {}

    user_settings = data.get(target, {})
    return user_settings if isinstance(user_settings, dict) else {}


def save_user_settings(email, updates):
    target = (email or "").strip().lower()
    if not target or not isinstance(updates, dict):
        return

    if db_enabled() and db_save_settings(target, updates):
        return

    with USER_SETTINGS_LOCK:
        current_data = {}
        try:
            raw = open(USER_SETTINGS_FILE, encoding="utf-8").read().strip()
            current_data = json.loads(raw) if raw else {}
        except Exception:
            current_data = {}

        if not isinstance(current_data, dict):
            current_data = {}

        user_settings = current_data.get(target, {})
        if not isinstance(user_settings, dict):
            user_settings = {}

        user_settings.update(updates)
        current_data[target] = user_settings

        with open(USER_SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(current_data, f, ensure_ascii=True, indent=2, sort_keys=True)


def trim_dict_cache(cache, max_items):
    if max_items <= 0:
        cache.clear()
        return

    while len(cache) > max_items:
        try:
            first_key = next(iter(cache))
        except StopIteration:
            return
        cache.pop(first_key, None)


def coerce_bool_setting(value, default=False):
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return value != 0

    txt = str(value).strip().lower()
    if txt in {"1", "true", "yes", "on"}:
        return True
    if txt in {"0", "false", "no", "off", ""}:
        return False
    return default

# ===== HASH =====

def hash_password(p):
    return hashlib.sha256(p.encode()).hexdigest()


def sanitize_custom_platform_name(raw_name):
    txt = (raw_name or "").strip()
    if not txt:
        return ""
    txt = txt.replace("|", " ").replace(",", " ")
    txt = " ".join(txt.split())
    return txt[:80]


def extract_custom_platform_name(platform_key):
    raw = (platform_key or "").strip()
    if raw.startswith(CUSTOM_PLATFORM_PREFIX):
        return sanitize_custom_platform_name(raw[len(CUSTOM_PLATFORM_PREFIX):])
    return ""


def normalize_trading_platform_selection(raw_values):
    out = []
    seen = set()

    if raw_values is None:
        values = []
    elif isinstance(raw_values, str):
        values = [part.strip() for part in raw_values.split(",")]
    elif isinstance(raw_values, (list, tuple, set)):
        values = [str(v).strip() for v in raw_values]
    else:
        values = [str(raw_values).strip()]

    for raw in values:
        if not raw:
            continue
        custom_name = extract_custom_platform_name(raw)
        if not custom_name and raw.lower().startswith("ovrig:"):
            custom_name = sanitize_custom_platform_name(raw.split(":", 1)[1])
        if not custom_name and raw.lower().startswith("övrig:"):
            custom_name = sanitize_custom_platform_name(raw.split(":", 1)[1])

        if custom_name:
            custom_key = f"{CUSTOM_PLATFORM_PREFIX}{custom_name}"
            if custom_key in seen:
                continue
            seen.add(custom_key)
            out.append(custom_key)
            continue

        key = _TRADING_PLATFORM_LOOKUP.get(raw.lower())
        if key:
            if key in seen:
                continue
            seen.add(key)
            out.append(key)
            continue

        free_text_custom = sanitize_custom_platform_name(raw)
        if free_text_custom:
            custom_key = f"{CUSTOM_PLATFORM_PREFIX}{free_text_custom}"
            if custom_key in seen:
                continue
            seen.add(custom_key)
            out.append(custom_key)

    if not out:
        return list(DEFAULT_TRADING_PLATFORMS)

    return out


def parse_user_record_line(raw_line):
    parts = (raw_line or "").strip().split("|")
    if len(parts) < 2:
        return None

    email = (parts[0] or "").strip().lower()
    password_hash = (parts[1] or "").strip()
    if not email or not password_hash:
        return None

    platforms_blob = (parts[2] or "").strip() if len(parts) >= 3 else ""
    platforms = normalize_trading_platform_selection(platforms_blob)

    return {
        "email": email,
        "password_hash": password_hash,
        "platforms": platforms,
    }


def pending_user_exists(email):
    if db_enabled():
        rec = db_find_user(email)
        return bool(rec and rec.get("status") == "pending")

    target = (email or "").strip().lower()
    with open(PENDING_FILE) as f:
        for raw in f:
            rec = parse_user_record_line(raw)
            if rec and rec["email"] == target:
                return True
    return False


def append_custom_platform_selection(selected_platforms, other_platform_raw):
    chosen = list(selected_platforms or [])
    other_name = sanitize_custom_platform_name(other_platform_raw)
    if other_name:
        chosen.append(f"{CUSTOM_PLATFORM_PREFIX}{other_name}")
    return normalize_trading_platform_selection(chosen)


def split_platforms_for_form(platforms):
    known = []
    other_name = ""

    for item in normalize_trading_platform_selection(platforms):
        custom_name = extract_custom_platform_name(item)
        if custom_name:
            if not other_name:
                other_name = custom_name
            continue
        if item in TRADING_PLATFORM_LOGIN_URLS:
            known.append(item)

    return known, other_name


def build_platform_links(platforms):
    links = []
    for item in normalize_trading_platform_selection(platforms):
        custom_name = extract_custom_platform_name(item)
        if custom_name:
            login_query = quote_plus(f"{custom_name} login")
            links.append({
                "name": custom_name,
                "url": f"https://www.google.com/search?q={login_query}",
            })
            continue

        url = TRADING_PLATFORM_LOGIN_URLS.get(item)
        if url:
            links.append({"name": item, "url": url})

    if not links:
        return [{"name": "Avanza", "url": TRADING_PLATFORM_LOGIN_URLS["Avanza"]}]

    return links


def build_platform_names_for_header(platforms):
    names = []
    for item in normalize_trading_platform_selection(platforms):
        custom_name = extract_custom_platform_name(item)
        if custom_name:
            names.append(custom_name)
        elif item in TRADING_PLATFORM_LOGIN_URLS:
            names.append(item)
    return names


def serialize_user_record(record):
    platforms = normalize_trading_platform_selection(record.get("platforms"))
    platform_blob = ",".join(platforms)
    return f"{record['email']}|{record['password_hash']}|{platform_blob}\n"


def dedupe_user_records_file(file_path):
    """Remove duplicate email records while preserving first valid occurrence."""
    if not os.path.exists(file_path):
        return 0

    lines = open(file_path, encoding="utf-8").readlines()
    seen_emails = set()
    output_lines = []
    removed = 0

    for raw in lines:
        rec = parse_user_record_line(raw)
        if not rec:
            output_lines.append(raw if raw.endswith("\n") else f"{raw}\n")
            continue

        email = rec["email"]
        if email in seen_emails:
            removed += 1
            continue

        seen_emails.add(email)
        output_lines.append(serialize_user_record(rec))

    if removed > 0 or len(output_lines) != len(lines):
        open(file_path, "w", encoding="utf-8").writelines(output_lines)

    return removed


def run_auth_data_self_heal():
    if db_enabled():
        return

    removed_users = dedupe_user_records_file(USERS_FILE)
    removed_pending = dedupe_user_records_file(PENDING_FILE)
    if removed_users or removed_pending:
        logger.info(
            "Auth self-heal removed duplicates | users=%s pending=%s",
            removed_users,
            removed_pending,
        )


def create_pending_user(email, password_hash, platforms=None):
    email = (email or "").strip().lower()
    if db_enabled():
        role = "admin" if email in ADMIN_EMAILS else "user"
        db_upsert_user(email, password_hash, status="pending", role=role, platforms=platforms)
        return

    rec = {
        "email": email,
        "password_hash": password_hash,
        "platforms": normalize_trading_platform_selection(platforms),
    }
    with open(PENDING_FILE, "a") as f:
        f.write(serialize_user_record(rec))


def create_user(email, password_hash, platforms=None):
    email = (email or "").strip().lower()
    if db_enabled():
        role = "admin" if email in ADMIN_EMAILS else "user"
        db_upsert_user(email, password_hash, status="active", role=role, platforms=platforms)
        return

    rec = {
        "email": email,
        "password_hash": password_hash,
        "platforms": normalize_trading_platform_selection(platforms),
    }
    with open(USERS_FILE, "a") as f:
        f.write(serialize_user_record(rec))


def load_extra_admin_emails():
    if db_enabled():
        admins = set()
        try:
            with db_connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT email FROM users WHERE role = 'admin'")
                    for row in cur.fetchall():
                        email = (row[0] or "").strip().lower()
                        if email:
                            admins.add(email)
        except Exception as ex:
            logger.warning("DB load admins failed: %s", ex)
        return admins

    admins = set()
    for raw in open(ADMINS_FILE).readlines():
        email = raw.strip().lower()
        if email:
            admins.add(email)
    return admins


def add_admin_email(email):
    target = (email or "").strip().lower()
    if not target:
        return False
    if db_enabled():
        existing = db_find_user(target)
        password_hash = existing["password_hash"] if existing else "ADMIN_ONLY_PLACEHOLDER_HASH"
        user_id = db_upsert_user(
            target,
            password_hash,
            status="active",
            role="admin",
            platforms=get_user_trading_platforms(target),
        )
        return bool(user_id)

    if target in ADMIN_EMAILS or target in load_extra_admin_emails():
        return False
    with open(ADMINS_FILE, "a") as f:
        f.write(f"{target}\n")
    return True


def is_admin_email(email):
    target = (email or "").strip().lower()
    return target in ADMIN_EMAILS or target in load_extra_admin_emails()

def check_user(email, password):
    if db_enabled():
        rec = db_find_user(email)
        if not rec:
            return False
        return rec.get("status") == "active" and rec.get("password_hash") == hash_password(password)

    target = (email or "").strip().lower()
    with open(USERS_FILE) as f:
        for l in f:
            rec = parse_user_record_line(l)
            if not rec:
                continue
            if rec["email"] == target and rec["password_hash"] == hash_password(password):
                    return True
        return False

def user_exists(email):
    if db_enabled():
        rec = db_find_user(email)
        return bool(rec and rec.get("status") == "active")

    target = (email or "").strip().lower()
    with open(USERS_FILE) as f:
        for l in f:
            rec = parse_user_record_line(l)
            if rec and rec["email"] == target:
                return True
    return False


def get_user_trading_platforms(email):
    if db_enabled():
        target = (email or "").strip().lower()
        try:
            with db_connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT p.platform_key
                        FROM user_trading_platforms p
                        JOIN users u ON u.id = p.user_id
                        WHERE LOWER(u.email) = LOWER(%s)
                        ORDER BY p.platform_key
                        """,
                        (target,),
                    )
                    values = [row[0] for row in cur.fetchall()]
            return normalize_trading_platform_selection(values)
        except Exception as ex:
            logger.warning("DB load platforms failed for %s: %s", target, ex)

    target = (email or "").strip().lower()
    with open(USERS_FILE) as f:
        for raw in f:
            rec = parse_user_record_line(raw)
            if rec and rec["email"] == target:
                return rec["platforms"]
    return list(DEFAULT_TRADING_PLATFORMS)


def load_registered_users():
    if db_enabled():
        try:
            with db_connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT email FROM users WHERE status='active' ORDER BY email")
                    return [row[0] for row in cur.fetchall() if row and row[0]]
        except Exception as ex:
            logger.warning("DB load registered users failed: %s", ex)

    users = []
    seen = set()
    for raw in open(USERS_FILE).readlines():
        rec = parse_user_record_line(raw)
        if not rec:
            continue
        email = rec["email"]
        if not email or email in seen:
            continue
        seen.add(email)
        users.append(email)
    return sorted(users)


def split_registered_users(users):
    admin_users = []
    regular_users = []
    for email in users:
        if is_admin_email(email):
            admin_users.append(email)
        else:
            regular_users.append(email)
    return regular_users, admin_users


def load_pending_users():
    if db_enabled():
        try:
            with db_connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT email FROM users WHERE status='pending' ORDER BY email")
                    return [row[0] for row in cur.fetchall() if row and row[0]]
        except Exception as ex:
            logger.warning("DB load pending users failed: %s", ex)

    users = []
    seen = set()
    for raw in open(PENDING_FILE).readlines():
        rec = parse_user_record_line(raw)
        if not rec:
            continue
        email = rec["email"]
        if not email or email in seen:
            continue
        seen.add(email)
        users.append(email)
    return sorted(users)


def approve_pending_user(email):
    """
    Returns one of: "approved", "already_registered", "not_found".
    """
    target = (email or "").strip().lower()
    if db_enabled():
        rec = db_find_user(target)
        if not rec:
            return "not_found"
        if rec.get("status") == "active":
            return "already_registered"
        try:
            with db_connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE users
                        SET status='active', approved_at=NOW()
                        WHERE LOWER(email)=LOWER(%s) AND status='pending'
                        """,
                        (target,),
                    )
                    changed = cur.rowcount > 0
                conn.commit()
            return "approved" if changed else "not_found"
        except Exception as ex:
            logger.warning("DB approve pending failed for %s: %s", target, ex)
            return "not_found"

    lines = open(PENDING_FILE).readlines()
    keep = []
    approved_record = None

    for raw in lines:
        rec = parse_user_record_line(raw)

        # Preserve unknown/legacy rows instead of silently dropping them.
        if not rec:
            keep.append(raw)
            continue

        if rec["email"] == target and approved_record is None:
            approved_record = rec
            continue

        keep.append(serialize_user_record(rec))

    open(PENDING_FILE, "w").writelines(keep)

    if not approved_record:
        return "not_found"

    if user_exists(target):
        return "already_registered"

    create_user(target, approved_record["password_hash"], approved_record["platforms"])
    return "approved"


def reject_pending_user(email):
    target = (email or "").strip().lower()
    if db_enabled():
        try:
            with db_connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "DELETE FROM users WHERE LOWER(email)=LOWER(%s) AND status='pending'",
                        (target,),
                    )
                    changed = cur.rowcount > 0
                conn.commit()
            return changed
        except Exception as ex:
            logger.warning("DB reject pending failed for %s: %s", target, ex)
            return False

    lines = open(PENDING_FILE).readlines()
    keep = []
    changed = False

    for raw in lines:
        rec = parse_user_record_line(raw)
        if not rec:
            continue
        if rec["email"] == target:
            changed = True
            continue
        keep.append(serialize_user_record(rec))

    open(PENDING_FILE, "w").writelines(keep)
    return changed


def delete_registered_user(email):
    target = (email or "").strip().lower()
    if db_enabled():
        try:
            with db_connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "DELETE FROM users WHERE LOWER(email)=LOWER(%s) AND status='active'",
                        (target,),
                    )
                    changed = cur.rowcount > 0
                conn.commit()
            return changed
        except Exception as ex:
            logger.warning("DB delete user failed for %s: %s", target, ex)
            return False

    lines = open(USERS_FILE).readlines()
    keep = []
    changed = False

    for raw in lines:
        rec = parse_user_record_line(raw)
        if not rec:
            continue
        if rec["email"] == target:
            changed = True
            continue
        keep.append(serialize_user_record(rec))

    open(USERS_FILE, "w").writelines(keep)
    return changed


def generate_temp_password(length=7):
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def build_updated_user_lines(email, new_hash):
    if db_enabled():
        return db_update_password(email, new_hash), None

    target = (email or "").strip().lower()
    updated = False
    lines = open(USERS_FILE).readlines()
    new_lines = []

    for raw in lines:
        rec = parse_user_record_line(raw)
        if not rec:
            continue
        if rec["email"] == target:
            rec["password_hash"] = new_hash
            new_lines.append(serialize_user_record(rec))
            updated = True
        else:
            new_lines.append(serialize_user_record(rec))

    return updated, new_lines


def build_updated_platform_lines(email, selected_platforms):
    if db_enabled():
        return db_update_platforms(email, selected_platforms), None

    target = (email or "").strip().lower()
    updated = False
    lines = open(USERS_FILE).readlines()
    new_lines = []
    normalized = normalize_trading_platform_selection(selected_platforms)

    for raw in lines:
        rec = parse_user_record_line(raw)
        if not rec:
            continue
        if rec["email"] == target:
            rec["platforms"] = normalized
            updated = True
        new_lines.append(serialize_user_record(rec))

    return updated, new_lines

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

ai_background_state = {
    "running": False,
    "last_start": 0.0,
}
AI_BACKGROUND_COOLDOWN = 30
AI_BACKGROUND_LOCK = threading.Lock()

alert_cache = {}

AI_REFRESH_TIME = 86400  # 24 timmar (sekunder)


def ensure_ai_background_loading(strategy="short", risk="medium", capital=10000):
    """Kick off a single background AI refresh when cache is empty."""
    if ai_results_cache.get("data") or ai_cache.get("data"):
        return False

    now = time.time()
    with AI_BACKGROUND_LOCK:
        if ai_background_state["running"]:
            return False
        if now - float(ai_background_state.get("last_start", 0.0)) < AI_BACKGROUND_COOLDOWN:
            return False

        ai_background_state["running"] = True
        ai_background_state["last_start"] = now

    def _worker():
        try:
            safe_fetch(lambda: run_daily_ai(strategy, risk, capital))
        finally:
            with AI_BACKGROUND_LOCK:
                ai_background_state["running"] = False

    threading.Thread(target=_worker, daemon=True).start()
    return True

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

    except Exception as ex:
        logger.warning("Finnhub fetch failed for %s: %s", symbol, ex)
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

# ===== FINNHUB FUNDAMENTAL DATA =====
def get_company_profile(symbol):
    try:
        profile = finnhub_client.company_profile2(symbol=symbol)
        return profile or {}
    except Exception as ex:
        logger.warning("Finnhub profile fetch failed for %s: %s", symbol, ex)
        return {}


def get_company_metrics(symbol):
    try:
        metrics = finnhub_client.company_basic_financials(symbol, "all")
        return metrics or {}
    except Exception as ex:
        logger.warning("Finnhub metrics fetch failed for %s: %s", symbol, ex)
        return {}


def get_asset_display_name(symbol):
    if symbol in COMPANY_NAME_CACHE:
        return COMPANY_NAME_CACHE[symbol]

    profile = get_company_profile(symbol)
    company_name = profile.get("name")

    if not company_name:
        try:
            url = f"https://query1.finance.yahoo.com/v7/finance/quote?symbols={symbol}"
            headers = {
                "User-Agent": "Mozilla/5.0",
                "Accept": "application/json",
                "Referer": "https://finance.yahoo.com/"
            }
            r = requests.get(url, headers=headers, timeout=5)
            if r.status_code == 200:
                data = r.json()
                result = data.get("quoteResponse", {}).get("result", [])
                if result:
                    company_name = result[0].get("longName") or result[0].get("shortName")
        except Exception as ex:
            logger.debug("Yahoo name fetch failed for %s: %s", symbol, ex)

    if not company_name:
        COMPANY_NAME_CACHE[symbol] = None
        trim_dict_cache(COMPANY_NAME_CACHE, MAX_COMPANY_CACHE_ITEMS)
        return None

    if symbol.upper() in company_name:
        display_name = company_name
    else:
        display_name = f"{company_name} ({symbol})"

    COMPANY_NAME_CACHE[symbol] = display_name
    trim_dict_cache(COMPANY_NAME_CACHE, MAX_COMPANY_CACHE_ITEMS)
    return display_name


def get_fundamental_summary(symbol):
    now = time.time()
    if symbol in FUNDAMENTAL_CACHE:
        summary, ts = FUNDAMENTAL_CACHE[symbol]
        if now - ts < FUNDAMENTAL_CACHE_TIME:
            return summary

    profile = get_company_profile(symbol)
    metrics = get_company_metrics(symbol)
    summary = {
        "name": profile.get("name", symbol),
        "industry": profile.get("finnhubIndustry", "N/A"),
        "marketCapitalization": profile.get("marketCapitalization"),
        "beta": profile.get("beta"),
        "country": profile.get("country"),
        "weburl": profile.get("weburl"),
        "logo": profile.get("logo"),
    }

    metric_data = metrics.get("metric", {}) if isinstance(metrics, dict) else {}
    summary.update({
        "peRatio": metric_data.get("peBasicExclExtraTTM"),
        "pbRatio": metric_data.get("pbAnnual"),
        "grossMargin": metric_data.get("grossMarginAnnual"),
        "netProfitMargin": metric_data.get("netProfitMarginAnnual"),
        "revenueGrowth": metric_data.get("revenueGrowth"),
        "eps": metric_data.get("epsTTM"),
    })

    FUNDAMENTAL_CACHE[symbol] = (summary, now)
    trim_dict_cache(FUNDAMENTAL_CACHE, MAX_FUNDAMENTAL_CACHE_ITEMS)
    return summary


def enrich_with_fundamentals(assets):
    for s in assets:
        if s.get("type") == "stock":
            s["fundamentals"] = get_fundamental_summary(s["t"])
    return assets


def get_news_triggers(t):
    now = time.time()

    if t in news_cache:
        data, ts = news_cache[t]
        if now - ts < NEWS_CACHE_TIME:
            return data

    triggers = {
        "keywords": [],
        "score": 0,
        "summary": []
    }
    keywords = ["launch", "announces", "partnership", "agreement", "funding", "acquisition", "expansion", "growth", "upgrade", "collaboration", "deal"]
    try:
        feed = feedparser.parse(f"https://news.google.com/rss/search?q={t}")
        entries = getattr(feed, "entries", [])
        for e in entries[:10]:
            txt = (e.title + " " + e.get("summary", "")).lower()
            for kw in keywords:
                if kw in txt and kw not in triggers["keywords"]:
                    triggers["keywords"].append(kw)
                    triggers["score"] += 1
                    triggers["summary"].append(f"{kw} i nyhetsrubrik")
        news_cache[t] = (triggers, now)
        trim_dict_cache(news_cache, MAX_NEWS_CACHE_ITEMS)
        return triggers
    except Exception as ex:
        logger.warning("News trigger fetch failed for %s: %s", t, ex)
        return triggers


def get_news_score(t, allow_network=True):
    now = time.time()

    if t in news_cache:
        score, ts = news_cache[t]
        if now - ts < NEWS_CACHE_TIME:
            if isinstance(score, dict):
                return score.get("score", 0)
            return score

    # In web request paths (e.g. dashboard tab switches), avoid blocking
    # external calls and use neutral fallback when cache is cold.
    if not allow_network:
        return 0

    data = get_news_triggers(t)
    return data.get("score", 0)


def get_news_sources(t, limit=5, allow_network=True):
    """
    Extracts news sources with URLs and sentiment for a given symbol.
    Returns a list of news items with title, URL, and sentiment.
    """
    sources = []
    if not allow_network:
        return sources

    positive_keywords = ["up", "gain", "profit", "growth", "rise", "surge", "bull", "positive", "strong", "upgrade"]
    negative_keywords = ["down", "loss", "decline", "fall", "drop", "bear", "negative", "weak", "downgrade", "warning"]
    
    try:
        feed = feedparser.parse(f"https://news.google.com/rss/search?q={t}")
        entries = getattr(feed, "entries", [])
        
        for e in entries[:limit]:
            title = e.get("title", "N/A")
            link = e.get("link", "")
            summary = e.get("summary", "").lower()
            
            # Determine sentiment
            sentiment = "neutral"
            if any(kw in summary for kw in positive_keywords):
                sentiment = "positive"
            elif any(kw in summary for kw in negative_keywords):
                sentiment = "negative"
            
            sources.append({
                "title": title,
                "url": link,
                "sentiment": sentiment
            })
    
    except Exception as ex:
        logger.warning("News sources fetch failed for %s: %s", t, ex)
    
    return sources


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

def market_scanner():

    symbols = get_global_stock_universe()

    candidates = []

    for sym in symbols:

        # ✅ tillåt fler aktier
        if len(sym) > 8:
            continue

        candidates.append(sym)

    candidates = candidates[:SCAN_CANDIDATE_LIMIT]

    print(f"✅ Scanner hittade {len(candidates)} kandidater")

    return candidates

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
            "symbol": sym,
            "name": sym,
            "display_name": get_asset_display_name(sym),
            "price": p,
            "currency": "USD",
            "type": "stock"
        })

    return assets

# ✅ CRYPTO (CoinGecko)
def get_crypto_assets():
    assets = []

    for page in range(1, COINGECKO_PAGES + 1):
        try:
            data = requests.get(
                f"https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd&per_page=250&page={page}"
            ).json()

            for c in data:
                
                symbol = c["symbol"].upper()
                assets.append({
                    "t": symbol,
                    "symbol": symbol,
                    "name": c["name"],
                    "display_name": f"{c['name']} ({symbol})",
                    "price": c["current_price"],
                    "volume": c.get("total_volume", 0),
                    "currency": "USD",
                    "type": "crypto"
                })
                
        except:
            continue

    return assets


def _analysis_key(value):
    return re.sub(r"[^A-Z0-9]", "", (value or "").upper())


def resolve_analysis_symbol(raw_query):
    query = (raw_query or "").strip()
    if not query:
        return None

    query_key = _analysis_key(query)
    if not query_key:
        return None

    symbol_lookup = {}
    for symbol in get_global_stock_universe():
        symbol_lookup.setdefault(_analysis_key(symbol), symbol)

    direct_match = symbol_lookup.get(query_key)
    if direct_match:
        return direct_match

    lower_query = query.lower()
    for symbol in SP500_SYMBOLS:
        display_name = get_asset_display_name(symbol) or symbol
        display_key = _analysis_key(display_name)
        if lower_query == symbol.lower() or lower_query in display_name.lower() or query_key in display_key:
            return symbol

    yahoo_symbol = search_yahoo_analysis_symbol(query)
    if yahoo_symbol:
        return yahoo_symbol

    return None


def extract_close_prices(hist_data):
    try:
        prices = (
            hist_data.get("chart", {})
            .get("result", [{}])[0]
            .get("indicators", {})
            .get("quote", [{}])[0]
            .get("close", [])
        )
    except Exception:
        return []

    return [float(price) for price in prices if isinstance(price, (int, float)) and price > 0]


def search_yahoo_analysis_symbol(query):
    raw_query = (query or "").strip()
    if not raw_query:
        return None

    try:
        response = requests.get(
            "https://query1.finance.yahoo.com/v1/finance/search",
            params={
                "q": raw_query,
                "quotesCount": 10,
                "newsCount": 0,
                "lang": "en-US",
                "region": "US",
            },
            headers={
                "User-Agent": "Mozilla/5.0",
                "Accept": "application/json",
                "Referer": "https://finance.yahoo.com/",
            },
            timeout=6,
        )
        if response.status_code != 200:
            return None

        payload = response.json() or {}
        quotes = payload.get("quotes", []) or []
        normalized_query = _analysis_key(raw_query)
        fallback_symbol = None

        for quote in quotes:
            symbol = (quote.get("symbol") or "").strip()
            if not symbol:
                continue

            long_name = (quote.get("longname") or quote.get("shortname") or quote.get("name") or "").strip()
            quote_type = (quote.get("quoteType") or "").strip().lower()
            exchange = (quote.get("exchange") or "").strip().lower()
            if _analysis_key(symbol) == normalized_query:
                return symbol
            if normalized_query and normalized_query in _analysis_key(long_name):
                return symbol
            if fallback_symbol is None and quote_type in {"equity", "etf"} and exchange not in {"", "pnk"}:
                fallback_symbol = symbol

        return fallback_symbol
    except Exception as ex:
        logger.debug("Yahoo search fallback failed for %s: %s", raw_query, ex)
        return None


def get_analysis_intent_meta(raw_action):
    action = (raw_action or "kopa").strip().lower()
    meta = {
        "kopa": {
            "label": "Funderar på att köpa",
            "short_label": "Köp",
        },
        "sjalja_innehav": {
            "label": "Sälja innehav på handelsplattform",
            "short_label": "Sälj innehav",
        },
        "kopa_mer": {
            "label": "Funderar på att köpa mer på handelsplattform",
            "short_label": "Köp mer",
        },
    }
    return action, meta.get(action, meta["kopa"])


def build_manual_analysis_row(raw_query, requested_action):
    query = (raw_query or "").strip()
    action, action_meta = get_analysis_intent_meta(requested_action)
    requested_label = action_meta["label"]

    if not query:
        return {
            "query": "",
            "requested_action": requested_label,
            "requested_action_value": action,
            "status": "empty",
        }

    symbol = resolve_analysis_symbol(query)
    if not symbol:
        return {
            "query": query,
            "requested_action": requested_label,
            "requested_action_value": action,
            "status": "not_found",
            "message": "Hittade ingen träff för den här raden. Testa ticker eller företagsnamn.",
        }

    price_data = get_price(symbol)
    if not price_data:
        return {
            "query": query,
            "symbol": symbol,
            "display_name": get_asset_display_name(symbol) or symbol,
            "requested_action": requested_label,
            "requested_action_value": action,
            "status": "no_price",
            "message": "Kunde inte hämta prisdata för den här tillgången just nu.",
        }

    if isinstance(price_data, dict):
        price = price_data.get("price") or 0
        volume = price_data.get("volume") or 0
    else:
        price = price_data or 0
        volume = 0

    hist_data = get_historical_data(symbol, "3mo")
    prices = extract_close_prices(hist_data) if hist_data else []

    signal = get_signal(price)
    score = get_score(signal, price, symbol)
    asset = {
        "t": symbol,
        "price": price,
        "score": score,
        "signal": signal,
        "volume": volume,
        "type": "stock",
    }
    reason = get_reason(signal, price, symbol, asset)
    summary = get_summary(asset)
    news_score = get_news_score(symbol)

    if signal == "KÖP":
        analysis_html = generate_investment_analysis(asset, prices=prices)
    else:
        asset["trigger_score"] = max(0, news_score)
        analysis_html = generate_watch_analysis(asset)

    if action == "kopa":
        if signal == "KÖP":
            ai_recommendation = "Funderar på att köpa"
            alignment = "AI ser stöd för ett köp på den här nivån."
        elif signal == "SÄLJ":
            ai_recommendation = "Avvakta med att köpa"
            alignment = "AI avråder från köp just nu eftersom signalen lutar svagare."
        else:
            ai_recommendation = "Funderar på att köpa, men signalen är svag"
            alignment = "AI vill se starkare bekräftelse innan köp."
    elif action == "sjalja_innehav":
        if signal == "SÄLJ":
            ai_recommendation = "Sälja innehav på handelsplattform"
            alignment = "AI ser stöd för att minska eller sälja innehavet."
        elif signal == "KÖP":
            ai_recommendation = "Avvakta med försäljning"
            alignment = "AI ser inte tillräckligt svag bild för att sälja nu."
        else:
            ai_recommendation = "Sälja innehav kan övervägas, men signalen är neutral"
            alignment = "AI vill se tydligare svaghet innan försäljning."
    else:
        if signal == "KÖP":
            ai_recommendation = "Funderar på att köpa mer på handelsplattform"
            alignment = "AI ser stöd för att öka innehavet."
        elif signal == "SÄLJ":
            ai_recommendation = "Avvakta med att köpa mer"
            alignment = "AI avråder från att öka positionen nu."
        else:
            ai_recommendation = "Köpa mer saknar tydligt stöd just nu"
            alignment = "AI vill se starkare momentum innan du ökar."

    display_name = get_asset_display_name(symbol) or symbol

    return {
        "query": query,
        "symbol": symbol,
        "display_name": display_name,
        "requested_action": requested_label,
        "requested_action_value": action,
        "status": "ok",
        "price": price,
        "volume": volume,
        "signal": signal,
        "score": score,
        "reason": reason,
        "summary": summary,
        "news_score": news_score,
        "trigger_score": max(0.0, float(news_score or 0)),
        "alignment": alignment,
        "analysis_html": analysis_html,
        "recommended_action": ai_recommendation,
        "recommended_qty": 0,
        "recommended_usd": 0.0,
        "recommended_sek": 0.0,
        "allocation_share_pct": 0,
    }


def apply_manual_analysis_buy_plan(analysis_rows, capital_amount_value, capital_currency):
    fx_rates = get_usd_fx_rates()
    usd_sek_rate = float(fx_rates.get("SEK", 10.5))
    usd_eur_rate = float(fx_rates.get("EUR", 0.92))
    capital_amount = parse_capital_amount(capital_amount_value, 10000)
    risk_profile = (session.get("ai_risk") or "medium").lower()

    total_capital_usd = convert_capital_to_usd(
        capital_amount,
        capital_currency,
        usd_sek_rate,
        usd_eur_rate,
    )

    buy_candidates = []
    for row in analysis_rows:
        row["recommended_qty"] = 0
        row["recommended_usd"] = 0.0
        row["recommended_sek"] = 0.0
        row["allocation_share_pct"] = 0

        if row.get("status") == "ok" and row.get("signal") == "KÖP":
            row["trigger_score"] = max(0.0, float(row.get("trigger_score") or row.get("news_score") or 0))
            buy_candidates.append(row)

    enrich_with_buy_plan(buy_candidates, total_capital_usd, usd_sek_rate, risk_profile)
    return analysis_rows


def build_analysis_cost_summary(analysis_rows, display_currency, usd_sek_rate, usd_eur_rate):
    ccy = (display_currency or "SEK").upper()
    if ccy not in {"SEK", "USD", "EUR"}:
        ccy = "SEK"

    total_recommended_qty = 0
    total_recommended_usd = 0.0

    for row in analysis_rows:
        if row.get("status") != "ok":
            continue

        qty = int(row.get("recommended_qty") or 0)
        price = float(row.get("price") or 0)
        total_usd = round(qty * price, 2)
        total_display = round(convert_usd_to_currency(total_usd, ccy, usd_sek_rate, usd_eur_rate), 2)
        total_sek = round(convert_usd_to_currency(total_usd, "SEK", usd_sek_rate, usd_eur_rate), 2)

        row["recommended_total_cost_usd"] = total_usd
        row["recommended_total_cost_display"] = total_display
        row["recommended_total_cost_sek"] = total_sek
        row["recommended_total_cost_currency"] = ccy
        row["price_display"] = round(convert_usd_to_currency(price, ccy, usd_sek_rate, usd_eur_rate), 2)
        row["price_sek"] = round(convert_usd_to_currency(price, "SEK", usd_sek_rate, usd_eur_rate), 2)

        total_recommended_qty += qty
        total_recommended_usd += total_usd

    total_display = round(convert_usd_to_currency(total_recommended_usd, ccy, usd_sek_rate, usd_eur_rate), 2)
    total_sek = round(convert_usd_to_currency(total_recommended_usd, "SEK", usd_sek_rate, usd_eur_rate), 2)

    return {
        "currency": ccy,
        "total_recommended_qty": total_recommended_qty,
        "total_recommended_usd": round(total_recommended_usd, 2),
        "total_recommended_display": total_display,
        "total_recommended_sek": total_sek,
    }

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
    symbols = symbols[:MARKET_SYMBOL_LIMIT]

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
    symbols = market_scanner()

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
def get_signal(price, score=None):

    if score is not None:
        if score >= 72:
            return "KÖP"
        elif score >= 55:
            return "AVVAKTA KÖP"
        elif score <= 35:
            return "SÄLJ"
        return "AVVAKTA"

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
    asset_type = s.get("type")

    # ✅ FIX price dict
    if isinstance(price, dict):
        price = price.get("price", 0)

    # ✅ STOCK LOGIK
    if asset_type == "stock":

        if price < 1:
            return False

        # Yahoo volume = 0 → ignorera
        if volume and volume < 200_000:
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
    asset_type = (s or {}).get("type") if isinstance(s, dict) else None
    if asset_type == "crypto" or (asset_type is None and len(t) > 5 and "." not in t and "-" not in t):
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
def get_usd_fx_rates(force_refresh=False):
    now = time.time()
    today_utc = datetime.utcnow().strftime("%Y-%m-%d")

    cached_rates = FX_RATE_CACHE.get("rates") or {}
    cache_is_fresh = (
        not force_refresh
        and cached_rates.get("SEK")
        and cached_rates.get("EUR")
        and (now - float(FX_RATE_CACHE.get("fetched_at") or 0)) <= FX_RATE_CACHE_TTL
    )
    if cache_is_fresh:
        return {
            "SEK": float(cached_rates["SEK"]),
            "EUR": float(cached_rates["EUR"]),
            "date": FX_RATE_CACHE.get("date") or today_utc,
            "source": FX_RATE_CACHE.get("source") or "cache",
        }

    # Primary source: Frankfurter (ECB-based daily rates).
    try:
        res = requests.get(
            "https://api.frankfurter.app/latest?from=USD&to=SEK,EUR",
            timeout=6,
        )
        if res.status_code == 200:
            payload = res.json()
            rates = payload.get("rates", {})
            sek = float(rates.get("SEK", 0))
            eur = float(rates.get("EUR", 0))
            if sek > 0 and eur > 0:
                FX_RATE_CACHE["fetched_at"] = now
                FX_RATE_CACHE["date"] = payload.get("date") or today_utc
                FX_RATE_CACHE["source"] = "frankfurter"
                FX_RATE_CACHE["rates"] = {"SEK": sek, "EUR": eur}
                return {
                    "SEK": sek,
                    "EUR": eur,
                    "date": FX_RATE_CACHE["date"],
                    "source": FX_RATE_CACHE["source"],
                }
    except Exception as ex:
        logger.warning("FX fetch failed (frankfurter): %s", ex)

    # Fallback source: open.er-api
    try:
        res = requests.get("https://open.er-api.com/v6/latest/USD", timeout=6)
        if res.status_code == 200:
            payload = res.json()
            rates = payload.get("rates", {})
            sek = float(rates.get("SEK", 0))
            eur = float(rates.get("EUR", 0))
            if sek > 0 and eur > 0:
                FX_RATE_CACHE["fetched_at"] = now
                FX_RATE_CACHE["date"] = payload.get("time_last_update_utc") or today_utc
                FX_RATE_CACHE["source"] = "open.er-api"
                FX_RATE_CACHE["rates"] = {"SEK": sek, "EUR": eur}
                return {
                    "SEK": sek,
                    "EUR": eur,
                    "date": today_utc,
                    "source": FX_RATE_CACHE["source"],
                }
    except Exception as ex:
        logger.warning("FX fetch failed (open.er-api): %s", ex)

    # Last-resort fallback: use latest cached rate if available.
    if cached_rates.get("SEK") and cached_rates.get("EUR"):
        return {
            "SEK": float(cached_rates["SEK"]),
            "EUR": float(cached_rates["EUR"]),
            "date": FX_RATE_CACHE.get("date") or today_utc,
            "source": FX_RATE_CACHE.get("source") or "cache",
        }

    return {
        "SEK": 10.5,
        "EUR": 0.92,
        "date": today_utc,
        "source": "fallback-static",
    }


def get_usd_sek():
    return float(get_usd_fx_rates().get("SEK", 10.5))


def get_usd_eur():
    return float(get_usd_fx_rates().get("EUR", 0.92))


def get_cached_or_fallback_fx_rates():
    cached_rates = FX_RATE_CACHE.get("rates") or {}
    if cached_rates.get("SEK") and cached_rates.get("EUR"):
        return {
            "SEK": float(cached_rates["SEK"]),
            "EUR": float(cached_rates["EUR"]),
            "date": FX_RATE_CACHE.get("date") or datetime.utcnow().strftime("%Y-%m-%d"),
            "source": FX_RATE_CACHE.get("source") or "cache",
        }
    return {
        "SEK": 10.5,
        "EUR": 0.92,
        "date": datetime.utcnow().strftime("%Y-%m-%d"),
        "source": "fallback-static",
    }


def normalize_fx_date_label(raw_date):
    value = (raw_date or "").strip()
    if not value:
        return datetime.utcnow().strftime("%Y-%m-%d")

    if len(value) >= 10 and value[4] == "-" and value[7] == "-":
        return value[:10]

    try:
        parsed = datetime.strptime(value, "%a, %d %b %Y %H:%M:%S %z")
        return parsed.strftime("%Y-%m-%d")
    except Exception:
        return datetime.utcnow().strftime("%Y-%m-%d")


def build_fx_info(fx_rates):
    source_key = (fx_rates or {}).get("source") or "fallback-static"
    source_map = {
        "frankfurter": "Frankfurter (ECB)",
        "open.er-api": "Open ER API",
        "cache": "Cache",
        "fallback-static": "Statisk fallback",
    }
    source_label = source_map.get(source_key, source_key)

    date_label = normalize_fx_date_label((fx_rates or {}).get("date"))

    return {
        "source_key": source_key,
        "source_label": source_label,
        "date_label": date_label,
    }

# ===== AI DAILY SCAN =====
def run_daily_ai(strategy="short", risk="medium", capital=10000):

    now = time.time()

    # ✅ cache
    if ai_cache["data"] and now - ai_cache["last_run"] < AI_REFRESH_TIME:
        return ai_cache["data"]

    print("🔄 Running AI daily scan...")

    result = []
  
    symbols = market_scanner()
    assets = get_stock_assets(symbols)
    assets += get_crypto_assets()[:AI_CRYPTO_LIMIT]

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

        sig_base = get_signal(price)
       
        if isinstance(price, dict):
            price = price.get("price", 0)

        hist = get_historical_data(s["t"], "3mo")

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
        base = 80 if sig_base == "KÖP" else 60 if sig_base == "AVVAKTA KÖP" else 30

        total_score = (
            base
            + (trend_score * trend_weight)
            + (rsi_score * rsi_weight)
            + (news_score * news_weight)
            + (ma_score * ma_weight)

        )

        if s.get("type") == "stock":
            total_score += 5

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
                total_score -= 2

            if rsi_score < 0:
                total_score -= 2

        elif strategy == "long":

            if trend_score < 0:
                total_score -= 3

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
            total_score -= 2

        s["score"] = max(0, min(100, int(total_score)))
        s["signal"] = get_signal(price, s["score"])
        s["reason"] = get_reason(s["signal"], price, s["t"], s)
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

    stock_results = [x for x in result if x.get("type") == "stock"]

    if len(stock_results) > 0:
        result = stock_results[:10] + result

    result = [s for s in result if s["price"] > 0]

    signal_counts = {"KÖP": 0, "AVVAKTA KÖP": 0, "AVVAKTA": 0, "SÄLJ": 0}
    for item in result:
        signal = item.get("signal", "AVVAKTA")
        signal_counts[signal] = signal_counts.get(signal, 0) + 1

    logger.info(
        "AI signal distribution | KÖP=%s AVVAKTA_KÖP=%s AVVAKTA=%s SÄLJ=%s total=%s",
        signal_counts.get("KÖP", 0),
        signal_counts.get("AVVAKTA KÖP", 0),
        signal_counts.get("AVVAKTA", 0),
        signal_counts.get("SÄLJ", 0),
        len(result),
    )

    print("---- DEBUG TOP ASSETS ----")
    for s in result[:15]:
        print(s["t"], "| price:", s.get("price"), "| score:", s.get("score"), "| signal:", s.get("signal"))
    print("--------------------------")

    # ✅ cache
    ai_cache["data"] = result
    ai_cache["last_run"] = now
    ai_results_cache["data"] = result
    return result


# ===== INVESTMENT ANALYSIS GENERATOR =====
def generate_investment_analysis(s, prices=None):
    """
    Generates a detailed investment analysis in Swedish when AI recommends BUY.
    Returns formatted HTML text with all analysis sections.
    """
    
    symbol = s.get("t", "N/A")
    price = s.get("price", 0)
    score = s.get("score", 0)
    signal = s.get("signal", "")
    news_score = get_news_score(symbol)
    
    if prices is None:
        prices = []
    
    # ===== CALCULATE INDICATORS =====
    trend_score = get_trend_score_from_history(prices) if prices else 0
    rsi_score = get_rsi_score_from_history(prices) if prices else 0
    rsi_value = calculate_rsi(prices) if prices else 50
    ma_score = get_ma_score(prices) if prices else 0
    
    # Calculate moving averages for display
    ma50 = calculate_ma(prices, 50) if prices else None
    ma200 = calculate_ma(prices, 200) if prices else None
    ma50_text = f"{ma50:.2f}" if isinstance(ma50, (int, float)) else "N/A"
    ma200_text = f"{ma200:.2f}" if isinstance(ma200, (int, float)) else "N/A"
    
    # Calculate price change
    price_change_3mo = 0
    if prices and len(prices) > 0:
        first_price = next((p for p in prices if p > 0), None)
        if first_price and price > 0:
            price_change_3mo = ((price - first_price) / first_price) * 100
    
    # ===== TREND TEXT =====
    if trend_score > 1:
        trend_text = "Stark uppåtgående trend"
        trend_detail = f"Priset stiger konsekvent. MA50 ({ma50_text}) ligger över MA200 ({ma200_text})."
    elif trend_score > 0:
        trend_text = "Mild uppåtgående trend"
        trend_detail = f"Priset visar positiv riktning med MA50 på {ma50_text}."
    else:
        trend_text = "Neutral trend"
        trend_detail = f"Priset rör sig sidledes med begränsade välutvecklingar."
    
    # ===== MOMENTUM TEXT =====
    if rsi_value < 30:
        momentum_text = "🔴 RSI visar översålt läge – potentiell reversal"
        momentum_detail = f"RSI: {rsi_value:.0f} (under 30 = överköpt)"
    elif rsi_value < 50:
        momentum_text = f"🟡 Mild köpmomentum – RSI: {rsi_value:.0f}"
        momentum_detail = "Momentum bygger men inte överdrivet"
    elif rsi_value < 70:
        momentum_text = f"🟢 Starkt köpmomentum – RSI: {rsi_value:.0f}"
        momentum_detail = "RSI nära overbought men fortfarande hälsosamt"
    else:
        momentum_text = f"⚠️ Överköpt läge – RSI: {rsi_value:.0f}"
        momentum_detail = "RSI över 70 kan indikera kursnedgång"
    
    # ===== NEWS ANALYSIS =====
    if news_score > 1:
        news_text = "😊 Övervägande positiva nyheter"
    elif news_score > 0:
        news_text = "😐 Något positiv nyhetstrend"
    elif news_score < -1:
        news_text = "😞 Övervägande negativa nyheter"
    else:
        news_text = "➖ Neutral nyhetssituation"
    
    # ===== RISK ASSESSMENT =====
    volume = s.get("volume", 0)
    risk_factors = []
    risk_level = "Låg"
    
    if volume < 1_000_000:
        risk_factors.append("Låg handelsvolym")
        risk_level = "Medel-Hög"
    elif volume < 10_000_000:
        risk_factors.append("Begränsad likviditet")
        risk_level = "Medel"
    
    if s.get("type") == "crypto":
        risk_factors.append("Crypto-volatilitet")
        risk_level = "Hög"
    
    if price > 500:
        risk_factors.append("Högt absolut pris")
    
    if rsi_value > 70:
        risk_factors.append("Överköpt RSI")
    
    if news_score < 0:
        risk_factors.append("Negativ nyhetstrenning")
    
    risk_text = ", ".join(risk_factors) if risk_factors else "Låg risknivå"
    
    # ===== GET NEWS SOURCES =====
    # Avoid blocking request path with live RSS fetch on constrained hosts.
    news_sources = get_news_sources(symbol, limit=3, allow_network=False)
    news_html = ""
    if news_sources:
        news_html = "<strong>Nyhetskällor:</strong><br>"
        for src in news_sources:
            emoji = "🟢" if src["sentiment"] == "positive" else "🔴" if src["sentiment"] == "negative" else "🟡"
            news_html += f'{emoji} <a href="{src["url"]}" target="_blank" style="color:#70E000;text-decoration:none;font-size:0.9rem;">{src["title"][:60]}...</a><br>'
    
    # ===== GENERATE ANALYSIS AS HTML =====
    analysis_html = f"""
<div style="margin-bottom: 14px;">
    <strong style="font-size:1.05rem;">📌 Sammanfattning</strong><br>
    <span style="font-size:0.95rem;">AI bedömer att <strong>{symbol}</strong> har stark köppotential. AI-confidence: <strong>{score}%</strong> baserat på teknisk analys, momentum och nyhetsflöde. Pristrend 3 mån: <strong style="color:#70E000;">+{price_change_3mo:.1f}%</strong></span>
</div>

<div style="margin-bottom: 14px;">
    <strong style="font-size:1.05rem;">📈 Trendanalys</strong><br>
    <span style="font-size:0.95rem;">{trend_text}. {trend_detail}</span>
</div>

<div style="margin-bottom: 14px;">
    <strong style="font-size:1.05rem;">🧠 Momentumanalys</strong><br>
    <span style="font-size:0.95rem;">{momentum_text}<br>{momentum_detail}</span>
</div>

<div style="margin-bottom: 14px;">
    <strong style="font-size:1.05rem;">📰 Nyhetsanalys</strong><br>
    <span style="font-size:0.95rem;">{news_text}<br>
    {news_html}
    </span>
</div>

<div style="margin-bottom: 14px;">
    <strong style="font-size:1.05rem;">⚠️ Riskanalys</strong><br>
    <span style="font-size:0.95rem;">Risknivå: <strong>{risk_level}</strong><br>Risk-faktorer: {risk_text}</span>
</div>

<div style="margin-bottom: 14px;">
    <strong style="font-size:1.05rem;">🎯 Slutsats</strong><br>
    <span style="font-size:0.95rem;">Tekniska indikatorer pekar uppåt. Priset visar styrka med positiv 3-månadstrend. Rekommenderad positionsstorlek: Moderat (begränsa exponering enligt din risktolerans).</span>
</div>

<div style="margin-bottom: 14px;">
    <strong style="font-size:1.05rem;">💡 Beslutsmotivering</strong><br>
    <span style="font-size:0.95rem;">BullEye AI rekommenderar köp för <strong>{symbol}</strong> baserat på:<br>
    ✓ Positiv trendanalys (MA50 > MA200)<br>
    ✓ Köpmomentum (RSI: {rsi_value:.0f})<br>
    ✓ {news_text.lower()}<br>
    ✓ AI-confidence på {score}%
    </span>
</div>

<div style="margin-bottom: 8px; padding-top: 10px; border-top: 1px solid rgba(148, 163, 184, 0.3);">
    <strong style="font-size:1.05rem;">🔗 Källor</strong><br>
    <span style="font-size:0.92rem;">
    • <strong>Teknisk analys:</strong> Yahoo Finance (3-månaders historik)<br>
    • <strong>Indikatorer:</strong> RSI, MA50, MA200, Prisförändring<br>
    • <strong>Nyheter:</strong> Google News RSS-flöde<br>
    • <strong>Marknadspriser:</strong> Finnhub + Yahoo Finance<br>
    • <strong>Fundamental:</strong> Finnhub Company Data
    </span>
</div>
"""
    
    return analysis_html

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


# ===== PORTFOLIO ANALYSIS GENERATOR =====
def generate_portfolio_analysis(position, decision, pl_pct, prices=None):
    """
    Generates detailed analysis for portfolio positions.
    decision: "SÄLJ", "KÖP MER", or "AVVAKTA"
    """
    
    symbol = position.get("t", "N/A")
    qty = position.get("qty", 0)
    avg_price = position.get("avg_price", 0)
    current_price = position.get("price", avg_price)
    
    if prices is None:
        prices = []
    
    # ===== CALCULATE INDICATORS =====
    trend_score = get_trend_score_from_history(prices) if prices else 0
    rsi_value = calculate_rsi(prices) if prices else 50
    ma_score = get_ma_score(prices) if prices else 0
    # Keep dashboard navigation responsive: do not block on live news fetch here.
    news_score = get_news_score(symbol, allow_network=False)
    
    # Calculate moving averages
    ma50 = calculate_ma(prices, 50) if prices else None
    ma200 = calculate_ma(prices, 200) if prices else None
    
    # ===== DECISION-SPECIFIC TEXT =====
    if decision == "SÄLJ":
        decision_emoji = "📉"
        decision_header = "Varför AI rekommenderar SÄLJ"
        
        # P/L analysis
        if pl_pct >= 10:
            decision_text = f"🎯 <strong>Take profit nådd!</strong> Position upp {pl_pct:.1f}%. Det är ett bra tillfälle att ta hem vinsten."
            recommendation = "Rekommendation: Sälj hela positionen och ta hem vinsten."
        elif pl_pct <= -6:
            decision_text = f"⚠️ <strong>Stop-loss triggad!</strong> Position ner {pl_pct:.1f}%. Limitera förluster."
            recommendation = "Rekommendation: Sälj för att begränsa förlusten."
        elif news_score < -1:
            decision_text = f"📰 <strong>Negativa nyheter.</strong> Nyheterna har vänt negativt vilket påverkar priset."
            recommendation = "Rekommendation: Avvakta eller sälj en del."
        elif trend_score < -1:
            decision_text = f"📉 <strong>Trenden bruten.</strong> Priset har gått under viktiga stödnivåer."
            recommendation = "Rekommendation: Överväg att sälj."
        else:
            decision_text = "Tekniska signaler indikerar svaghet."
            recommendation = "Rekommendation: Sälj för omallokering."
        
        reasoning = f"Trendarbitrag: {trend_score} | RSI: {rsi_value:.0f} | Nyheter: {news_score}"
    
    elif decision == "KÖP MER":
        decision_emoji = "📈"
        decision_header = "Varför AI rekommenderar KÖP MER"
        
        decision_text = f"💡 <strong>Position visar styrka.</strong> P/L: +{pl_pct:.1f}%. Positiv momentum detekterat."
        recommendation = "Rekommendation: Lägg till till positionen på dip eller håll kursen."
        reasoning = f"Trend fortsätter upp | RSI: {rsi_value:.0f} | Positiva nyheter"
    
    else:  # AVVAKTA
        decision_emoji = "⏸️"
        decision_header = "Varför AI rekommenderar AVVAKTA"
        
        decision_text = f"⏸️ <strong>Osäker marknadssituation.</strong> Position P/L: {pl_pct:+.1f}%. Vänta på starkare signaler."
        recommendation = "Rekommendation: Håll positionen men lägg ingen ny pengar tills marknaden blir klarare."
        reasoning = f"Neutral trend | RSI: {rsi_value:.0f} | Blandad nyhetsbild"
    
    # ===== TREND DETAIL =====
    if ma50 and ma200:
        if ma50 > ma200:
            trend_detail = f"Uppåtgående MA (MA50: {ma50:.2f} > MA200: {ma200:.2f})"
        else:
            trend_detail = f"Nedåtgående MA (MA50: {ma50:.2f} < MA200: {ma200:.2f})"
    else:
        trend_detail = "Otillräcklig historisk data"
    
    # ===== GET NEWS SOURCES =====
    # Avoid blocking request path with live RSS fetch on constrained hosts.
    news_sources = get_news_sources(symbol, limit=2, allow_network=False)
    news_html = ""
    if news_sources:
        news_html = "<strong>Senaste nyheterna:</strong><br>"
        for src in news_sources:
            emoji = "🟢" if src["sentiment"] == "positive" else "🔴" if src["sentiment"] == "negative" else "🟡"
            news_html += f'{emoji} <a href="{src["url"]}" target="_blank" style="color:#70E000;text-decoration:none;font-size:0.9rem;">{src["title"][:50]}...</a><br>'
    
    # ===== GENERATE ANALYSIS HTML =====
    analysis_html = f"""
<div style="margin-bottom: 14px;">
    <strong style="font-size:1.05rem;">📌 Sammanfattning</strong><br>
    <span style="font-size:0.95rem;">Position i <strong>{symbol}</strong>: {qty} st @ {avg_price:.2f} SEK. Nuläge: <strong style="color:{'#70E000' if pl_pct > 0 else '#f87171'};">{pl_pct:+.1f}%</strong></span>
</div>

<div style="margin-bottom: 14px;">
    <strong style="font-size:1.05rem;">📊 Positionsanalys</strong><br>
    <span style="font-size:0.95rem;">
    Inköp: {avg_price:.2f} SEK | Nuläge: {current_price:.2f} SEK | P/L: <strong style="color:{'#70E000' if pl_pct > 0 else '#f87171'};">{pl_pct:+.1f}%</strong><br>
    Break-even: {avg_price:.2f} SEK | Stop-loss: {avg_price * 0.90:.2f} SEK | Target: {avg_price * 1.20:.2f} SEK
    </span>
</div>

<div style="margin-bottom: 14px;">
    <strong style="font-size:1.05rem;">📈 Trendanalys</strong><br>
    <span style="font-size:0.95rem;">{trend_detail}</span>
</div>

<div style="margin-bottom: 14px;">
    <strong style="font-size:1.05rem;">🧠 Momentumanalys</strong><br>
    <span style="font-size:0.95rem;">RSI: {rsi_value:.0f} {'(Överköpt)' if rsi_value > 70 else '(Översålt)' if rsi_value < 30 else '(Neutral)'}<br>
    MA-signal: {'Uppåt' if ma_score > 0 else 'Nedåt' if ma_score < 0 else 'Neutral'}
    </span>
</div>

<div style="margin-bottom: 14px;">
    <strong style="font-size:1.05rem;">📰 Nyhetsläge</strong><br>
    <span style="font-size:0.95rem;">{news_html}</span>
</div>

<div style="margin-bottom: 14px;">
    <strong style="font-size:1.05rem;">{decision_emoji} {decision_header}</strong><br>
    <span style="font-size:0.95rem;">{decision_text}</span>
</div>

<div style="margin-bottom: 14px;">
    <strong style="font-size:1.05rem;">✅ Rekommendation</strong><br>
    <span style="font-size:0.95rem;">{recommendation}</span>
</div>

<div style="margin-bottom: 8px; padding-top: 10px; border-top: 1px solid rgba(148, 163, 184, 0.3);">
    <strong style="font-size:1.05rem;">🔗 Källor & Indikatorer</strong><br>
    <span style="font-size:0.92rem;">
    {reasoning}<br>
    • Teknisk analys: Yahoo Finance<br>
    • Nyheter: Google News<br>
    • Indikatorer: RSI ({rsi_value:.0f}), MA50/200
    </span>
</div>
"""
    
    return analysis_html

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
def normalize_portfolio_decision(decision):
    d = (str(decision or "")).strip().upper()
    if d in {"SÄLJ", "SALJ", "SELL"}:
        return "SÄLJ"
    if d in {"KÖP MER", "KOP MER", "BUY MORE", "BUYMORE"}:
        return "KÖP MER"
    return "AVVAKTA"


def portfolio_ai_decision(pl_pct, current_price, start_price, t, risk, strategy):

    # Use cached/neutral news in interactive dashboard requests.
    news = get_news_score(t, allow_network=False)
    trend = 0
    if start_price > 0:
        trend_pct = (current_price - start_price) / start_price * 100
        if trend_pct > 3:
            trend = 1
        elif trend_pct < -3:
            trend = -1
    
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
            return "AVVAKTA", "Strong trend continues"
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

    return "AVVAKTA", "No strong signal"


def get_ai_recommended_sell_qty(position, decision, pl_pct):
    qty = int(position.get("qty") or 0)
    if qty <= 0:
        return 0

    if decision != "SÄLJ":
        return 1

    if decision == "SÄLJ":
        if pl_pct <= -8 or pl_pct >= 12:
            return qty
        return min(qty, max(1, int(math.ceil(qty * 0.5))))

    return 1


def get_ai_recommended_buy_more_qty(position, decision, pl_pct):
    qty = int(position.get("qty") or 0)
    if qty <= 0 or decision != "KÖP MER":
        return 0

    if pl_pct >= 10:
        factor = 0.2
    elif pl_pct >= 5:
        factor = 0.3
    else:
        factor = 0.4

    return max(1, int(math.ceil(qty * factor)))


def apply_portfolio_ai_actions(user, sell_list, buy_more_list, do_sell=False, do_buy_more=False):
    if do_sell:
        for item in sell_list:
            owned_qty = int(item.get("qty") or 0)
            rec_qty = int(item.get("recommended_sell_qty") or 0)
            qty = min(owned_qty, max(0, rec_qty))
            if qty > 0:
                sell(user, item["t"], qty)

    if do_buy_more:
        for item in buy_more_list:
            qty = int(item.get("recommended_buy_qty") or 0)
            price = float(item.get("price") or item.get("avg_price") or 0)
            if qty > 0 and price > 0:
                buy(user, item["t"], qty, price)

# ===== DATA (portfolio & trades) =====
# ===== DATA =====

def portfolio(user):
    if db_enabled():
        target = (user or "").strip().lower()
        try:
            with db_connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT t.ticker, SUM(t.qty) AS total_qty, SUM(t.qty * t.price) AS total_cost
                        FROM trades t
                        JOIN users u ON u.id = t.user_id
                        WHERE LOWER(u.email) = LOWER(%s)
                          AND t.side = 'BUY'
                        GROUP BY t.ticker
                        """,
                        (target,),
                    )
                    rows = cur.fetchall()
            result = []
            for ticker, qty_val, cost_val in rows:
                qty = int(float(qty_val or 0))
                if qty <= 0:
                    continue
                total_cost = float(cost_val or 0)
                avg_price = round(total_cost / qty, 2) if qty else 0
                result.append({
                    "t": ticker,
                    "symbol": ticker,
                    "display_name": get_asset_display_name(ticker),
                    "qty": qty,
                    "avg_price": avg_price,
                })
            return result
        except Exception as ex:
            logger.warning("DB portfolio load failed for %s: %s", target, ex)

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
            "symbol": t,
            "display_name": get_asset_display_name(t),
            "qty": d["qty"],
            "avg_price": avg_price
        })

    return result


def load_user_trade_rows(user):
    if db_enabled():
        target = (user or "").strip().lower()
        try:
            with db_connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT t.ticker, t.qty, t.price
                        FROM trades t
                        JOIN users u ON u.id = t.user_id
                        WHERE LOWER(u.email) = LOWER(%s)
                          AND t.side = 'BUY'
                        ORDER BY t.id
                        """,
                        (target,),
                    )
                    rows = cur.fetchall()
            out = []
            for ticker, qty_raw, price_raw in rows:
                qty = int(float(qty_raw or 0))
                price = float(price_raw or 0)
                if qty <= 0 or price <= 0:
                    continue
                out.append({
                    "ticker": (ticker or "").strip().upper(),
                    "qty": qty,
                    "buy_price": price,
                    "cost": qty * price,
                })
            return out
        except Exception as ex:
            logger.warning("DB trade row load failed for %s: %s", target, ex)

    target = (user or "").strip().lower()
    rows = []

    for raw in open(DATA_FILE).readlines():
        parts = raw.strip().split("|")
        if len(parts) < 4:
            continue

        u, ticker, qty_raw, price_raw = parts[:4]
        if (u or "").strip().lower() != target:
            continue

        try:
            qty = int(float(qty_raw))
            buy_price = float(price_raw)
        except Exception:
            continue

        if qty <= 0 or buy_price <= 0:
            continue

        rows.append({
            "ticker": (ticker or "").strip().upper(),
            "qty": qty,
            "buy_price": buy_price,
            "cost": qty * buy_price,
        })

    return rows


def normalize_min_trend_index_key(raw_key):
    key = (raw_key or "STANDARD").strip().upper()
    if key not in MIN_TREND_INDEX_OPTIONS:
        key = "STANDARD"
    return key


def normalize_min_trend_range_key(raw_key):
    key = (raw_key or "1Y").strip().upper()
    if key not in MIN_TREND_RANGE_OPTIONS:
        key = "1Y"
    return key


def normalize_min_trend_index_keys(raw_index_keys=None, fallback_key="STANDARD"):
    fallback = normalize_min_trend_index_key(fallback_key)
    out = {}
    source = raw_index_keys if isinstance(raw_index_keys, dict) else {}

    for chart_key in MIN_TREND_CHART_KEYS:
        out[chart_key] = normalize_min_trend_index_key(source.get(chart_key) or fallback)

    return out


def clone_index_history_payload(payload):
    return {
        "key": payload.get("key"),
        "range_key": payload.get("range_key"),
        "symbol": payload.get("symbol"),
        "name": payload.get("name"),
        "range_label": payload.get("range_label"),
        "values": list(payload.get("values", [])),
        "labels": list(payload.get("labels", [])),
        "start": payload.get("start", 0),
        "end": payload.get("end", 0),
        "change_pct": payload.get("change_pct", 0),
    }


def sample_series(values, points):
    if not values:
        return [100.0] * max(points, 1)
    if points <= 1:
        return [float(values[-1])]

    out = []
    span = len(values) - 1
    for i in range(points):
        idx = int(round((i / (points - 1)) * span))
        idx = max(0, min(span, idx))
        out.append(float(values[idx]))
    return out


def fetch_index_history(index_key, range_key):
    normalized_index_key = normalize_min_trend_index_key(index_key)
    normalized_range_key = normalize_min_trend_range_key(range_key)
    cache_key = (normalized_index_key, normalized_range_key)
    ttl = INDEX_HISTORY_CACHE_TTL.get(normalized_range_key, 900)
    now = time.time()

    cached = INDEX_HISTORY_CACHE.get(cache_key)
    if cached and (now - cached.get("ts", 0)) <= ttl:
        return clone_index_history_payload(cached["data"])

    index_cfg = MIN_TREND_INDEX_OPTIONS[normalized_index_key]
    range_cfg = MIN_TREND_RANGE_OPTIONS[normalized_range_key]

    symbol = index_cfg["symbol"]
    name = index_cfg["name"]

    closes = []
    labels = []

    try:
        url = (
            f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
            f"?range={range_cfg['yahoo_range']}&interval={range_cfg['interval']}"
        )
        headers = {
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json",
            "Referer": "https://finance.yahoo.com/",
        }
        res = requests.get(url, headers=headers, timeout=8)
        if res.status_code == 200:
            payload = res.json()
            result = payload.get("chart", {}).get("result", [{}])[0]
            closes_raw = (
                result.get("indicators", {})
                .get("quote", [{}])[0]
                .get("close", [])
            )
            ts_raw = result.get("timestamp", [])

            for idx, value in enumerate(closes_raw):
                if not isinstance(value, (int, float)) or value <= 0:
                    continue
                closes.append(float(value))
                ts = ts_raw[idx] if idx < len(ts_raw) else None
                if isinstance(ts, (int, float)):
                    labels.append(datetime.utcfromtimestamp(int(ts)).strftime("%Y-%m-%d"))
                else:
                    labels.append(str(len(labels) + 1))
    except Exception as ex:
        logger.warning("Index history fetch failed for %s: %s", symbol, ex)

    if not closes:
        closes = [100.0]
        labels = ["1"]

    start = closes[0]
    end = closes[-1]
    change_pct = ((end - start) / start * 100) if start else 0.0

    payload = {
        "key": normalized_index_key,
        "range_key": normalized_range_key,
        "symbol": symbol,
        "name": name,
        "range_label": range_cfg["label"],
        "values": [round(x, 2) for x in closes],
        "labels": labels,
        "start": round(start, 2),
        "end": round(end, 2),
        "change_pct": round(change_pct, 2),
    }

    INDEX_HISTORY_CACHE[cache_key] = {
        "ts": now,
        "data": clone_index_history_payload(payload),
    }
    trim_dict_cache(INDEX_HISTORY_CACHE, MAX_INDEX_HISTORY_CACHE_ITEMS)

    return payload


def build_min_trend_data(user, ranked, index_keys=None, range_key="1Y"):
    normalized_range_key = normalize_min_trend_range_key(range_key)
    normalized_index_keys = normalize_min_trend_index_keys(index_keys, "STANDARD")
    trade_rows = load_user_trade_rows(user)
    price_map = {}

    for s in ranked or []:
        ticker = (s.get("t") or "").strip().upper()
        if not ticker:
            continue
        raw_price = s.get("price", 0)
        if isinstance(raw_price, dict):
            raw_price = raw_price.get("price", 0)
        try:
            p = float(raw_price)
        except Exception:
            p = 0
        if p > 0:
            price_map[ticker] = p

    by_symbol = {}
    running_positions = {}
    buy_event_labels = []
    buy_event_values = []
    cumulative_buy = []
    portfolio_curve = []
    running_buy_total = 0.0

    for idx, row in enumerate(trade_rows, 1):
        ticker = row["ticker"]
        if ticker not in by_symbol:
            by_symbol[ticker] = {"qty": 0, "cost": 0.0}
        by_symbol[ticker]["qty"] += row["qty"]
        by_symbol[ticker]["cost"] += row["cost"]

        if ticker not in running_positions:
            running_positions[ticker] = {"qty": 0, "cost": 0.0}
        running_positions[ticker]["qty"] += row["qty"]
        running_positions[ticker]["cost"] += row["cost"]

        running_buy_total += row["cost"]
        buy_event_labels.append(f"Köp {idx}: {ticker}")
        buy_event_values.append(round(row["cost"], 2))
        cumulative_buy.append(round(running_buy_total, 2))

        current_total_value = 0.0
        for run_ticker, run_pos in running_positions.items():
            qty = run_pos["qty"]
            cost = run_pos["cost"]
            avg_price = (cost / qty) if qty else 0.0
            current_price = price_map.get(run_ticker) or avg_price
            current_total_value += qty * current_price
        portfolio_curve.append(round(current_total_value, 2))

    chart_index_meta = {
        chart_key: fetch_index_history(normalized_index_keys[chart_key], normalized_range_key)
        for chart_key in MIN_TREND_CHART_KEYS
    }

    total_index_series = sample_series(chart_index_meta["total"]["values"], len(cumulative_buy)) if cumulative_buy else []
    total_index_curve = []
    total_index_units = 0.0
    for idx, buy_cost in enumerate(buy_event_values):
        index_price = total_index_series[idx] if idx < len(total_index_series) else total_index_series[-1]
        if index_price > 0:
            total_index_units += buy_cost / index_price
        total_index_curve.append(round(total_index_units * index_price, 2))

    pl_index_series = sample_series(chart_index_meta["pl"]["values"], len(cumulative_buy)) if cumulative_buy else []
    pl_index_curve = []
    pl_index_units = 0.0
    for idx, buy_cost in enumerate(buy_event_values):
        index_price = pl_index_series[idx] if idx < len(pl_index_series) else pl_index_series[-1]
        if index_price > 0:
            pl_index_units += buy_cost / index_price
        pl_index_curve.append(round(pl_index_units * index_price, 2))

    recent_index_series = sample_series(chart_index_meta["recent"]["values"], len(cumulative_buy)) if cumulative_buy else []

    pl_curve = []
    for idx, invested in enumerate(cumulative_buy):
        portfolio_value = portfolio_curve[idx] if idx < len(portfolio_curve) else 0.0
        index_value = pl_index_curve[idx] if idx < len(pl_index_curve) else 0.0
        pl_curve.append(round(portfolio_value - invested, 2))
        pl_index_curve[idx] = round(index_value - invested, 2)

    positions = []
    missing_market_price = 0

    total_cost = 0.0
    total_value = 0.0

    for ticker, pos in sorted(by_symbol.items()):
        qty = pos["qty"]
        cost = pos["cost"]
        avg_price = (cost / qty) if qty else 0

        current_price = price_map.get(ticker)
        if not current_price:
            current_price = avg_price
            missing_market_price += 1

        current_value = qty * current_price
        pl_value = current_value - cost
        pl_pct = ((pl_value / cost) * 100) if cost else 0

        total_cost += cost
        total_value += current_value

        positions.append({
            "ticker": ticker,
            "qty": qty,
            "cost": round(cost, 2),
            "value": round(current_value, 2),
            "pl": round(pl_value, 2),
            "pl_pct": round(pl_pct, 2),
        })

    total_pl = total_value - total_cost
    total_pl_pct = ((total_pl / total_cost) * 100) if total_cost else 0

    recent_count = 8
    recent_buy_labels = buy_event_labels[-recent_count:]
    recent_buy_values = buy_event_values[-recent_count:]
    recent_index_values = recent_index_series[-recent_count:] if recent_index_series else []

    total_meta = chart_index_meta["total"]
    recent_meta = chart_index_meta["recent"]
    pl_meta = chart_index_meta["pl"]
    range_meta = chart_index_meta["range"]

    return {
        "has_data": len(trade_rows) > 0,
        "summary": {
            "total_cost": round(total_cost, 2),
            "total_value": round(total_value, 2),
            "total_pl": round(total_pl, 2),
            "total_pl_pct": round(total_pl_pct, 2),
            "buy_count": len(trade_rows),
            "position_count": len(positions),
            "missing_market_price": missing_market_price,
        },
        "index": {
            "key": total_meta["key"],
            "range_key": total_meta["range_key"],
            "name": total_meta["name"],
            "symbol": total_meta["symbol"],
            "start": total_meta["start"],
            "end": total_meta["end"],
            "change_pct": total_meta["change_pct"],
            "range_label": total_meta["range_label"],
        },
        "indices": {
            chart_key: {
                "key": meta["key"],
                "name": meta["name"],
                "symbol": meta["symbol"],
                "range_key": meta["range_key"],
                "range_label": meta["range_label"],
                "change_pct": meta["change_pct"],
            }
            for chart_key, meta in chart_index_meta.items()
        },
        "range_chart": {
            "labels": range_meta["labels"],
            "index": range_meta["values"],
            "index_label": range_meta["name"],
            "range_label": range_meta["range_label"],
        },
        "total_curve": {
            "labels": buy_event_labels,
            "invested": cumulative_buy,
            "portfolio": portfolio_curve,
            "index": total_index_curve,
            "index_label": total_meta["name"],
        },
        "recent_buys": {
            "labels": recent_buy_labels,
            "values": recent_buy_values,
            "index_values": recent_index_values,
            "index_label": recent_meta["name"],
        },
        "pl_curve": {
            "labels": buy_event_labels,
            "portfolio": pl_curve,
            "index": pl_index_curve,
            "index_label": pl_meta["name"],
        },
        "positions": positions,
    }

# ===== TRADE =====
# Trade-funktioner flyttade till trading.py och importeras högst upp

# ===== AUTH (all user) =====
# ===== APPROVAL SYSTEM =====
PENDING_FILE = "stock_data/pending.txt"
open(PENDING_FILE, "a").close()
run_auth_data_self_heal()

import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from urllib.parse import quote_plus

@app.route("/approve")
def approve():

    email = request.args.get("email")
    if not email:
        return "❌ Missing email", 400

    status = approve_pending_user(email)
    if status == "approved":
        ok, err = send_account_approved_email((email or "").strip().lower())
        if ok:
            return "✅ User approved! Confirmation message sent."
        logger.warning("Approval completed but confirmation mail failed for %s: %s", email, err)
        return "✅ User approved! Confirmation message failed."
    if status == "already_registered":
        return "ℹ️ User is already registered."
    return "ℹ️ User not found in pending list."


@app.route("/reject")
def reject():

    email = request.args.get("email")

    if not email:
        return "❌ Missing email", 400

    changed = reject_pending_user(email)
    if changed:
        return "❌ User rejected."
    return "ℹ️ User not found in pending list."

# ===== LOGIN =====
@app.route('/logo.png')
@app.route('/Bulleye_ver3.png')
def logo():
    base_dir = os.path.dirname(__file__)
    png_path = os.path.join(base_dir, 'Bulleye_ver3.png')
    jpg_path = os.path.join(base_dir, 'bulleye logo svart bakgrund.jpg')

    if os.path.exists(png_path):
        return send_file(png_path, mimetype='image/png')

    if os.path.exists(jpg_path):
        return send_file(jpg_path, mimetype='image/jpeg')

    return "Logo file not found", 404

@app.route("/login", methods=["GET", "POST"])
def login():
    print("DEBUG: LOGIN ROUTE CALLED")
    msg = session.pop("login_msg", "")

    if request.method == "POST":

        action = request.form.get("action")
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password")

        # ===== LOGIN =====
        if action == "login":
            if check_user(email, password):
                session["user"] = email
                session.permanent = True
                session["fast_login_bootstrap"] = True
                session["allow_fast_query_once"] = True
                return redirect("/dashboard")
            else:
                msg = "Fel login"

        elif action == "register":
            return redirect("/register")

    return f"""
    <html>
    <head>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
    :root {{
        --bg: #0B2341;
        --surface: rgba(11, 35, 65, 0.94);
        --surface-strong: rgba(7, 19, 44, 0.97);
        --text: #e2e8f0;
        --muted: #b3c7df;
        --accent: #70E000;
        --accent-soft: rgba(112, 224, 0, 0.18);
        --accent-gold: #F4B400;
        --border: rgba(30, 90, 168, 0.28);
    }}

    * {{ box-sizing: border-box; }}
    body {{
        margin: 0;
        min-height: 100vh;
        background: radial-gradient(circle at 30% 15%, rgba(112, 224, 0, 0.16), transparent 15%),
                    radial-gradient(circle at 80% 8%, rgba(244, 180, 0, 0.10), transparent 13%),
                    radial-gradient(circle at 50% 80%, rgba(112, 224, 0, 0.06), transparent 18%),
                    linear-gradient(180deg, #050c23 0%, #071a3f 100%);
        color: var(--text);
        font-family: Inter, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
        overflow-x: hidden;
    }}

    .login-shell {{
        display: grid;
        place-items: center;
        min-height: 100vh;
        padding: 24px;
    }}

    .login-card {{
        width: min(100%, 460px);
        background: var(--surface);
        border: 1px solid var(--border);
        border-radius: 30px;
        box-shadow: 0 30px 80px rgba(0, 0, 0, 0.35);
        overflow: hidden;
    }}

    .login-hero {{
        padding: 36px 28px;
        background: radial-gradient(circle at top center, rgba(112, 224, 0, 0.14), transparent 25%),
                    radial-gradient(circle at 15% 15%, rgba(244, 180, 0, 0.12), transparent 18%),
                    linear-gradient(180deg, rgba(11, 35, 65, 0.98), rgba(30, 90, 168, 0.92));
        border-bottom: 1px solid rgba(244, 180, 0, 0.16);
        text-align: center;
    }}

    .logo {{
        width: 520px;
        max-width: 95%;
        height: auto;
        margin-bottom: -40px;
        filter: drop-shadow(0 6px 24px rgba(0, 0, 0, 0.35));
        border-radius: 24px;
        position: relative;
        z-index: 10;
    }}

    .hero-subtitle {{
        margin: 16px auto 0;
        max-width: 320px;
        color: var(--muted);
        font-size: 0.98rem;
        line-height: 1.7;
    }}

    .login-body {{
        padding: 28px;
        display: grid;
        gap: 18px;
    }}

    .field-group label {{
        display: block;
        margin-bottom: 8px;
        font-size: 0.95rem;
        color: var(--muted);
    }}

    .field-group input {{
        width: 100%;
        border-radius: 14px;
        border: 1px solid rgba(100, 175, 255, 0.16);
        background: rgba(8, 18, 42, 0.9);
        color: var(--text);
        padding: 14px 16px;
        outline: none;
    }}

    .field-group input:focus {{
        border-color: var(--accent);
        box-shadow: 0 0 0 5px rgba(38, 255, 156, 0.12);
    }}

    .password-wrap {{
        position: relative;
    }}

    .password-wrap .password-input {{
        padding-right: 54px;
    }}

    .toggle-password-btn {{
        position: absolute;
        right: 10px;
        top: 50%;
        transform: translateY(-50%);
        border: 1px solid rgba(100, 175, 255, 0.2);
        background: rgba(8, 18, 42, 0.95);
        color: var(--muted);
        border-radius: 10px;
        width: 36px;
        height: 34px;
        display: inline-flex;
        align-items: center;
        justify-content: center;
        cursor: pointer;
        padding: 0;
    }}

    .toggle-password-btn:hover {{
        color: var(--text);
        border-color: rgba(112, 224, 0, 0.42);
    }}

    .button-row {{ display: flex; gap: 12px; flex-wrap: wrap; }}

    .button-primary,
    .button-secondary {{
        border: none;
        border-radius: 999px;
        padding: 14px 22px;
        cursor: pointer;
        font-weight: 600;
        transition: transform 0.2s ease, background 0.2s ease;
    }}

    .button-primary {{
        background: linear-gradient(135deg, #70E000, #F4B400);
        color: #08161d;
    }}

    .button-primary:hover {{ transform: translateY(-2px); }}

    .button-secondary {{
        background: rgba(255, 255, 255, 0.08);
        color: var(--text);
        border: 1px solid rgba(255, 255, 255, 0.08);
    }}

    .note {{
        color: var(--muted);
        font-size: 0.95rem;
        text-align: center;
        line-height: 1.6;
    }}

    .msg {{
        color: #ff8f8f;
        font-size: 0.95rem;
        text-align: center;
        min-height: 22px;
    }}

    .login-footer {{
        display: flex;
        justify-content: space-between;
        gap: 12px;
        flex-wrap: wrap;
        color: var(--muted);
        font-size: 0.92rem;
    }}

    a {{ color: var(--accent); text-decoration: none; }}

    @media (max-width: 520px) {{
        .login-body {{ padding: 22px 20px; }}
    }}
    </style>
    </head>

    <body>

    <div class="login-shell">
        <div class="login-card">
            <div class="login-hero">
                <img class="logo" src="/Bulleye_ver3.png" alt="BullEye AI logo">
                <p class="hero-subtitle">Sikta rätt. Investera smart. En svensk fintech-plattform för AI-drivna portföljer.</p>
            </div>
            <div class="login-body">
                <form method="post" action="/login">
                    <div class="field-group">
                        <label>Email</label>
                        <input type="email" name="email" required>
                    </div>
                    <div class="field-group">
                        <label>Lösenord</label>
                        <div class="password-wrap">
                            <input id="login-password" class="password-input" type="password" name="password" required>
                            <button type="button" class="toggle-password-btn" data-target="login-password" aria-label="Visa lösenord">👁</button>
                        </div>
                    </div>
                    <div class="button-row" style="margin-top: 8px;">
                        <button class="button-primary" type="submit" name="action" value="login">Logga in</button>
                        <a class="button-secondary" href="/register" style="text-decoration:none; display:inline-flex; align-items:center;">Skapa konto</a>
                    </div>
                    <p class="msg">{msg}</p>
                    <div class="login-footer">
                        <span>Premium AI-investering.</span>
                        <a href="/forgot">Glömt lösenord?</a>
                    </div>
                </form>
            </div>
        </div>
    </div>

    <script>
    document.querySelectorAll('.toggle-password-btn').forEach(function(btn) {{
        btn.addEventListener('click', function() {{
            const targetId = btn.getAttribute('data-target');
            const input = document.getElementById(targetId);
            if (!input) return;

            const showing = input.type === 'text';
            input.type = showing ? 'password' : 'text';
            btn.setAttribute('aria-label', showing ? 'Visa lösenord' : 'Dölj lösenord');
            btn.textContent = showing ? '👁' : '🙈';
        }});
    }});
    </script>

    </body>
    </html>
    """


@app.route("/register", methods=["GET", "POST"])
def register_account():
    msg = ""
    msg_class = "msg-neutral"
    entered_email = ""
    selected_known = []
    other_name = ""

    if request.method == "POST":
        entered_email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""
        selected_known = request.form.getlist("trading_platforms")
        wants_other = request.form.get("use_other_platform") == "1"
        other_name = sanitize_custom_platform_name(request.form.get("other_platform")) if wants_other else ""
        selected_platforms = append_custom_platform_selection(selected_known, other_name)

        has_known_or_other = bool(selected_known) or bool(other_name)
        if not entered_email or "@" not in entered_email:
            msg = "❌ Ange en giltig email-adress"
            msg_class = "msg-error"
        elif not password:
            msg = "❌ Ange ett lösenord"
            msg_class = "msg-error"
        elif not has_known_or_other:
            msg = "❌ Välj minst en handelsplattform eller fyll i Övrig"
            msg_class = "msg-error"
        elif user_exists(entered_email):
            msg = "Användare finns redan"
            msg_class = "msg-error"
        elif pending_user_exists(entered_email):
            msg = "ℹ️ Kontoansökan väntar redan på godkännande"
            msg_class = "msg-neutral"
        else:
            hashed = hash_password(password)
            create_pending_user(entered_email, hashed, selected_platforms)
            request_base_url = (request.url_root or "").rstrip("/")
            admin_mail_ok, admin_mail_err = send_approval_email(entered_email, request_base_url)
            user_mail_ok, user_mail_err = send_registration_received_email(entered_email, request_base_url)

            if not is_email_enabled():
                msg = "✅ Ansökan skickad. Vi återkommer när kontot har granskats."
                msg_class = "msg-success"
            elif admin_mail_ok and user_mail_ok:
                msg = "✅ Ansökan skickad. Bekräftelsemeddelande är skickat och du får ett nytt meddelande när kontot blir godkänt."
                msg_class = "msg-success"
            elif admin_mail_ok and not user_mail_ok:
                msg = f"✅ Ansökan skickad. Admin-notis skickades, men bekräftelsemeddelande till dig misslyckades ({user_mail_err})."
                msg_class = "msg-warn"
            elif not admin_mail_ok and user_mail_ok:
                msg = f"✅ Ansökan skickad. Bekräftelsemeddelande till dig skickades, men admin-notis misslyckades ({admin_mail_err})."
                msg_class = "msg-warn"
            else:
                msg = (
                    "✅ Ansökan skickad. Meddelandeutskick misslyckades just nu "
                    f"(admin: {admin_mail_err}, bekräftelse: {user_mail_err}). "
                    "Förfrågan finns ändå sparad och kan godkännas manuellt."
                )
                msg_class = "msg-warn"
            entered_email = ""
            selected_known = []
            other_name = ""

    options_html = "".join(
        [
            f'<label><input type="checkbox" name="trading_platforms" value="{name}" {"checked" if name in selected_known else ""}> {name}</label>'
            for name in TRADING_PLATFORM_LOGIN_URLS.keys()
        ]
    )
    other_checked = "checked" if other_name else ""

    return f"""
    <html>
    <head>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
    :root {{
        --bg: #0B2341;
        --surface: rgba(11, 35, 65, 0.94);
        --surface-strong: rgba(7, 19, 44, 0.97);
        --text: #e2e8f0;
        --muted: #b3c7df;
        --accent: #70E000;
        --accent-gold: #F4B400;
        --border: rgba(30, 90, 168, 0.28);
    }}

    * {{ box-sizing: border-box; }}
    body {{
        margin: 0;
        min-height: 100vh;
        background: radial-gradient(circle at 30% 15%, rgba(112, 224, 0, 0.16), transparent 15%),
                    radial-gradient(circle at 80% 8%, rgba(244, 180, 0, 0.10), transparent 13%),
                    radial-gradient(circle at 50% 80%, rgba(112, 224, 0, 0.06), transparent 18%),
                    linear-gradient(180deg, #050c23 0%, #071a3f 100%);
        color: var(--text);
        font-family: Inter, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
        overflow-x: hidden;
    }}

    .login-shell {{
        display: grid;
        place-items: center;
        min-height: 100vh;
        padding: 24px;
    }}

    .login-card {{
        width: min(100%, 680px);
        background: var(--surface);
        border: 1px solid var(--border);
        border-radius: 30px;
        box-shadow: 0 30px 80px rgba(0, 0, 0, 0.35);
        overflow: hidden;
    }}

    .login-hero {{
        padding: 30px 28px;
        background: radial-gradient(circle at top center, rgba(112, 224, 0, 0.14), transparent 25%),
                    radial-gradient(circle at 15% 15%, rgba(244, 180, 0, 0.12), transparent 18%),
                    linear-gradient(180deg, rgba(11, 35, 65, 0.98), rgba(30, 90, 168, 0.92));
        border-bottom: 1px solid rgba(244, 180, 0, 0.16);
        text-align: center;
    }}

    .logo {{
        width: 300px;
        max-width: 95%;
        height: auto;
        margin-bottom: -10px;
        filter: drop-shadow(0 6px 24px rgba(0, 0, 0, 0.35));
        border-radius: 24px;
    }}

    .hero-subtitle {{
        margin: 14px auto 0;
        max-width: 540px;
        color: var(--muted);
        font-size: 0.95rem;
        line-height: 1.7;
    }}

    .login-body {{
        padding: 24px;
        display: grid;
        gap: 16px;
    }}

    .field-group label {{
        display: block;
        margin-bottom: 8px;
        font-size: 0.95rem;
        color: var(--muted);
    }}

    .field-group input {{
        width: 100%;
        border-radius: 14px;
        border: 1px solid rgba(100, 175, 255, 0.16);
        background: rgba(8, 18, 42, 0.9);
        color: var(--text);
        padding: 14px 16px;
        outline: none;
    }}

    .field-group input:focus {{
        border-color: var(--accent);
        box-shadow: 0 0 0 5px rgba(38, 255, 156, 0.12);
    }}

    .password-wrap {{
        position: relative;
    }}

    .password-wrap .password-input {{
        padding-right: 54px;
    }}

    .toggle-password-btn {{
        position: absolute;
        right: 10px;
        top: 50%;
        transform: translateY(-50%);
        border: 1px solid rgba(100, 175, 255, 0.2);
        background: rgba(8, 18, 42, 0.95);
        color: var(--muted);
        border-radius: 10px;
        width: 36px;
        height: 34px;
        display: inline-flex;
        align-items: center;
        justify-content: center;
        cursor: pointer;
        padding: 0;
    }}

    .toggle-password-btn:hover {{
        color: var(--text);
        border-color: rgba(112, 224, 0, 0.42);
    }}

    .platform-picker {{
        border: 1px solid rgba(100, 175, 255, 0.16);
        border-radius: 14px;
        background: rgba(8, 18, 42, 0.45);
        padding: 12px;
    }}

    .platform-picker > span {{
        display: block;
        color: var(--muted);
        margin-bottom: 8px;
        font-size: 0.9rem;
    }}

    .platform-grid {{
        display: grid;
        grid-template-columns: repeat(2, minmax(0, 1fr));
        gap: 8px;
        font-size: 0.88rem;
    }}

    .platform-grid label {{
        display: flex;
        gap: 7px;
        align-items: flex-start;
        color: var(--text);
    }}

    .other-wrap {{
        margin-top: 10px;
        display: grid;
        gap: 8px;
    }}

    .other-row {{
        display: flex;
        align-items: center;
        gap: 8px;
        font-size: 0.9rem;
    }}

    .other-info {{
        color: var(--muted);
        font-size: 0.82rem;
        line-height: 1.5;
    }}

    .button-row {{ display: flex; gap: 12px; flex-wrap: wrap; }}

    .button-primary,
    .button-secondary {{
        border: none;
        border-radius: 999px;
        padding: 14px 22px;
        cursor: pointer;
        font-weight: 600;
        transition: transform 0.2s ease, background 0.2s ease;
    }}

    .button-primary {{
        background: linear-gradient(135deg, #70E000, #F4B400);
        color: #08161d;
    }}

    .button-primary:hover {{ transform: translateY(-2px); }}

    .button-secondary {{
        background: rgba(255, 255, 255, 0.08);
        color: var(--text);
        border: 1px solid rgba(255, 255, 255, 0.08);
        text-decoration: none;
        display: inline-flex;
        align-items: center;
    }}

    .msg {{
        font-size: 0.95rem;
        text-align: center;
        min-height: 28px;
        line-height: 1.5;
        padding: 10px 12px;
        border-radius: 10px;
        margin: 0;
    }}

    .msg-neutral {{
        color: #cbd5e1;
        background: rgba(148, 163, 184, 0.14);
        border: 1px solid rgba(148, 163, 184, 0.25);
    }}

    .msg-success {{
        color: #dcfce7;
        background: rgba(22, 163, 74, 0.18);
        border: 1px solid rgba(74, 222, 128, 0.35);
    }}

    .msg-warn {{
        color: #fef3c7;
        background: rgba(234, 179, 8, 0.16);
        border: 1px solid rgba(250, 204, 21, 0.34);
    }}

    .msg-error {{
        color: #fecaca;
        background: rgba(239, 68, 68, 0.14);
        border: 1px solid rgba(248, 113, 113, 0.34);
    }}

    @media (max-width: 680px) {{
        .platform-grid {{ grid-template-columns: 1fr; }}
    }}
    </style>
    </head>

    <body>
    <div class="login-shell">
        <div class="login-card">
            <div class="login-hero">
                <img class="logo" src="/Bulleye_ver3.png" alt="BullEye AI logo">
                <p class="hero-subtitle">Skapa konto och välj vilka handelsplattformar du använder för dina rekommendationslänkar.</p>
            </div>
            <div class="login-body">
                <form method="post" action="/register">
                    <div class="field-group">
                        <label>Email</label>
                        <input type="email" name="email" value="{entered_email}" required>
                    </div>
                    <div class="field-group">
                        <label>Lösenord</label>
                        <div class="password-wrap">
                            <input id="register-password" class="password-input" type="password" name="password" required>
                            <button type="button" class="toggle-password-btn" data-target="register-password" aria-label="Visa lösenord">👁</button>
                        </div>
                    </div>
                    <div class="platform-picker">
                        <span>Välj handelsplattform(ar):</span>
                        <div class="platform-grid">{options_html}</div>
                        <div class="other-wrap">
                            <label class="other-row"><input type="checkbox" name="use_other_platform" value="1" {other_checked}> Övrig:</label>
                            <input type="text" name="other_platform" value="{other_name}" placeholder="Skriv namn på plattform">
                            <div class="other-info">Info: För Övrig skapas en direkt söklänk till plattformens inloggningssida.</div>
                        </div>
                    </div>
                    <div class="button-row" style="margin-top: 8px;">
                        <button class="button-primary" type="submit">Skapa konto</button>
                        <a class="button-secondary" href="/login">Tillbaka till login</a>
                    </div>
                    <p class="msg {msg_class}">{msg}</p>
                </form>
            </div>
        </div>
    </div>
    <script>
    document.querySelectorAll('.toggle-password-btn').forEach(function(btn) {{
        btn.addEventListener('click', function() {{
            const targetId = btn.getAttribute('data-target');
            const input = document.getElementById(targetId);
            if (!input) return;

            const showing = input.type === 'text';
            input.type = showing ? 'password' : 'text';
            btn.setAttribute('aria-label', showing ? 'Visa lösenord' : 'Dölj lösenord');
            btn.textContent = showing ? '👁' : '🙈';
        }});
    }});
    </script>
    </body>
    </html>
    """

# ===== LOGOUT =====
@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


@app.route("/delete_account", methods=["POST"])
def delete_account():
    user = session.get("user")
    if not user:
        return redirect("/login")

    delete_registered_user(user)
    reject_pending_user(user)
    session.clear()
    session["login_msg"] = "✅ Konto avregistrerat"
    return redirect("/login")

# ✅ ===== CHANGE PASSWORD =====
@app.route("/change_password", methods=["GET", "POST"])
def change_password():
    msg = ""

    user = session.get("user")
    if not user:
        return redirect("/login")

    if request.method == "POST":
        new_password = (request.form.get("new_password") or "").strip()
        confirm_password = (request.form.get("confirm_password") or "").strip()

        if not new_password:
            msg = "❌ Ange ett nytt lösenord"
        elif len(new_password) < 6:
            msg = "❌ Lösenordet måste vara minst 6 tecken"
        elif new_password != confirm_password:
            msg = "❌ Lösenorden matchar inte"
        else:
            new_hash = hash_password(new_password)

            updated, new_lines = build_updated_user_lines(user, new_hash)
            if updated:
                if new_lines is not None:
                    open(USERS_FILE, "w").writelines(new_lines)
                session.clear()
                session["login_msg"] = "✅ Lösenordet är uppdaterat. Logga in med ditt nya lösenord."
                return redirect("/login")
            else:
                msg = "❌ Kunde inte uppdatera lösenord"

    return f"""
    <html>
    <head>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
    :root {{
        --bg: #0B2341;
        --surface: rgba(11, 35, 65, 0.94);
        --text: #e2e8f0;
        --muted: #b3c7df;
        --accent: #70E000;
        --accent-gold: #F4B400;
        --border: rgba(30, 90, 168, 0.28);
    }}

    * {{ box-sizing: border-box; }}
    body {{
        margin: 0;
        min-height: 100vh;
        background: radial-gradient(circle at 30% 15%, rgba(112, 224, 0, 0.16), transparent 15%),
                    radial-gradient(circle at 80% 8%, rgba(244, 180, 0, 0.10), transparent 13%),
                    radial-gradient(circle at 50% 80%, rgba(112, 224, 0, 0.06), transparent 18%),
                    linear-gradient(180deg, #050c23 0%, #071a3f 100%);
        color: var(--text);
        font-family: Inter, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
        display: grid;
        place-items: center;
        padding: 24px;
    }}

    .card {{
        width: min(100%, 560px);
        background: var(--surface);
        border: 1px solid var(--border);
        border-radius: 24px;
        box-shadow: 0 30px 80px rgba(0, 0, 0, 0.35);
        overflow: hidden;
    }}

    .hero {{
        padding: 28px 24px;
        text-align: center;
        background: radial-gradient(circle at top center, rgba(112, 224, 0, 0.14), transparent 25%),
                    radial-gradient(circle at 15% 15%, rgba(244, 180, 0, 0.12), transparent 18%),
                    linear-gradient(180deg, rgba(11, 35, 65, 0.98), rgba(30, 90, 168, 0.92));
        border-bottom: 1px solid rgba(244, 180, 0, 0.16);
    }}

    .logo {{
        width: 240px;
        max-width: 95%;
        height: auto;
        margin-bottom: -4px;
        border-radius: 16px;
        filter: drop-shadow(0 6px 20px rgba(0,0,0,0.35));
    }}

    .hero p {{
        margin: 10px 0 0;
        color: var(--muted);
        font-size: 0.95rem;
        line-height: 1.6;
    }}

    .body {{
        padding: 22px;
        display: grid;
        gap: 14px;
    }}

    label {{
        color: var(--muted);
        font-size: 0.92rem;
        display: block;
        margin-bottom: 8px;
    }}

    input[type="password"] {{
        width: 100%;
        border-radius: 12px;
        border: 1px solid rgba(100, 175, 255, 0.16);
        background: rgba(8, 18, 42, 0.9);
        color: var(--text);
        padding: 12px 14px;
        outline: none;
    }}

    input[type="password"]:focus {{
        border-color: var(--accent);
        box-shadow: 0 0 0 4px rgba(38, 255, 156, 0.12);
    }}

    .row {{
        display: flex;
        gap: 10px;
        flex-wrap: wrap;
        align-items: center;
    }}

    button {{
        border: none;
        border-radius: 999px;
        padding: 12px 18px;
        font-weight: 600;
        cursor: pointer;
        background: linear-gradient(135deg, #70E000, #F4B400);
        color: #08161d;
    }}

    a {{
        color: var(--accent);
        text-decoration: none;
    }}

    .msg {{
        min-height: 24px;
        color: #ffb4b4;
        margin: 0;
    }}
    </style>
    </head>
    <body>
    <div class="card">
        <div class="hero">
            <img class="logo" src="/Bulleye_ver3.png" alt="BullEye AI logo">
            <p>Byt lösenord. Du loggas ut direkt efter uppdatering och loggar in med det nya lösenordet.</p>
        </div>
        <div class="body">
            <form method="post">
                <div>
                    <label>Nytt lösenord</label>
                    <input type="password" name="new_password" required>
                </div>
                <div>
                    <label>Bekräfta nytt lösenord</label>
                    <input type="password" name="confirm_password" required>
                </div>
                <div class="row">
                    <button type="submit">Uppdatera lösenord</button>
                    <a href="/dashboard">Tillbaka till dashboard</a>
                </div>
                <p class="msg">{msg}</p>
            </form>
        </div>
    </div>
    </body>
    </html>
    """


@app.route("/change_trading_platform", methods=["GET", "POST"])
def change_trading_platform():
    user = session.get("user")
    if not user:
        return redirect("/login")

    msg = ""
    current_platforms = get_user_trading_platforms(user)
    selected_known, selected_other = split_platforms_for_form(current_platforms)

    if request.method == "POST":
        selected_platforms = request.form.getlist("trading_platforms")
        wants_other = request.form.get("use_other_platform") == "1"
        other_name = sanitize_custom_platform_name(request.form.get("other_platform")) if wants_other else ""
        final_selection = append_custom_platform_selection(selected_platforms, other_name)

        if not selected_platforms and not other_name:
            msg = "❌ Välj minst en handelsplattform eller fyll i Övrig"
        else:
            updated, new_lines = build_updated_platform_lines(user, final_selection)
            if updated:
                if new_lines is not None:
                    open(USERS_FILE, "w").writelines(new_lines)
                # Match login flow: render dashboard immediately and finish heavy work in the background.
                session["fast_login_bootstrap"] = True
                return redirect("/dashboard?tab=dashboard")
            else:
                msg = "❌ Kunde inte uppdatera handelsplattformar"

    options_html = "".join(
        [
            f'<label><input type="checkbox" name="trading_platforms" value="{name}" {"checked" if name in selected_known else ""}> {name}</label>'
            for name in TRADING_PLATFORM_LOGIN_URLS.keys()
        ]
    )
    other_checked = "checked" if selected_other else ""

    return f"""
    <html>
    <head>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
    :root {{
        --bg: #0B2341;
        --surface: rgba(11, 35, 65, 0.94);
        --text: #e2e8f0;
        --muted: #b3c7df;
        --accent: #70E000;
        --border: rgba(30, 90, 168, 0.28);
    }}
    body {{
        margin: 0;
        min-height: 100vh;
        background: linear-gradient(180deg, #050c23 0%, #071a3f 100%);
        color: var(--text);
        font-family: Inter, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
        display: grid;
        place-items: center;
        padding: 24px;
    }}
    .card {{
        width: min(100%, 640px);
        background: var(--surface);
        border: 1px solid var(--border);
        border-radius: 18px;
        padding: 22px;
    }}
    h2 {{ margin-top: 0; }}
    .platform-grid {{
        display: grid;
        grid-template-columns: repeat(2, minmax(0, 1fr));
        gap: 9px;
        margin: 12px 0 18px;
    }}
    .platform-grid label {{
        display: flex;
        gap: 8px;
        align-items: flex-start;
        font-size: 0.93rem;
    }}
    .button-row {{
        display: flex;
        gap: 10px;
        flex-wrap: wrap;
        align-items: center;
    }}
    button {{
        border: 1px solid rgba(112, 224, 0, 0.35);
        border-radius: 999px;
        background: rgba(112, 224, 0, 0.18);
        color: var(--text);
        padding: 10px 16px;
        cursor: pointer;
    }}
    .button-ghost {{
        border-color: rgba(100, 175, 255, 0.32);
        background: rgba(100, 175, 255, 0.16);
    }}
    .save-spinner {{
        width: 12px;
        height: 12px;
        border-radius: 50%;
        border: 2px solid rgba(226, 232, 240, 0.35);
        border-top-color: rgba(226, 232, 240, 1);
        display: none;
        animation: spin 0.8s linear infinite;
    }}
    @keyframes spin {{
        to {{ transform: rotate(360deg); }}
    }}
    .msg {{ min-height: 20px; color: #ffb4b4; }}
    a {{ color: var(--accent); text-decoration: none; }}
    </style>
    </head>
    <body>
    <div class="card">
        <h2>Ändra Handelsplattform</h2>
        <form method="post" id="change-platform-form">
            <div class="platform-grid">{options_html}</div>
            <div style="margin-bottom:14px;">
                <label style="display:flex;gap:8px;align-items:center;margin-bottom:8px;"><input type="checkbox" name="use_other_platform" value="1" {other_checked}> Övrig:</label>
                <input type="text" name="other_platform" value="{selected_other}" placeholder="Skriv namn på plattform" style="width:100%;border-radius:10px;border:1px solid rgba(100, 175, 255, 0.16);background:rgba(8, 18, 42, 0.9);color:var(--text);padding:10px 12px;">
                <div style="margin-top:8px;color:var(--muted);font-size:0.82rem;line-height:1.5;">Info: För Övrig skapas en direkt söklänk till plattformens inloggningssida.</div>
            </div>
            <div class="button-row">
                <button type="submit" id="save-platforms-btn">Spara</button>
                <button type="button" id="keep-platforms-btn" class="button-ghost">Behåll befintliga plattformar</button>
                <span id="save-spinner" class="save-spinner" aria-hidden="true"></span>
            </div>
            <div id="save-platforms-status" style="display:none;margin-top:10px;color:var(--muted);font-size:0.9rem;">Sparar... skickar tillbaka till dashboard.</div>
        </form>
        <p class="msg">{msg}</p>
        <a href="/dashboard">Tillbaka till dashboard</a>
    </div>
    <script>
    (function () {{
        const form = document.getElementById('change-platform-form');
        const saveBtn = document.getElementById('save-platforms-btn');
        const keepBtn = document.getElementById('keep-platforms-btn');
        const status = document.getElementById('save-platforms-status');
        const spinner = document.getElementById('save-spinner');
        if (!form || !saveBtn || !keepBtn || !status || !spinner) return;

        form.addEventListener('submit', function () {{
            saveBtn.disabled = true;
            saveBtn.textContent = 'Sparar...';
            saveBtn.style.opacity = '0.7';
            saveBtn.style.cursor = 'default';
            keepBtn.disabled = true;
            keepBtn.style.opacity = '0.6';
            keepBtn.style.cursor = 'default';
            spinner.style.display = 'inline-block';
            status.style.display = 'block';
        }});

        keepBtn.addEventListener('click', function () {{
            keepBtn.disabled = true;
            keepBtn.textContent = 'Återgår...';
            keepBtn.style.opacity = '0.7';
            keepBtn.style.cursor = 'default';
            saveBtn.disabled = true;
            saveBtn.style.opacity = '0.6';
            saveBtn.style.cursor = 'default';
            spinner.style.display = 'inline-block';
            status.textContent = 'Inga ändringar sparas. Skickar tillbaka till dashboard.';
            status.style.display = 'block';
            window.location.href = '/dashboard?tab=dashboard';
        }});
    }})();
    </script>
    </body>
    </html>
    """

# ===== FORGOT PASSWORD =====
@app.route("/forgot", methods=["GET", "POST"])
def forgot():

    msg = ""

    if request.method == "POST":
        email = (request.form.get("email") or "").strip()

        if not email:
            msg = "❌ Ange en giltig email-adress."
        elif not user_exists(email):
            msg = "❌ Detta konto finns inte registrerat"
        else:
            new_password = generate_temp_password(7)
            new_hash = hash_password(new_password)

            updated, new_lines = build_updated_user_lines(email, new_hash)

            if not updated:
                msg = "❌ Kunde inte uppdatera användaren."
            else:
                ok, err = send_reset_email(email, new_password)
                if ok:
                    if new_lines is not None:
                        open(USERS_FILE, "w").writelines(new_lines)
                    msg = f"✅ Meddelande skickat till {email}. Ett nytt lösenord på 7 tecken har genererats."
                else:
                    msg = f"❌ {err}"

    return f"""
    <html>
    <head>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
    :root {{
        --bg: #0B2341;
        --surface: rgba(11, 35, 65, 0.94);
        --surface-strong: rgba(7, 19, 44, 0.97);
        --text: #e2e8f0;
        --muted: #b3c7df;
        --accent: #70E000;
        --accent-soft: rgba(112, 224, 0, 0.18);
        --accent-gold: #F4B400;
        --border: rgba(30, 90, 168, 0.28);
    }}

    * {{ box-sizing: border-box; }}
    body {{
        margin: 0;
        min-height: 100vh;
        background: radial-gradient(circle at 30% 15%, rgba(112, 224, 0, 0.16), transparent 15%),
                    radial-gradient(circle at 80% 8%, rgba(244, 180, 0, 0.10), transparent 13%),
                    radial-gradient(circle at 50% 80%, rgba(112, 224, 0, 0.06), transparent 18%),
                    linear-gradient(180deg, #050c23 0%, #071a3f 100%);
        color: var(--text);
        font-family: Inter, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}

    .login-shell {{
        display: grid;
        place-items: center;
        min-height: 100vh;
        padding: 24px;
    }}

    .login-card {{
        width: min(100%, 460px);
        background: var(--surface);
        border: 1px solid var(--border);
        border-radius: 30px;
        box-shadow: 0 30px 80px rgba(0, 0, 0, 0.35);
        overflow: hidden;
    }}

    .login-hero {{
        padding: 36px 28px;
        background: radial-gradient(circle at top center, rgba(112, 224, 0, 0.14), transparent 25%),
                    radial-gradient(circle at 15% 15%, rgba(244, 180, 0, 0.12), transparent 18%),
                    linear-gradient(180deg, rgba(11, 35, 65, 0.98), rgba(30, 90, 168, 0.92));
        border-bottom: 1px solid rgba(244, 180, 0, 0.16);
        text-align: center;
    }}

    .logo {{
        width: 210px;
        max-width: 100%;
        height: auto;
        margin-bottom: 18px;
        filter: drop-shadow(0 6px 24px rgba(0, 0, 0, 0.35));
    }}

    .hero-title {{
        color: transparent;
        font-size: 0;
        margin: 0;
        line-height: 0;
        height: 0;
        overflow: hidden;
    }}

    .hero-subtitle {{
        margin: 16px auto 0;
        max-width: 320px;
        color: var(--muted);
        font-size: 0.98rem;
        line-height: 1.7;
    }}

    .login-body {{
        padding: 28px;
        display: grid;
        gap: 18px;
    }}

    .field-group label {{
        display: block;
        margin-bottom: 8px;
        font-size: 0.95rem;
        color: var(--muted);
    }}

    .field-group input {{
        width: 100%;
        border-radius: 14px;
        border: 1px solid rgba(100, 175, 255, 0.16);
        background: rgba(8, 18, 42, 0.9);
        color: var(--text);
        padding: 14px 16px;
        outline: none;
    }}

    .field-group input:focus {{
        border-color: var(--accent);
        box-shadow: 0 0 0 5px rgba(38, 255, 156, 0.12);
    }}

    .button-row {{ display: flex; gap: 12px; flex-wrap: wrap; }}

    .button-primary,
    .button-secondary {{
        border: none;
        border-radius: 999px;
        padding: 14px 22px;
        cursor: pointer;
        font-weight: 600;
        transition: transform 0.2s ease, background 0.2s ease;
    }}

    .button-primary {{
        background: linear-gradient(135deg, #70E000, #F4B400);
        color: #08161d;
    }}

    .button-primary:hover {{ transform: translateY(-2px); }}

    .button-secondary {{
        background: rgba(255, 255, 255, 0.08);
        color: var(--text);
        border: 1px solid rgba(255, 255, 255, 0.08);
    }}

    .note {{
        color: var(--muted);
        font-size: 0.95rem;
        text-align: center;
        line-height: 1.6;
    }}

    .msg {{
        color: #ff8f8f;
        font-size: 0.95rem;
        text-align: center;
        min-height: 22px;
    }}

    .login-footer {{
        display: flex;
        justify-content: space-between;
        gap: 12px;
        flex-wrap: wrap;
        color: var(--muted);
        font-size: 0.92rem;
    }}

    a {{ color: var(--accent); text-decoration: none; }}

    @media (max-width: 520px) {{
        .login-body {{ padding: 22px 20px; }}
    }}
    </style>
    </head>

    <body>

    <div class="login-shell">
        <div class="login-card">
            <div class="login-hero">
                <img class="logo" src="/Bulleye_ver3.png" alt="BullEye AI logo">
                <h1 class="hero-title">Återställ lösenord</h1>
                <p class="hero-subtitle">Skriv in din email så skickar vi dig en återställningslänk direkt.</p>
            </div>
            <div class="login-body">
                <form method="post" action="/forgot">
                    <div class="field-group">
                        <label>Email</label>
                        <input type="email" name="email" required>
                    </div>
                    <div class="button-row">
                        <button class="button-primary" type="submit">Skicka</button>
                    </div>
                    <p class="msg">{msg}</p>
                    <div class="login-footer">
                        <span>AI-styrd återställning.</span>
                        <a href="/login">Tillbaka</a>
                    </div>
                </form>
            </div>
        </div>
    </div>

    </body>
    </html>
    """
# ===== EMAIL/SYSTEM =====
# ===== APPROVAL EMAIL =====

def get_smtp_host():
    return (os.environ.get("EMAIL_HOST") or os.environ.get("SMTP_HOST") or "smtp.office365.com").strip()


def get_smtp_port():
    raw_port = (os.environ.get("EMAIL_PORT") or os.environ.get("SMTP_PORT") or "587").strip()
    try:
        return int(raw_port)
    except Exception:
        return 587


def get_email_user():
    return (os.environ.get("EMAIL_USER") or os.environ.get("SMTP_USER") or "").strip()


def get_email_password():
    return (os.environ.get("EMAIL_PASSWORD") or os.environ.get("SMTP_PASSWORD") or "").strip()


def get_brevo_api_key():
    return (os.environ.get("BREVO_API_KEY") or "").strip()


def get_brevo_sender_email():
    return (os.environ.get("BREVO_SENDER_EMAIL") or "").strip()


def get_brevo_sender_name():
    return (os.environ.get("BREVO_SENDER_NAME") or "BullEye AI").strip()


def send_mail_via_brevo_api(recipients, subject, text_body, html_body=None):
    if not is_email_enabled():
        logger.info("Email disabled via EMAIL_ENABLED; skipping Brevo API send")
        return True, "EMAIL_ENABLED=0"

    api_key = get_brevo_api_key()
    sender_email = get_brevo_sender_email()
    if not api_key or not sender_email:
        return False, "BREVO_API_KEY/BREVO_SENDER_EMAIL saknas"

    to_payload = []
    for email in recipients:
        target = (email or "").strip()
        if target:
            to_payload.append({"email": target})

    if not to_payload:
        return False, "Inga mottagare"

    payload = {
        "sender": {
            "name": get_brevo_sender_name(),
            "email": sender_email,
        },
        "to": to_payload,
        "subject": subject,
        "textContent": text_body,
    }
    if html_body:
        payload["htmlContent"] = html_body

    try:
        res = requests.post(
            "https://api.brevo.com/v3/smtp/email",
            headers={
                "accept": "application/json",
                "api-key": api_key,
                "content-type": "application/json",
            },
            json=payload,
            timeout=20,
        )
        if 200 <= res.status_code < 300:
            return True, ""

        detail = ""
        try:
            detail = str(res.json())
        except Exception:
            detail = (res.text or "").strip()
        return False, f"Brevo API {res.status_code}: {detail[:240]}"
    except Exception as ex:
        return False, str(ex)

def send_registration_received_email(user_email, base_url=None):
    if not is_email_enabled():
        logger.info("Email disabled via EMAIL_ENABLED; skipping registration confirmation to %s", user_email)
        return True, "EMAIL_ENABLED=0"

    sender = get_email_user()
    password = get_email_password()

    if not sender or not password:
        logger.warning("Registration confirmation mail not sent: EMAIL_USER/EMAIL_PASSWORD missing")
        return False, "EMAIL_USER/EMAIL_PASSWORD saknas"

    base = (base_url or BASE_URL or "").strip().rstrip("/")
    if not base:
        base = "http://localhost:10000"

    body = f"""
Hej,

Vi har tagit emot din kontoansökan till BullEye AI.

Nästa steg:
1. Din ansökan granskas av admin.
2. När kontot är godkänt får du ett nytt mail.
3. Därefter kan du logga in här: {base}/login

Vänliga hälsningar,
BullEye AI
"""

    msg = MIMEText(body)
    msg["Subject"] = "BullEye AI - Vi har tagit emot din ansökan"
    msg["From"] = sender
    msg["To"] = user_email

    # Prefer Brevo API when configured to avoid SMTP auth issues.
    api_ok, api_err = send_mail_via_brevo_api(
        [user_email],
        "BullEye AI - Vi har tagit emot din ansökan",
        body,
        None,
    )
    if api_ok:
        logger.info("REGISTRATION CONFIRMATION MAIL SENT TO: %s (Brevo API)", user_email)
        return True, ""
    if get_brevo_api_key():
        logger.warning("Brevo API registration mail failed, fallback SMTP: %s", api_err)

    try:
        server = smtplib.SMTP(get_smtp_host(), get_smtp_port(), timeout=20)
        server.starttls()
        server.login(sender, password)
        server.send_message(msg)
        server.quit()
        logger.info("REGISTRATION CONFIRMATION MAIL SENT TO: %s", user_email)
        return True, ""
    except Exception as ex:
        logger.error(
            "Registration confirmation mail error for %s via %s:%s user=%s: %s",
            user_email,
            get_smtp_host(),
            get_smtp_port(),
            _email_hint(get_email_user()),
            ex,
        )
        return False, str(ex)

def send_approval_email(new_user_email, base_url=None):
    if not is_email_enabled():
        logger.info("Email disabled via EMAIL_ENABLED; skipping approval request mail for %s", new_user_email)
        return True, "EMAIL_ENABLED=0"

    sender = get_email_user()
    password = get_email_password()

    if not sender or not password:
        logger.warning("Approval mail not sent: EMAIL_USER/EMAIL_PASSWORD missing")
        return False, "EMAIL_USER/EMAIL_PASSWORD saknas"

    base = (base_url or BASE_URL or "").strip().rstrip("/")
    if not base:
        base = "http://localhost:10000"

    encoded_email = quote_plus(new_user_email)
    approve_link = f"{base}/approve?email={encoded_email}"
    reject_link = f"{base}/reject?email={encoded_email}"

    recipients = sorted(load_extra_admin_emails() | set(ADMIN_EMAILS))
    if not recipients:
        recipients = [sender]

    text_body = f"""
Ny användare vill registrera:

{new_user_email}

Godkänn:
{approve_link}

Neka:
{reject_link}
"""

    html_body = f"""
<html>
    <body style=\"font-family:Segoe UI,Arial,sans-serif;background:#0b1220;color:#e2e8f0;padding:20px;\">
        <div style=\"max-width:560px;margin:auto;background:#111827;border:1px solid #334155;border-radius:14px;padding:18px;\">
            <h2 style=\"margin-top:0;color:#f8fafc;\">Ny användaransökan</h2>
            <p style=\"line-height:1.5;\">En ny användare vill registrera sig i BullEye AI:</p>
            <p style=\"font-weight:700;color:#93c5fd;\">{new_user_email}</p>

            <div style=\"margin-top:18px;display:flex;gap:10px;flex-wrap:wrap;\">
                <a href=\"{approve_link}\" style=\"text-decoration:none;background:#16a34a;color:#ffffff;padding:10px 16px;border-radius:999px;font-weight:700;\">Approve</a>
                <a href=\"{reject_link}\" style=\"text-decoration:none;background:#dc2626;color:#ffffff;padding:10px 16px;border-radius:999px;font-weight:700;\">Reject</a>
            </div>

            <p style=\"margin-top:16px;color:#94a3b8;font-size:12px;\">Om knapparna inte fungerar, använd länkarna nedan:</p>
            <p style=\"font-size:12px;word-break:break-all;\">Approve: {approve_link}<br>Reject: {reject_link}</p>
        </div>
    </body>
</html>
"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = "Godkänn användare"
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    # Prefer Brevo API when configured to avoid SMTP auth issues.
    api_ok, api_err = send_mail_via_brevo_api(
        recipients,
        "Godkänn användare",
        text_body,
        html_body,
    )
    if api_ok:
        logger.info("Approval email sent for %s to %s (Brevo API)", new_user_email, recipients)
        return True, ""
    if get_brevo_api_key():
        logger.warning("Brevo API approval mail failed, fallback SMTP: %s", api_err)

    try:
        server = smtplib.SMTP(get_smtp_host(), get_smtp_port(), timeout=20)
        server.starttls()
        server.login(sender, password)
        server.send_message(msg)
        server.quit()
        logger.info("Approval email sent for %s to %s", new_user_email, recipients)
        return True, ""
    except Exception as e:
        logger.error(
            "Approval mail error for %s via %s:%s user=%s: %s",
            new_user_email,
            get_smtp_host(),
            get_smtp_port(),
            _email_hint(get_email_user()),
            e,
        )
        return False, str(e)


def send_account_approved_email(user_email):
    if not is_email_enabled():
        logger.info("Email disabled via EMAIL_ENABLED; skipping account-approved mail to %s", user_email)
        return True, "EMAIL_ENABLED=0"

    sender = get_email_user()
    password = get_email_password()

    if not sender or not password:
        logger.warning("Approval confirmation mail not sent: EMAIL_USER/EMAIL_PASSWORD missing")
        return False, "EMAIL_USER/EMAIL_PASSWORD saknas"

    body = f"""
Hej,

Ditt konto i BullEye AI har nu blivit godkänt.

Du kan logga in med din registrerade email här:
{BASE_URL}/login

Välkommen!
"""

    msg = MIMEText(body)
    msg["Subject"] = "BullEye AI - Konto godkänt"
    msg["From"] = sender
    msg["To"] = user_email

    # Prefer Brevo API when configured to avoid SMTP auth issues.
    api_ok, api_err = send_mail_via_brevo_api(
        [user_email],
        "BullEye AI - Konto godkänt",
        body,
        None,
    )
    if api_ok:
        logger.info("ACCOUNT APPROVAL MAIL SENT TO: %s (Brevo API)", user_email)
        return True, ""
    if get_brevo_api_key():
        logger.warning("Brevo API approval confirmation mail failed, fallback SMTP: %s", api_err)

    try:
        server = smtplib.SMTP(get_smtp_host(), get_smtp_port(), timeout=20)
        server.starttls()
        server.login(sender, password)
        server.send_message(msg)
        server.quit()
        logger.info("ACCOUNT APPROVAL MAIL SENT TO: %s", user_email)
        return True, ""
    except Exception as ex:
        logger.error(
            "Account approval mail error for %s via %s:%s user=%s: %s",
            user_email,
            get_smtp_host(),
            get_smtp_port(),
            _email_hint(get_email_user()),
            ex,
        )
        return False, str(ex)

# ===== ALERT FUNCTION =====
def send_alert(email, message, alert_type="GENERAL"):
    if not is_email_enabled():
        logger.info("Email disabled via EMAIL_ENABLED; alert mail skipped for %s", email)
        with open(ALERT_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {email} {alert_type} DISABLED {message}\n")
        return

    sender = get_email_user()
    password = get_email_password()

    if not sender or not password:
        logger.warning("Email not configured; alert not sent: %s", message)
        with open(ALERT_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {email} {alert_type} {message}\n")
        return

    msg = MIMEText(message)
    msg["Subject"] = f"🚨 Trading Alert: {alert_type}"
    msg["From"] = sender
    msg["To"] = email

    try:
        server = smtplib.SMTP("smtp.office365.com", 587)
        server.starttls()
        server.login(sender, password)
        server.send_message(msg)
        server.quit()
        logger.info("ALERT SENT to %s: %s", email, message)
    except Exception as ex:
        logger.error(
            "Alert error for %s via %s:%s user=%s: %s",
            email,
            get_smtp_host(),
            get_smtp_port(),
            _email_hint(get_email_user()),
            ex,
        )
        with open(ALERT_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {email} {alert_type} FAILED {message} {ex}\n")
        return

# ===== RESET EMAIL =====
def send_reset_email(email, new_password):
    if not is_email_enabled():
        logger.warning("Reset mail blocked because EMAIL_ENABLED is disabled")
        return False, "Meddelandefunktionen är tillfälligt avstängd (EMAIL_ENABLED=0). Kontakta admin."

    sender = get_email_user()
    password = get_email_password()

    if not sender or not password:
        logger.warning("Reset mail not sent: EMAIL_USER/EMAIL_PASSWORD missing")
        return False, "Meddelandetjänsten är inte konfigurerad på servern (EMAIL_USER/EMAIL_PASSWORD saknas)."

    body = f"""
Hej,

Du har begärt återställning av lösenord för BullEye AI.

Ditt nya tillfälliga lösenord är:
{new_password}

Logga in och byt lösenord direkt efter inloggning.
"""

    msg = MIMEText(body)
    msg["Subject"] = "BullEye AI - Nytt lösenord"
    msg["From"] = sender
    msg["To"] = email

    try:
        server = smtplib.SMTP("smtp.office365.com", 587)
        server.starttls()
        server.login(sender, password)
        server.send_message(msg)
        server.quit()
        logger.info("RESET MAIL SENT TO: %s", email)
        return True, ""
    except Exception as e:
        logger.error(
            "Reset mail error for %s via %s:%s user=%s: %s",
            email,
            get_smtp_host(),
            get_smtp_port(),
            _email_hint(get_email_user()),
            e,
        )
        return False, f"Kunde inte skicka meddelande: {e}"

# ===== UI HELPERS =====
def get_buy_link(t):
    # Returnerar en korrekt HTML-länk till köp-sidor beroende på asset
    if len(t) > 5:
        return f'<a href="https://safello.com/sv/kop/{t.lower()}" target="_blank">Safello</a>'
    else:
        return f'<a href="https://www.avanza.se/aktier/sok.html?query={t}" target="_blank">Avanza</a>'



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

{s.get("summary", "")}<br><br>

{s.get("reason", "").replace("\n", "<br>")}
"""

    # ✅ RIKTIG LÄNK (FIXAD)
    link = f'<a href="https://news.google.com/search?q={s["t"]}" target="_blank">🔗 Läs mer / nyheter</a>'

    return f"""
    <div style="position:relative; margin-bottom:10px;">

    <b>{name} (Score {s.get('score','-')}) | {buy_link}</b>
    {extra}
    <span class="{cls}">AI: {signal}</span><br>

    <span class="ai-box">
        <button type="button" onclick="togglePopup(this)">AI Analys</button>

        <div class="ai-popup">

            <div style="text-align:right;">
                <button onclick="closePopup(this)" style="background:none;border:none;font-size:16px;">Close</button>
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


def build_watch_summary(s):
    signal = s.get("signal", "AVVAKTA")
    score = s.get("score", 0)
    trigger = s.get("trigger_score", 0)

    if signal == "KÖP":
        lead = "AI ser visst köpmomentum, men tillgången är inte prioriterad för direkt köp just nu."
    elif signal == "SÄLJ":
        lead = "AI ser svaghet i nuläget och lutar mot sälj eller minskad exponering."
    else:
        lead = "Signalen är neutral och tillräckligt svag för att avvakta just nu."

    if trigger < 2:
        trigger_txt = "Triggerstyrkan är ännu för låg för en tydlig köptrigger."
    else:
        trigger_txt = "Triggerstyrkan finns, men andra kandidater bedöms starkare just nu."

    if score < 70:
        score_txt = f"Score {score} är under nivån för topprekommendation."
    else:
        score_txt = f"Score {score} är okej, men räcker inte för att flyttas upp till AI Aktieval nu."

    return f"{lead} {trigger_txt} {score_txt}"


def generate_watch_analysis(s):
    symbol = s.get("t", "N/A")
    score = s.get("score", 0)
    trigger = s.get("trigger_score", 0)
    signal = s.get("signal", "AVVAKTA")
    summary = build_watch_summary(s)

    if signal == "KÖP":
        current_state = "Köpsignal finns, men tillgången är nedprioriterad jämfört med starkare kandidater."
    elif signal == "SÄLJ":
        current_state = "Säljsignal dominerar, så en försiktigare position eller försäljning är mer rimlig än nytt köp."
    else:
        current_state = "Signalen är neutral och saknar tillräcklig styrka för ett aktivt köpbeslut."

    if trigger < 2:
        trigger_state = "Låg triggerstyrka - invänta tydligare momentum, nyhetsdriv eller bekräftad trend."
    else:
        trigger_state = "Trigger finns, men totalrankingen är lägre än de aktiva KÖP-kandidaterna."

    analysis_html = f"""
<div style="margin-bottom: 14px;">
    <strong style="font-size:1.05rem;">📌 Sammanfattning</strong><br>
    <span style="font-size:0.95rem;">AI rekommenderar <strong>{signal}</strong> för <strong>{symbol}</strong>. Nuvarande score: <strong>{score}</strong>, trigger: <strong>{trigger}</strong>.</span>
</div>

<div style="margin-bottom: 14px;">
    <strong style="font-size:1.05rem;">🧠 Nuvarande signalbild</strong><br>
    <span style="font-size:0.95rem;">{current_state}</span>
</div>

<div style="margin-bottom: 14px;">
    <strong style="font-size:1.05rem;">⚠️ Varför avvakta</strong><br>
    <span style="font-size:0.95rem;">{summary}<br>{trigger_state}</span>
</div>

<div style="margin-bottom: 8px; padding-top: 10px; border-top: 1px solid rgba(148, 163, 184, 0.3);">
    <strong style="font-size:1.05rem;">📋 Vad AI vill se före köp</strong><br>
    <span style="font-size:0.92rem;">
    • Starkare relativ score mot övriga kandidater<br>
    • Tydligare trendbekräftelse och/eller momentum<br>
    • Positivt nyhetsflöde med bättre triggerutfall
    </span>
</div>
"""

    return analysis_html


def enrich_with_buy_plan(candidates, total_capital_usd, usd_sek_rate=10.5, risk_profile="medium"):
    """
    Attach AI-driven buy quantity suggestions to each candidate.
    Allocation is score-weighted and capped by available capital share per candidate.
    """
    if not candidates:
        return

    try:
        capital = float(total_capital_usd)
    except Exception:
        capital = 0.0

    if capital <= 0:
        for s in candidates:
            s["recommended_qty"] = 0
            s["recommended_usd"] = 0.0
            s["recommended_sek"] = 0.0
            s["allocation_share_pct"] = 0
        return

    if risk_profile == "low":
        exponent = 0.85
    elif risk_profile == "high":
        exponent = 1.25
    else:
        exponent = 1.0

    weights = []
    for s in candidates:
        score = max(float(s.get("score", 0) or 0), 1.0)
        trigger = max(float(s.get("trigger_score", 0) or 0), 0.0)
        # Score is the primary driver; trigger gives a small bonus.
        weight = (score ** exponent) * (1.0 + (0.08 * trigger))
        weights.append(weight)

    total_weight = sum(weights)
    if total_weight <= 0:
        total_weight = float(len(candidates))
        weights = [1.0] * len(candidates)

    for idx, s in enumerate(candidates):
        raw_price = s.get("price", 0)
        if isinstance(raw_price, dict):
            raw_price = raw_price.get("price", 0)

        try:
            price = float(raw_price)
        except Exception:
            price = 0.0

        allocation_usd = capital * (weights[idx] / total_weight)
        allocation_pct = int(round((weights[idx] / total_weight) * 100))

        if price <= 0:
            qty = 0
        else:
            qty = int(allocation_usd // price)
            if s.get("signal") == "KÖP" and qty == 0 and allocation_usd >= (price * 0.5):
                qty = 1

        usd_value = round(qty * price, 2) if price > 0 else 0.0
        sek_value = round(usd_value * float(usd_sek_rate), 2)

        s["recommended_qty"] = max(0, qty)
        s["recommended_usd"] = usd_value
        s["recommended_sek"] = sek_value
        s["allocation_share_pct"] = max(0, allocation_pct)


def parse_capital_amount(raw_value, default_value=10000):
    """Parse user-entered capital amount safely from text input."""
    if raw_value is None:
        return int(default_value)

    txt = str(raw_value).strip()
    if not txt:
        return int(default_value)

    txt = txt.replace(" ", "").replace(",", ".")
    try:
        val = float(txt)
    except Exception:
        return int(default_value)

    return max(0, int(val))


def convert_capital_to_usd(amount_value, currency, usd_sek_rate, usd_eur_rate):
    """Convert capital from selected currency to USD for allocation math."""
    amount = float(amount_value or 0)
    ccy = (currency or "SEK").upper()

    if ccy == "USD":
        return amount

    if ccy == "EUR":
        return amount / float(usd_eur_rate or 0.92)

    # Default: SEK
    return amount / float(usd_sek_rate or 10.5)


def convert_usd_to_currency(amount_usd, currency, usd_sek_rate, usd_eur_rate):
    amount = float(amount_usd or 0)
    ccy = (currency or "USD").upper()

    if ccy == "SEK":
        return amount * float(usd_sek_rate or 10.5)

    if ccy == "EUR":
        return amount * float(usd_eur_rate or 0.92)

    return amount


def build_mintrend_summary_display(min_trend_data, currency, usd_sek_rate, usd_eur_rate):
    summary = (min_trend_data or {}).get("summary", {})
    ccy = (currency or "USD").upper()
    if ccy not in {"SEK", "USD", "EUR"}:
        ccy = "USD"

    return {
        "currency": ccy,
        "total_cost": round(convert_usd_to_currency(summary.get("total_cost", 0), ccy, usd_sek_rate, usd_eur_rate), 2),
        "total_value": round(convert_usd_to_currency(summary.get("total_value", 0), ccy, usd_sek_rate, usd_eur_rate), 2),
        "total_pl": round(convert_usd_to_currency(summary.get("total_pl", 0), ccy, usd_sek_rate, usd_eur_rate), 2),
        "total_pl_pct": summary.get("total_pl_pct", 0),
    }


def dedupe_by_symbol(items):
    """Keep first occurrence per symbol to avoid duplicate allocations."""
    out = []
    seen = set()
    for item in items:
        symbol = item.get("t")
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        out.append(item)
    return out


def build_emergency_recommendations(limit=5):
    """Build a minimal fallback list so dashboard is never empty."""
    try:
        target_limit = max(1, int(limit or 5))
    except Exception:
        target_limit = 5

    seeds = [
        "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "V", "MA", "JPM", "TSLA", "SPY", "QQQ",
    ]
    out = []
    seen = set()

    for symbol in seeds:
        if symbol in seen:
            continue
        seen.add(symbol)

        price_data = get_price(symbol)
        if isinstance(price_data, dict):
            price = float(price_data.get("price") or 0)
            volume = float(price_data.get("volume") or 0)
        else:
            price = float(price_data or 0)
            volume = 0.0

        if price <= 0:
            continue

        display_name = get_asset_display_name(symbol) or symbol
        out.append(
            {
                "t": symbol,
                "name": display_name,
                "display_name": display_name,
                "type": "stock",
                "currency": "USD",
                "price": price,
                "volume": volume,
                "score": 55,
                "signal": "AVVAKTA KÖP",
                "trigger_score": 1,
                "trigger_reasons": ["Reservläge"],
                "reason": "Reservläge: tillfälligt urval medan full AI-scan laddar.",
                "summary": "Reservrekommendation för att undvika tom dashboard under cache/API-störning.",
            }
        )
        if len(out) >= target_limit:
            break

    return out


@app.route("/api/mintrend-data", methods=["GET"])
def api_mintrend_data():
    user = session.get("user")
    if not user:
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    legacy_mintrend_index_key = normalize_min_trend_index_key(
        request.args.get("mintrend_index") or session.get("mintrend_index") or "STANDARD"
    )
    mintrend_index_keys = normalize_min_trend_index_keys(
        {
            "total": request.args.get("mintrend_index_total") or session.get("mintrend_index_total"),
            "recent": request.args.get("mintrend_index_recent") or session.get("mintrend_index_recent"),
            "pl": request.args.get("mintrend_index_pl") or session.get("mintrend_index_pl"),
            "range": request.args.get("mintrend_index_range") or session.get("mintrend_index_range"),
        },
        legacy_mintrend_index_key,
    )

    mintrend_range_key = normalize_min_trend_range_key(
        request.args.get("mintrend_range") or session.get("mintrend_range") or "1Y"
    )
    mintrend_currency = (request.args.get("mintrend_currency") or session.get("mintrend_currency") or "USD").upper()
    if mintrend_currency not in {"SEK", "USD", "EUR"}:
        mintrend_currency = "USD"

    session["mintrend_index"] = mintrend_index_keys["total"]
    for chart_key, idx_key in mintrend_index_keys.items():
        session[f"mintrend_index_{chart_key}"] = idx_key
    session["mintrend_range"] = mintrend_range_key
    session["mintrend_currency"] = mintrend_currency

    ranked = ai_results_cache.get("data") or ai_cache.get("data") or []
    min_trend_data = build_min_trend_data(user, ranked, mintrend_index_keys, mintrend_range_key)
    fx_rates = get_usd_fx_rates()
    usd_sek_rate = float(fx_rates.get("SEK", 10.5))
    usd_eur_rate = float(fx_rates.get("EUR", 0.92))
    mintrend_summary_display = build_mintrend_summary_display(
        min_trend_data,
        mintrend_currency,
        usd_sek_rate,
        usd_eur_rate,
    )
    mintrend_fx_info = build_fx_info(fx_rates)

    return jsonify(
        {
            "ok": True,
            "min_trend_data": min_trend_data,
            "mintrend_summary_display": mintrend_summary_display,
            "mintrend_fx_info": mintrend_fx_info,
            "mintrend_index_keys": mintrend_index_keys,
            "mintrend_range_key": mintrend_range_key,
            "mintrend_currency": mintrend_currency,
        }
    )
    
@app.route("/dashboard", methods=["GET", "POST"])
def dashboard():

    user = session.get("user")
    if not user:
        return redirect("/login")

    is_admin = is_admin_email(user)
    user_platforms = get_user_trading_platforms(user)
    platform_links = build_platform_links(user_platforms)
    platform_names = build_platform_names_for_header(user_platforms)
    requested_tab = request.args.get("tab") or request.form.get("active_tab") or "dashboard"
    if requested_tab not in {"dashboard", "portfolio", "mintrend", "users"}:
        requested_tab = "dashboard"
    if requested_tab == "users" and not is_admin:
        requested_tab = "dashboard"
    active_tab = requested_tab
    fast_login_bootstrap = bool(session.pop("fast_login_bootstrap", False))
    fast_query_requested = request.args.get("fast") == "1"
    allow_fast_query_once = bool(session.pop("allow_fast_query_once", False))
    fast_query_bootstrap = fast_query_requested and allow_fast_query_once

    quick_bootstrap = fast_login_bootstrap or fast_query_bootstrap
    if request.method != "GET" or active_tab != "dashboard":
        quick_bootstrap = False

    legacy_mintrend_index_key = normalize_min_trend_index_key(
        request.form.get("mintrend_index") or request.args.get("mintrend_index") or session.get("mintrend_index") or "STANDARD"
    )
    mintrend_index_keys = normalize_min_trend_index_keys(
        {
            "total": request.form.get("mintrend_index_total") or request.args.get("mintrend_index_total") or session.get("mintrend_index_total"),
            "recent": request.form.get("mintrend_index_recent") or request.args.get("mintrend_index_recent") or session.get("mintrend_index_recent"),
            "pl": request.form.get("mintrend_index_pl") or request.args.get("mintrend_index_pl") or session.get("mintrend_index_pl"),
            "range": request.form.get("mintrend_index_range") or request.args.get("mintrend_index_range") or session.get("mintrend_index_range"),
        },
        legacy_mintrend_index_key,
    )
    mintrend_index_key = mintrend_index_keys["total"]
    mintrend_range_key = normalize_min_trend_range_key(
        request.form.get("mintrend_range") or request.args.get("mintrend_range") or session.get("mintrend_range") or "1Y"
    )
    mintrend_currency = (request.form.get("mintrend_currency") or request.args.get("mintrend_currency") or session.get("mintrend_currency") or "USD").upper()
    if mintrend_currency not in {"SEK", "USD", "EUR"}:
        mintrend_currency = "USD"

    session["mintrend_index"] = mintrend_index_key
    for chart_key, idx_key in mintrend_index_keys.items():
        session[f"mintrend_index_{chart_key}"] = idx_key
    session["mintrend_range"] = mintrend_range_key
    session["mintrend_currency"] = mintrend_currency

    if request.method == "POST" and is_admin:
        if "admin_add_user" in request.form:
            target = (request.form.get("admin_add_user") or request.form.get("admin_new_user_email") or "").strip().lower()
            if not target or "@" not in target:
                session["users_msg"] = "❌ Ange en giltig email-adress"
            elif not user_exists(target):
                session["users_msg"] = f"ℹ️ {target} finns inte bland registrerade users"
            elif is_admin_email(target):
                session["users_msg"] = f"ℹ️ {target} är redan admin"
            else:
                if add_admin_email(target):
                    session["users_msg"] = f"✅ {target} är nu admin"
                else:
                    session["users_msg"] = f"❌ Kunde inte uppdatera admin för {target}"
            return redirect("/dashboard?tab=users")

        if "admin_approve" in request.form:
            target = (request.form.get("admin_approve") or "").strip()
            status = approve_pending_user(target) if target else "not_found"
            if status == "approved":
                ok, err = send_account_approved_email(target.strip().lower())
                if ok:
                    session["users_msg"] = f"✅ Godkände {target} och skickade godkännandemeddelande"
                else:
                    session["users_msg"] = f"✅ Godkände {target}, men kunde inte skicka godkännandemeddelande ({err})"
            elif status == "already_registered":
                session["users_msg"] = f"ℹ️ {target} är redan registrerad"
            else:
                session["users_msg"] = f"ℹ️ Kunde inte godkänna {target}"
            return redirect("/dashboard?tab=users")

        if "admin_reject" in request.form:
            target = (request.form.get("admin_reject") or "").strip()
            if target and reject_pending_user(target):
                session["users_msg"] = f"❌ Nekade {target}"
            else:
                session["users_msg"] = f"ℹ️ Kunde inte neka {target}"
            return redirect("/dashboard?tab=users")

        if "admin_delete" in request.form:
            target = (request.form.get("admin_delete") or "").strip()
            if is_admin_email(target):
                session["users_msg"] = "⚠️ Admin-kontot kan inte tas bort"
            elif target and delete_registered_user(target):
                session["users_msg"] = f"🗑️ Tog bort {target}"
            else:
                session["users_msg"] = f"ℹ️ Kunde inte ta bort {target}"
            return redirect("/dashboard?tab=users")

        if "admin_reset_password" in request.form:
            target = (request.form.get("admin_reset_password") or "").strip().lower()
            if not target:
                session["users_msg"] = "❌ Ange en giltig user"
                return redirect("/dashboard?tab=users")

            new_password = generate_temp_password(7)
            new_hash = hash_password(new_password)
            updated, new_lines = build_updated_user_lines(target, new_hash)

            if not updated:
                session["users_msg"] = f"❌ Kontot {target} hittades inte"
                return redirect("/dashboard?tab=users")

            ok, err = send_reset_email(target, new_password)
            if ok:
                if new_lines is not None:
                    open(USERS_FILE, "w").writelines(new_lines)
                session["users_msg"] = f"✅ Nytt lösenord skickat till {target}"
            else:
                session["users_msg"] = f"❌ Kunde inte skicka lösenordsmeddelande: {err}"
            return redirect("/dashboard?tab=users")

    if request.method == "POST" and (
        "apply_mintrend_settings" in request.form
        or "mintrend_index" in request.form
        or "mintrend_range" in request.form
        or "mintrend_currency" in request.form
        or any(name.startswith("mintrend_index_") for name in request.form.keys())
    ):
        return redirect("/dashboard?tab=mintrend")

    # ✅ SETTINGS

    user_settings = load_user_settings(user)
    settings_form_submitted = (
        request.method == "POST"
        and (
            "amount" in request.form
            or "capital_currency" in request.form
            or "ai_strategy" in request.form
            or "ai_risk" in request.form
            or "top_n" in request.form
            or "priority" in request.form
            or "send_buy_alerts" in request.form
            or "send_sell_alerts" in request.form
            or "block_loss_sells" in request.form
            or "pf_strategy" in request.form
            or "pf_risk" in request.form
        )
    )

    amount = parse_capital_amount(
        request.form.get("amount"),
        session.get("amount", user_settings.get("amount", 10000)),
    )
    capital_currency = (
        request.form.get("capital_currency")
        or session.get("capital_currency")
        or user_settings.get("capital_currency", "SEK")
    ).upper()
    if capital_currency not in {"SEK", "USD", "EUR"}:
        capital_currency = "SEK"
    ai_strategy = request.form.get("ai_strategy") or session.get("ai_strategy") or user_settings.get("ai_strategy", "short")
    ai_risk = request.form.get("ai_risk") or session.get("ai_risk") or user_settings.get("ai_risk", "medium")
    top_n_raw = request.form.get("top_n") or session.get("top_n") or user_settings.get("top_n", 5)
    try:
        top_n = int(top_n_raw)
    except Exception:
        top_n = 5
    priority = request.form.get("priority") or session.get("priority") or user_settings.get("priority", "mix")
    if settings_form_submitted:
        send_buy_alerts = request.form.get("send_buy_alerts") == "on"
        send_sell_alerts = request.form.get("send_sell_alerts") == "on"
        block_loss_sells = request.form.get("block_loss_sells") == "on"
    else:
        send_buy_alerts = coerce_bool_setting(
            session.get("send_buy_alerts", user_settings.get("send_buy_alerts", False)),
            default=False,
        )
        send_sell_alerts = coerce_bool_setting(
            session.get("send_sell_alerts", user_settings.get("send_sell_alerts", False)),
            default=False,
        )
        block_loss_sells = coerce_bool_setting(
            session.get("block_loss_sells", user_settings.get("block_loss_sells", False)),
            default=False,
        )
    if not is_alerts_enabled():
        send_buy_alerts = False
        send_sell_alerts = False

    session["amount"] = amount
    session["ai_strategy"] = ai_strategy
    session["ai_risk"] = ai_risk
    session["top_n"] = top_n
    session["priority"] = priority
    session["capital_currency"] = capital_currency
    session["send_buy_alerts"] = send_buy_alerts
    session["send_sell_alerts"] = send_sell_alerts
    session["block_loss_sells"] = block_loss_sells

    # Portfolio strategy/risk are independent from dashboard strategy/risk.
    pf_strategy = request.form.get("pf_strategy") or session.get("pf_strategy") or user_settings.get("pf_strategy", "short")
    pf_risk = request.form.get("pf_risk") or session.get("pf_risk") or user_settings.get("pf_risk", "medium")
    session["pf_strategy"] = pf_strategy
    session["pf_risk"] = pf_risk

    save_user_settings(
        user,
        {
            "amount": amount,
            "capital_currency": capital_currency,
            "ai_strategy": ai_strategy,
            "ai_risk": ai_risk,
            "top_n": top_n,
            "priority": priority,
            "send_buy_alerts": bool(send_buy_alerts),
            "send_sell_alerts": bool(send_sell_alerts),
            "block_loss_sells": bool(block_loss_sells),
            "pf_strategy": pf_strategy,
            "pf_risk": pf_risk,
        },
    )
    
    ranked = ai_results_cache.get("data") or ai_cache.get("data")
    ai_loading = False
    emergency_recommendations = False

    if not ranked:
        print("⚠️ AI-cache tom – försöker direkt omkörning")
        ranked = safe_fetch(lambda: run_daily_ai(ai_strategy, ai_risk, amount)) or []

        if ranked:
            print(f"✅ AI omkörning klar – laddade {len(ranked)} kandidater")
        else:
            print("⚠️ AI-scan gav inga kandidater – startar bakgrundsladdning + reservlista")
            ensure_ai_background_loading(ai_strategy, ai_risk, amount)
            ranked = build_emergency_recommendations(max(top_n, 5))
            emergency_recommendations = bool(ranked)
            ai_loading = not emergency_recommendations

    loss_blocked_tickers = get_loss_blocked_tickers(user) if block_loss_sells else set()
    ranked_for_recommendations = [s for s in ranked if s.get("t") not in loss_blocked_tickers]

    # ✅ GLOBAL TOP
    top_global = [
        s for s in ranked_for_recommendations
        if s.get("score", 0) >= 75 and s.get("trigger_score", 0) >= 2
    ][:5]

    # ✅ PORTFOLIO
    pf = []
    sell_list = []
    buy_more_list = []
    wait_list = []

    if not quick_bootstrap:
        pf = portfolio(user)

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

            decision, reason = portfolio_ai_decision(
                pl_pct,
                current_price,
                s["avg_price"],
                s["t"],
                pf_risk,
                pf_strategy
            )
            decision = normalize_portfolio_decision(decision)

            s["price"] = current_price
            s["decision"] = decision
            s["reason"] = reason
            s["pl_pct"] = pl_pct
            s["recommended_sell_qty"] = get_ai_recommended_sell_qty(s, decision, pl_pct)
            s["recommended_buy_qty"] = get_ai_recommended_buy_more_qty(s, decision, pl_pct)
            if decision == "SÄLJ" and s["recommended_sell_qty"] >= s.get("qty", 0):
                s["sell_recommendation_text"] = "AI rekommenderar: Sälj allt"
            elif decision == "SÄLJ":
                s["sell_recommendation_text"] = f"AI rekommenderar: Sälj {s['recommended_sell_qty']} av {s.get('qty', 0)}"
            else:
                s["sell_recommendation_text"] = ""

            # ✅ GENERATE PORTFOLIO ANALYSIS
            hist_data = get_historical_data(s["t"], "3mo")
            prices = []
            if hist_data:
                try:
                    prices = hist_data["chart"]["result"][0]["indicators"]["quote"][0]["close"]
                    prices = [p for p in prices if p]
                except:
                    prices = []

            s["portfolio_analysis"] = generate_portfolio_analysis(s, decision, pl_pct, prices)

            if decision == "SÄLJ":
                sell_list.append(s)
            elif decision == "KÖP MER":
                buy_more_list.append(s)
            else:
                wait_list.append(s)

    # ✅ ✅ VIKTIGT: UTANFÖR LOOPEN

    owned_symbols = {x.get("t") for x in pf}

    stock_candidates = [
        x for x in ranked_for_recommendations
        if x.get("type") != "crypto" and x.get("t") not in owned_symbols
    ]
    stock_buy_candidates = [
        x for x in stock_candidates
        if x.get("signal") == "KÖP"
    ]
    stock_buy_candidates = dedupe_by_symbol(stock_buy_candidates)

    def _fill_recommendations(primary_candidates, full_candidates, limit):
        selected = list(primary_candidates[:limit])
        if len(selected) >= limit:
            return selected

        selected_symbols = {x.get("t") for x in selected}

        # Fallback 1: include AVVAKTA KÖP when strict KÖP is too sparse.
        avvakta_kop = [
            x for x in full_candidates
            if x.get("signal") == "AVVAKTA KÖP" and x.get("t") not in selected_symbols
        ]
        for cand in avvakta_kop:
            selected.append(cand)
            selected_symbols.add(cand.get("t"))
            if len(selected) >= limit:
                return selected

        return selected

    crypto_candidates = [
        x for x in ranked_for_recommendations
        if x.get("type") == "crypto" and x.get("t") not in owned_symbols
    ]

    crypto_candidates = [
        x for x in crypto_candidates
        if x.get("trigger_score", 0) >= 2
    ]
    crypto_buy_candidates = [
        x for x in crypto_candidates
        if x.get("signal") == "KÖP"
    ]
    crypto_buy_candidates = dedupe_by_symbol(crypto_buy_candidates)

    # Ensure dashboard always has recommendation rows, even when strict KÖP is scarce.
    stock_display_candidates = _fill_recommendations(stock_buy_candidates, stock_candidates, top_n)
    crypto_display_candidates = _fill_recommendations(crypto_buy_candidates, crypto_candidates, top_n)

    # ✅ PRIORITY
    if priority == "stocks":
        stocks = stock_display_candidates[:top_n]
        crypto = []

    elif priority == "crypto":
        crypto = crypto_display_candidates[:top_n]
        stocks = []

    else:  # mix
        stocks = stock_display_candidates[:top_n]
        crypto = crypto_display_candidates[:top_n]

    # stocks = enrich_with_fundamentals(stocks)  # ⏳ DISABLED: Too slow with rate-limited API - causing dashboard timeouts
    # TODO: Implement async fundamentals fetching or client-side enrichment

    if send_buy_alerts or send_sell_alerts:
        for s in stocks:
            key = f"{user}_{s['t']}_ALERT"
            if alert_cache.get(key) == "sent":
                continue

            if send_buy_alerts and s.get("signal") == "KÖP":
                send_alert(
                    user,
                    f"📈 AI-rekommendation: KÖP {s['t']} – score {s.get('score')}",
                    "BUY"
                )
                alert_cache[key] = "sent"

            if send_sell_alerts and s.get("signal") == "SÄLJ":
                send_alert(
                    user,
                    f"⚠️ AI-rekommendation: SÄLJ {s['t']} – score {s.get('score')}",
                    "SELL"
                )
                alert_cache[key] = "sent"

    fx_rates = get_cached_or_fallback_fx_rates() if quick_bootstrap else get_usd_fx_rates()
    usd_sek_rate = float(fx_rates.get("SEK", 10.5))
    usd_eur_rate = float(fx_rates.get("EUR", 0.92))
    capital_usd_for_plan = convert_capital_to_usd(amount, capital_currency, usd_sek_rate, usd_eur_rate)

    # Add quantity recommendations used by stock/crypto cards in dashboard.
    enrich_with_buy_plan(stocks + crypto, capital_usd_for_plan, usd_sek_rate, ai_risk)

    # ✅ HANDLE BUY/SELL after recommendation lists are built
    if request.method == "POST":
        if "sell_all_holdings" in request.form:
            for item in pf:
                qty = int(item.get("qty") or 0)
                if qty > 0:
                    sell(user, item["t"], qty)
            return redirect("/dashboard?tab=portfolio")

        if "follow_ai_sell_recommendations" in request.form:
            apply_portfolio_ai_actions(user, sell_list, buy_more_list, do_sell=True, do_buy_more=False)
            return redirect("/dashboard?tab=portfolio")

        if "follow_ai_buy_more_recommendations" in request.form:
            apply_portfolio_ai_actions(user, sell_list, buy_more_list, do_sell=False, do_buy_more=True)
            return redirect("/dashboard?tab=portfolio")

        if "follow_ai_sell_and_buy_recommendations" in request.form:
            apply_portfolio_ai_actions(user, sell_list, buy_more_list, do_sell=True, do_buy_more=True)
            return redirect("/dashboard?tab=portfolio")

        for form_key in request.form.keys():
            if not form_key.startswith("sell_"):
                continue
            symbol = form_key[len("sell_"):].strip()
            if not symbol:
                continue
            qty_raw = (request.form.get(f"sellqty_{symbol}") or "").strip()
            try:
                qty = int(qty_raw)
            except Exception:
                qty = 0
            if qty > 0:
                sell(user, symbol, qty)
                return redirect("/dashboard?tab=portfolio")

        ranked_tmp = ai_results_cache.get("data") or ranked

        if "buy_ai_recommendations" in request.form:
            for s in stocks + crypto:
                qty = int(s.get("recommended_qty") or 0)
                if qty <= 0:
                    continue
                buy(user, s["t"], qty, s.get("price", 0))

            return redirect("/dashboard")

        for s in ranked_tmp:
            t = s["t"]

            if f"buy_{t}" in request.form:
                qty_raw = (request.form.get(f"buyqty_{t}") or "").strip()
                try:
                    qty = int(qty_raw)
                except Exception:
                    qty = 0
                if qty > 0:
                    buy(user, t, qty, s["price"])
                    return redirect("/dashboard")

            if f"sell_{t}" in request.form:
                qty = request.form.get(f"sellqty_{t}")
                if qty and qty.isdigit() and int(qty) > 0:
                    sell(user, t, int(qty))
                    return redirect("/dashboard?tab=portfolio")

    # ✅ GENERATE INVESTMENT ANALYSIS FOR BUY SIGNALS
    if not quick_bootstrap:
        for s in stocks + crypto:
            if s.get("signal") == "KÖP":
                # Get historical prices for better analysis
                hist_data = get_historical_data(s["t"], "3mo")
                prices = []
                if hist_data:
                    try:
                        prices = hist_data["chart"]["result"][0]["indicators"]["quote"][0]["close"]
                        prices = [p for p in prices if p]
                    except:
                        prices = []

                # Generate detailed analysis
                s["investment_analysis"] = generate_investment_analysis(s, prices)

    # ✅ USAGE
    usage = finnhub_calls.get("count", 0)
    limit = FINNHUB_LIMIT
    percent = int((usage / limit) * 100) if limit else 0

    # Passa listor till mallen så Jinja kan iterera över dem
    users_msg = session.pop("users_msg", "")
    registered_users = load_registered_users() if is_admin else []
    regular_users = []
    admin_users = []
    if is_admin:
        regular_users, admin_users = split_registered_users(registered_users)
    pending_users = load_pending_users() if is_admin else []
    if quick_bootstrap:
        min_trend_data = {
            "has_data": False,
            "summary": {
                "missing_market_price": 0,
            },
        }
        mintrend_summary_display = {
            "currency": mintrend_currency,
            "total_cost": 0.0,
            "total_value": 0.0,
            "total_pl": 0.0,
            "total_pl_pct": 0.0,
        }
        ai_loading = True
    else:
        min_trend_data = build_min_trend_data(user, ranked, mintrend_index_keys, mintrend_range_key)
        mintrend_summary_display = build_mintrend_summary_display(
            min_trend_data,
            mintrend_currency,
            usd_sek_rate,
            usd_eur_rate,
        )
    mintrend_fx_info = build_fx_info(fx_rates)

    return render_template(
        "dashboard.html",
        user=user,
        platform_links=platform_links,
        platform_names=platform_names,
        is_admin=is_admin,
        users_msg=users_msg,
        registered_users=registered_users,
        regular_users=regular_users,
        admin_users=admin_users,
        pending_users=pending_users,
        usage=usage,
        limit=limit,
        percent=percent,
        stocks=stocks,
        crypto=crypto,
        wait=[],
        sell_list=sell_list,
        buy_more_list=buy_more_list,
        wait_list=wait_list,
        amount=amount,
        capital_currency=capital_currency,
        ai_strategy=ai_strategy,
        ai_risk=ai_risk,
        pf_strategy=pf_strategy,
        pf_risk=pf_risk,
        top_n=top_n,
        priority=priority,
        send_buy_alerts=send_buy_alerts,
        send_sell_alerts=send_sell_alerts,
        block_loss_sells=block_loss_sells,
        alerts_enabled=is_alerts_enabled(),
        usd_sek=usd_sek_rate,
        top_global=top_global,
        ai_loading=ai_loading,
        quick_bootstrap=quick_bootstrap,
        emergency_recommendations=emergency_recommendations,
        ranked_count=len(ranked),
        visible_count=len(stocks) + len(crypto),
        active_tab=active_tab,
        min_trend_data=min_trend_data,
        mintrend_index_key=mintrend_index_key,
        mintrend_index_keys=mintrend_index_keys,
        mintrend_range_key=mintrend_range_key,
        mintrend_currency=mintrend_currency,
        mintrend_index_options=MIN_TREND_INDEX_OPTIONS,
        mintrend_range_options=MIN_TREND_RANGE_OPTIONS,
        mintrend_summary_display=mintrend_summary_display,
        mintrend_fx_info=mintrend_fx_info,
    )


@app.route("/portfolio", methods=["GET", "POST"])
def portfolio_page():
    user = session.get("user")
    if not user:
        return redirect("/login")

    is_admin = is_admin_email(user)
    user_platforms = get_user_trading_platforms(user)
    platform_links = build_platform_links(user_platforms)
    platform_names = build_platform_names_for_header(user_platforms)
    mintrend_index_keys = normalize_min_trend_index_keys(
        {
            "total": session.get("mintrend_index_total"),
            "recent": session.get("mintrend_index_recent"),
            "pl": session.get("mintrend_index_pl"),
            "range": session.get("mintrend_index_range"),
        },
        session.get("mintrend_index") or "STANDARD",
    )
    mintrend_index_key = mintrend_index_keys["total"]
    mintrend_range_key = normalize_min_trend_range_key(session.get("mintrend_range") or "1Y")
    mintrend_currency = (session.get("mintrend_currency") or "USD").upper()
    if mintrend_currency not in {"SEK", "USD", "EUR"}:
        mintrend_currency = "USD"

    user_settings = load_user_settings(user)
    if request.method == "POST" and (
        "pf_strategy" in request.form or "pf_risk" in request.form or "block_loss_sells" in request.form
    ):
        block_loss_sells = request.form.get("block_loss_sells") == "on"
    else:
        block_loss_sells = coerce_bool_setting(
            session.get("block_loss_sells", user_settings.get("block_loss_sells", False)),
            default=False,
        )

    ranked = ai_results_cache.get("data") or ai_cache.get("data") or []
    ai_loading = False
    if not ranked:
        print("⚠️ AI-cache tom i portfolio – använder snabb vy")
        ai_loading = True
        ensure_ai_background_loading(
            session.get("ai_strategy", "short"),
            session.get("ai_risk", "medium"),
            session.get("amount", 10000),
        )

    if request.method == "POST":
        for s in ranked:
            t = s["t"]

            if f"buy_{t}" in request.form:
                qty = request.form.get(f"buyqty_{t}")
                if qty and qty.isdigit() and int(qty) > 0:
                    buy(user, t, int(qty), s["price"])

            if f"sell_{t}" in request.form:
                qty = request.form.get(f"sellqty_{t}")
                if qty and qty.isdigit() and int(qty) > 0:
                    sell(user, t, int(qty))

    pf_strategy = request.form.get("pf_strategy") or session.get("pf_strategy", "short")
    pf_risk = request.form.get("pf_risk") or session.get("pf_risk", "medium")
    session["pf_strategy"] = pf_strategy
    session["pf_risk"] = pf_risk
    session["block_loss_sells"] = block_loss_sells

    save_user_settings(
        user,
        {
            "pf_strategy": pf_strategy,
            "pf_risk": pf_risk,
            "block_loss_sells": bool(block_loss_sells),
        },
    )

    pf = portfolio(user)

    sell_list = []
    buy_more_list = []
    wait_list = []

    for s in pf:
        match = next((x for x in ranked if x["t"] == s["t"]), None)
        if match:
            s["name"] = match.get("name", s["t"])
            s["display_name"] = match.get("display_name", f"{s['name']} ({s['t']})")

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
            pf_risk,
            pf_strategy
        )
        decision = normalize_portfolio_decision(decision)

        s["price"] = current_price
        s["reason"] = reason
        s["signal"] = decision
        s["pl_pct"] = pl_pct
        s["recommended_sell_qty"] = get_ai_recommended_sell_qty(s, decision, pl_pct)
        s["recommended_buy_qty"] = get_ai_recommended_buy_more_qty(s, decision, pl_pct)
        if decision == "SÄLJ" and s["recommended_sell_qty"] >= s.get("qty", 0):
            s["sell_recommendation_text"] = "AI rekommenderar: Sälj allt"
        elif decision == "SÄLJ":
            s["sell_recommendation_text"] = f"AI rekommenderar: Sälj {s['recommended_sell_qty']} av {s.get('qty', 0)}"
        else:
            s["sell_recommendation_text"] = ""

        if decision == "SÄLJ":
            sell_list.append(s)
        elif decision == "KÖP MER":
            buy_more_list.append(s)
        else:
            wait_list.append(s)

    if request.method == "POST":
        if "sell_all_holdings" in request.form:
            for item in pf:
                qty = int(item.get("qty") or 0)
                if qty > 0:
                    sell(user, item["t"], qty)
            return redirect("/dashboard?tab=portfolio")

        if "follow_ai_sell_recommendations" in request.form:
            apply_portfolio_ai_actions(user, sell_list, buy_more_list, do_sell=True, do_buy_more=False)
            return redirect("/dashboard?tab=portfolio")

        if "follow_ai_buy_more_recommendations" in request.form:
            apply_portfolio_ai_actions(user, sell_list, buy_more_list, do_sell=False, do_buy_more=True)
            return redirect("/dashboard?tab=portfolio")

        if "follow_ai_sell_and_buy_recommendations" in request.form:
            apply_portfolio_ai_actions(user, sell_list, buy_more_list, do_sell=True, do_buy_more=True)
            return redirect("/dashboard?tab=portfolio")

    send_buy_alerts = coerce_bool_setting(session.get("send_buy_alerts", False), default=False)
    send_sell_alerts = coerce_bool_setting(session.get("send_sell_alerts", False), default=False)
    if not is_alerts_enabled():
        send_buy_alerts = False
        send_sell_alerts = False

    registered_users = load_registered_users() if is_admin else []
    regular_users, admin_users = split_registered_users(registered_users) if is_admin else ([], [])
    pending_users = load_pending_users() if is_admin else []
    min_trend_data = build_min_trend_data(user, ranked, mintrend_index_keys, mintrend_range_key)
    fx_rates = get_usd_fx_rates()
    usd_sek_rate = float(fx_rates.get("SEK", 10.5))
    usd_eur_rate = float(fx_rates.get("EUR", 0.92))
    mintrend_summary_display = build_mintrend_summary_display(
        min_trend_data,
        mintrend_currency,
        usd_sek_rate,
        usd_eur_rate,
    )
    mintrend_fx_info = build_fx_info(fx_rates)

    # ✅ Smart alerts (ingen spam)
    for s in sell_list:
        key = f"{user}_{s['t']}_SELL"
        if send_sell_alerts and alert_cache.get(key) != "sent":
            send_alert(user, f"🚨 SÄLJ {s['t']} – du äger denna", "SELL")
            alert_cache[key] = "sent"

    for s in buy_more_list:
        key = f"{user}_{s['t']}_BUYMORE"
        if send_buy_alerts and alert_cache.get(key) != "sent":
            send_alert(user, f"📈 KÖP MER {s['t']} – stark trend", "BUYMORE")
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
        "dashboard.html",
        user=user,
        platform_links=platform_links,
        platform_names=platform_names,
        is_admin=is_admin,
        users_msg="",
        registered_users=registered_users,
        regular_users=regular_users,
        admin_users=admin_users,
        pending_users=pending_users,
        usd_sek=usd_sek_rate,
        sell_list=sell_list,
        buy_more_list=buy_more_list,
        wait_list=wait_list,
        pf_strategy=pf_strategy,
        pf_risk=pf_risk,
        capital_currency=session.get("capital_currency", "SEK"),
        block_loss_sells=block_loss_sells,
        send_buy_alerts=send_buy_alerts,
        send_sell_alerts=send_sell_alerts,
        alerts_enabled=is_alerts_enabled(),
        quick_bootstrap=False,
        active_tab="portfolio",
        min_trend_data=min_trend_data,
        mintrend_index_key=mintrend_index_key,
        mintrend_index_keys=mintrend_index_keys,
        mintrend_range_key=mintrend_range_key,
        mintrend_currency=mintrend_currency,
        mintrend_index_options=MIN_TREND_INDEX_OPTIONS,
        mintrend_range_options=MIN_TREND_RANGE_OPTIONS,
        mintrend_summary_display=mintrend_summary_display,
        mintrend_fx_info=mintrend_fx_info,
        # ge tomma listor för att undvika Jinja-fel om sidan renderas utan AI-data
        stocks=[],
        crypto=[],
        wait=[],
    )


@app.route("/analysis_search", methods=["GET", "POST"])
def analysis_search():

    user = session.get("user")
    if not user:
        return redirect("/login")

    is_admin = is_admin_email(user)
    user_platforms = get_user_trading_platforms(user)
    platform_names = build_platform_names_for_header(user_platforms)
    fx_rates = get_usd_fx_rates()
    usd_sek_rate = float(fx_rates.get("SEK", 10.5))
    usd_eur_rate = float(fx_rates.get("EUR", 0.92))

    display_currency = (request.form.get("analysis_currency") or session.get("analysis_currency") or "SEK").upper()
    if display_currency not in {"SEK", "USD", "EUR"}:
        display_currency = "SEK"
    session["analysis_currency"] = display_currency

    row_count = 8
    analysis_rows = []
    for idx in range(1, row_count + 1):
        input_value = request.form.get(f"analysis_input_{idx}", "") if request.method == "POST" else ""
        action_value = request.form.get(f"analysis_action_{idx}", "kopa") if request.method == "POST" else "kopa"
        result = build_manual_analysis_row(input_value, action_value)
        result["index"] = idx
        analysis_rows.append(result)

    capital_amount_value = request.form.get("analysis_capital") if request.method == "POST" else None
    if not capital_amount_value:
        capital_amount_value = session.get("analysis_capital", session.get("amount", 10000))
    session["analysis_capital"] = parse_capital_amount(capital_amount_value, 10000)

    apply_manual_analysis_buy_plan(analysis_rows, session["analysis_capital"], display_currency)
    analysis_summary = build_analysis_cost_summary(analysis_rows, display_currency, usd_sek_rate, usd_eur_rate)

    submitted = request.method == "POST"
    filled_rows = sum(1 for row in analysis_rows if row.get("status") not in {"empty"})
    trade_action = (request.form.get("trade_action") or "").strip().lower()

    def _parse_qty(raw_value, fallback_value=0):
        txt = (raw_value or "").strip()
        if not txt:
            return int(fallback_value or 0)
        try:
            qty = int(float(txt))
        except Exception:
            qty = 0
        return max(0, qty)

    if request.method == "POST" and trade_action in {"buy_one", "buy_all"}:
        bought = []
        target_index = request.form.get("target_index")
        target_row = None
        if trade_action == "buy_one" and target_index:
            try:
                idx_value = int(target_index)
            except Exception:
                idx_value = 0
            target_row = next((row for row in analysis_rows if row.get("index") == idx_value), None)

        candidate_rows = analysis_rows if trade_action == "buy_all" else ([target_row] if target_row else [])
        for row in candidate_rows:
            if not row or row.get("status") != "ok":
                continue

            qty_key = f"buyqty_{row['index']}"
            qty = _parse_qty(request.form.get(qty_key), row.get("recommended_qty", 0))
            if qty <= 0:
                continue

            buy(user, row["symbol"], qty, row.get("price", 0))
            bought.append(
                {
                    "index": row["index"],
                    "symbol": row["symbol"],
                    "qty": qty,
                    "price": row.get("price", 0),
                }
            )

        payload = {
            "ok": True,
            "bought": bought,
            "redirect_url": "/dashboard?tab=portfolio",
            "message": "Köpet har registrerats och ligger nu i portfolio.",
            "analysis_currency": display_currency,
        }

        if request.headers.get("X-Requested-With") == "analysis-search":
            return jsonify(payload)

        return redirect("/dashboard?tab=portfolio")

    if request.method == "POST" and request.headers.get("X-Requested-With") == "analysis-search":
        return jsonify(
            {
                "ok": True,
                "filled_rows": filled_rows,
                "row_count": row_count,
                "analysis_rows": analysis_rows,
                "analysis_summary": analysis_summary,
                "analysis_currency": display_currency,
            }
        )

    return render_template(
        "analysis_search.html",
        user=user,
        platform_names=platform_names,
        is_admin=is_admin,
        analysis_rows=analysis_rows,
        row_count=row_count,
        submitted=submitted,
        filled_rows=filled_rows,
        analysis_summary=analysis_summary,
        analysis_currency=display_currency,
        analysis_capital=session["analysis_capital"],
        usd_sek_rate=usd_sek_rate,
        active_page="analysis_search",
    )


# ===== HOME =====

@app.route("/")
def home():
    return redirect("/login")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))

    # ✅ Auto refresh AI (AVSTÄNGD för debugging)
    def auto_refresh_ai():
        while True:
            print("🔄 Auto-refresh AI")
            safe_fetch(lambda: run_daily_ai("short", "medium", 10000))
            time.sleep(1800)

    threading.Thread(target=auto_refresh_ai, daemon=True).start()

    # ✅ Preload AI
    def preload_ai():
        print("🚀 Preloading AI...")
        safe_fetch(lambda: run_daily_ai("short", "medium", 10000))

    threading.Thread(target=preload_ai, daemon=True).start()

    # ✅ Background scanner (AVSTÄNGD för nu)
    # t = threading.Thread(target=scan_market_background)
    # t.daemon = True

    app.run(host="0.0.0.0", port=port)