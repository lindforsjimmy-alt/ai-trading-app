-- PostgreSQL schema for AI-bors
-- Safe to run multiple times.

CREATE TABLE IF NOT EXISTS users (
  id BIGSERIAL PRIMARY KEY,
  email TEXT NOT NULL,
  password_hash TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT 'user' CHECK (role IN ('user', 'admin')),
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('pending', 'active', 'disabled')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  approved_at TIMESTAMPTZ NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_users_email_lower
  ON users ((LOWER(email)));

CREATE TABLE IF NOT EXISTS user_trading_platforms (
  user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  platform_key TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (user_id, platform_key)
);

CREATE TABLE IF NOT EXISTS trades (
  id BIGSERIAL PRIMARY KEY,
  user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  ticker TEXT NOT NULL,
  side TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
  qty NUMERIC(20,8) NOT NULL CHECK (qty > 0),
  price NUMERIC(20,8) NOT NULL CHECK (price > 0),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_trades_user_created
  ON trades (user_id, created_at DESC);

CREATE INDEX IF NOT EXISTS ix_trades_user_ticker
  ON trades (user_id, ticker);

CREATE TABLE IF NOT EXISTS user_settings (
  user_id BIGINT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  amount NUMERIC(18,2) NOT NULL DEFAULT 10000,
  capital_currency TEXT NOT NULL DEFAULT 'SEK' CHECK (capital_currency IN ('SEK', 'USD', 'EUR')),
  ai_strategy TEXT NOT NULL DEFAULT 'short',
  ai_risk TEXT NOT NULL DEFAULT 'medium',
  top_n INTEGER NOT NULL DEFAULT 5 CHECK (top_n >= 1 AND top_n <= 100),
  priority TEXT NOT NULL DEFAULT 'mix',
  send_buy_alerts BOOLEAN NOT NULL DEFAULT FALSE,
  send_sell_alerts BOOLEAN NOT NULL DEFAULT FALSE,
  pf_strategy TEXT NOT NULL DEFAULT 'short',
  pf_risk TEXT NOT NULL DEFAULT 'medium',
  mintrend_index_total TEXT NOT NULL DEFAULT 'STANDARD',
  mintrend_index_recent TEXT NOT NULL DEFAULT 'STANDARD',
  mintrend_index_pl TEXT NOT NULL DEFAULT 'STANDARD',
  mintrend_index_range TEXT NOT NULL DEFAULT 'STANDARD',
  mintrend_range TEXT NOT NULL DEFAULT '1Y',
  mintrend_currency TEXT NOT NULL DEFAULT 'USD' CHECK (mintrend_currency IN ('SEK', 'USD', 'EUR')),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
