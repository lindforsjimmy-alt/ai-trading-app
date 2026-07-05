import argparse
import json
import os
from pathlib import Path

import psycopg

DEFAULT_PLATFORMS = ["Avanza", "Safello"]


def parse_user_line(raw_line):
    parts = (raw_line or "").strip().split("|")
    if len(parts) < 2:
        return None
    email = (parts[0] or "").strip().lower()
    password_hash = (parts[1] or "").strip()
    if not email or not password_hash:
        return None
    platform_blob = (parts[2] or "").strip() if len(parts) >= 3 else ""
    platforms = [p.strip() for p in platform_blob.split(",") if p.strip()] or list(DEFAULT_PLATFORMS)
    return {"email": email, "password_hash": password_hash, "platforms": platforms}


def read_lines(path):
    if not path.exists():
        return []
    return path.read_text(encoding="utf-8", errors="ignore").splitlines()


def load_json(path):
    if not path.exists():
        return {}
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw) if raw.strip() else {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def ensure_schema(conn, schema_sql):
    with conn.cursor() as cur:
        cur.execute(schema_sql)


def upsert_user(cur, email, password_hash, role, status):
    cur.execute(
        """
        INSERT INTO users (email, password_hash, role, status, approved_at)
        VALUES (%s, %s, %s, %s, CASE WHEN %s = 'active' THEN NOW() ELSE NULL END)
        ON CONFLICT ((LOWER(email))) DO UPDATE
        SET password_hash = EXCLUDED.password_hash,
            role = CASE
                WHEN users.role = 'admin' OR EXCLUDED.role = 'admin' THEN 'admin'
                ELSE users.role
            END,
            status = CASE
                WHEN users.status = 'active' OR EXCLUDED.status = 'active' THEN 'active'
                ELSE EXCLUDED.status
            END,
            approved_at = CASE
                WHEN users.status = 'active' OR EXCLUDED.status = 'active' THEN COALESCE(users.approved_at, NOW())
                ELSE users.approved_at
            END
        RETURNING id
        """,
        (email, password_hash, role, status, status),
    )
    return cur.fetchone()[0]


def set_platforms(cur, user_id, platforms):
    cur.execute("DELETE FROM user_trading_platforms WHERE user_id = %s", (user_id,))
    for p in dict.fromkeys(platforms):
        cur.execute(
            "INSERT INTO user_trading_platforms (user_id, platform_key) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            (user_id, p),
        )


def insert_trade(cur, user_id, ticker, qty, price):
    cur.execute(
        """
        INSERT INTO trades (user_id, ticker, side, qty, price)
        VALUES (%s, %s, 'BUY', %s, %s)
        """,
        (user_id, ticker, qty, price),
    )


def upsert_settings(cur, user_id, settings):
    amount = float(settings.get("amount", 10000))
    currency = str(settings.get("capital_currency", "SEK")).upper()
    if currency not in {"SEK", "USD", "EUR"}:
        currency = "SEK"

    ai_strategy = str(settings.get("ai_strategy", "short"))
    ai_risk = str(settings.get("ai_risk", "medium"))
    top_n = int(settings.get("top_n", 5) or 5)
    top_n = max(1, min(top_n, 100))
    priority = str(settings.get("priority", "mix"))
    send_buy_alerts = bool(settings.get("send_buy_alerts", False))
    send_sell_alerts = bool(settings.get("send_sell_alerts", False))
    block_loss_sells = bool(settings.get("block_loss_sells", False))
    pf_strategy = str(settings.get("pf_strategy", "short"))
    pf_risk = str(settings.get("pf_risk", "medium"))

    mintrend_index_total = str(settings.get("mintrend_index_total", "STANDARD")).upper()
    mintrend_index_recent = str(settings.get("mintrend_index_recent", "STANDARD")).upper()
    mintrend_index_pl = str(settings.get("mintrend_index_pl", "STANDARD")).upper()
    mintrend_index_range = str(settings.get("mintrend_index_range", "STANDARD")).upper()
    mintrend_range = str(settings.get("mintrend_range", "1Y")).upper()
    mintrend_currency = str(settings.get("mintrend_currency", "USD")).upper()
    if mintrend_currency not in {"SEK", "USD", "EUR"}:
        mintrend_currency = "USD"

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
            user_id, amount, currency, ai_strategy, ai_risk, top_n, priority,
            send_buy_alerts, send_sell_alerts, block_loss_sells, pf_strategy, pf_risk,
            mintrend_index_total, mintrend_index_recent, mintrend_index_pl, mintrend_index_range,
            mintrend_range, mintrend_currency,
        ),
    )


def main():
    parser = argparse.ArgumentParser(description="Migrate file-based data to PostgreSQL")
    parser.add_argument("--data-dir", default="stock_data", help="Path to stock_data folder")
    parser.add_argument("--schema", default="db_schema.sql", help="Path to schema SQL file")
    parser.add_argument("--dry-run", action="store_true", help="Validate input and report counts without writing")
    args = parser.parse_args()

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise SystemExit("DATABASE_URL is missing. Add it to your environment first.")

    data_dir = Path(args.data_dir)
    users_file = data_dir / "users.txt"
    admins_file = data_dir / "admins.txt"
    pending_file = data_dir / "pending.txt"
    trades_file = data_dir / "my_trades.txt"
    settings_file = data_dir / "user_settings.json"

    users_lines = read_lines(users_file)
    pending_lines = read_lines(pending_file)
    admin_emails = {line.strip().lower() for line in read_lines(admins_file) if line.strip()}
    settings_map = load_json(settings_file)

    active_users = [rec for rec in (parse_user_line(l) for l in users_lines) if rec]
    pending_users = [rec for rec in (parse_user_line(l) for l in pending_lines) if rec]

    valid_trades = []
    for raw in read_lines(trades_file):
        parts = raw.strip().split("|")
        if len(parts) < 4:
            continue
        email = (parts[0] or "").strip().lower()
        ticker = (parts[1] or "").strip().upper()
        try:
            qty = float(parts[2])
            price = float(parts[3])
        except Exception:
            continue
        if not email or not ticker or qty <= 0 or price <= 0:
            continue
        valid_trades.append((email, ticker, qty, price))

    if args.dry_run:
        print("Dry-run summary:")
        print(f"  active users: {len(active_users)}")
        print(f"  pending users: {len(pending_users)}")
        print(f"  admin emails: {len(admin_emails)}")
        print(f"  trades: {len(valid_trades)}")
        print(f"  settings users: {len(settings_map)}")
        return

    schema_sql = Path(args.schema).read_text(encoding="utf-8")

    with psycopg.connect(database_url) as conn:
        with conn.cursor() as cur:
            ensure_schema(conn, schema_sql)

            email_to_id = {}

            for rec in active_users:
                role = "admin" if rec["email"] in admin_emails else "user"
                user_id = upsert_user(cur, rec["email"], rec["password_hash"], role, "active")
                set_platforms(cur, user_id, rec["platforms"])
                email_to_id[rec["email"]] = user_id

            for rec in pending_users:
                role = "admin" if rec["email"] in admin_emails else "user"
                user_id = upsert_user(cur, rec["email"], rec["password_hash"], role, "pending")
                set_platforms(cur, user_id, rec["platforms"])
                email_to_id[rec["email"]] = user_id

            for email in admin_emails:
                if email in email_to_id:
                    continue
                user_id = upsert_user(cur, email, "ADMIN_ONLY_PLACEHOLDER_HASH", "admin", "active")
                email_to_id[email] = user_id

            for email, ticker, qty, price in valid_trades:
                user_id = email_to_id.get(email)
                if user_id is None:
                    user_id = upsert_user(cur, email, "MIGRATED_PLACEHOLDER_HASH", "user", "active")
                    email_to_id[email] = user_id
                insert_trade(cur, user_id, ticker, qty, price)

            for email, settings in settings_map.items():
                target = (email or "").strip().lower()
                if not target or not isinstance(settings, dict):
                    continue
                user_id = email_to_id.get(target)
                if user_id is None:
                    user_id = upsert_user(cur, target, "MIGRATED_PLACEHOLDER_HASH", "user", "active")
                    email_to_id[target] = user_id
                upsert_settings(cur, user_id, settings)

        conn.commit()

    print("Migration complete.")
    print(f"Users migrated or upserted: {len(email_to_id)}")
    print(f"Trades migrated: {len(valid_trades)}")


if __name__ == "__main__":
    main()
