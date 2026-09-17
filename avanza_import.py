from __future__ import annotations

import csv
import hashlib
import io


REQUIRED_COLUMNS = frozenset(
    {
        "Datum",
        "Konto",
        "Typ av transaktion",
        "Värdepapper/beskrivning",
        "Antal",
        "Kurs",
        "Transaktionsvaluta",
        "ISIN",
    }
)
SUPPORTED_TRANSACTION_TYPES = {"Köp": "BUY", "Sälj": "SELL"}
FUND_NAME_PREFIXES = ("avanza auto", "lansforsakringar", "länsförsäkringar")
CRYPTO_KEYWORDS = ("bitcoin", "ethereum", "solana", "hyperliquid", "valour")


def _number(value: str) -> float | None:
    cleaned = (value or "").strip().replace(" ", "").replace("\u00a0", "").replace(",", ".")
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _asset_type(security: str) -> str:
    normalized = security.lower()
    if normalized.startswith(FUND_NAME_PREFIXES):
        return "fund"
    if any(keyword in normalized for keyword in CRYPTO_KEYWORDS):
        return "crypto"
    return "stock"


def parse_avanza_csv(raw_bytes: bytes) -> tuple[list[dict[str, object]], list[str]]:
    text = raw_bytes.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text), delimiter=";")
    headers = set(reader.fieldnames or [])
    if not REQUIRED_COLUMNS.issubset(headers):
        text = raw_bytes.decode("cp1252", errors="replace")
        reader = csv.DictReader(io.StringIO(text), delimiter=";")
        headers = set(reader.fieldnames or [])
    missing = REQUIRED_COLUMNS - headers
    if missing:
        raise ValueError(f"Avanza-filen saknar kolumner: {', '.join(sorted(missing))}.")

    transactions: list[dict[str, object]] = []
    skipped: list[str] = []
    for row_number, row in enumerate(reader, start=2):
        transaction_type = (row.get("Typ av transaktion") or "").strip()
        side = SUPPORTED_TRANSACTION_TYPES.get(transaction_type)
        if not side:
            skipped.append(f"Rad {row_number}: {transaction_type or 'okand transaktionstyp'}")
            continue

        quantity = _number(row.get("Antal") or "")
        price = _number(row.get("Kurs") or "")
        security = (row.get("Värdepapper/beskrivning") or "").strip()
        isin = (row.get("ISIN") or "").strip().upper()
        if not security or not isin or quantity is None or price is None or quantity == 0 or price <= 0:
            skipped.append(f"Rad {row_number}: saknar giltigt vardepapper, ISIN, antal eller kurs")
            continue
        asset_type = _asset_type(security)
        if asset_type == "fund":
            skipped.append(f"Rad {row_number}: fonden {security} importeras inte till aktier eller krypto")
            continue

        fingerprint_source = "|".join(
            str(row.get(column) or "").strip()
            for column in reader.fieldnames or []
        )
        transactions.append(
            {
                "fingerprint": hashlib.sha256(fingerprint_source.encode("utf-8")).hexdigest(),
                "date": (row.get("Datum") or "").strip(),
                "account": (row.get("Konto") or "").strip(),
                "side": side,
                "security": security,
                "asset_type": asset_type,
                "isin": isin,
                "quantity": abs(quantity),
                "price": price,
                "currency": (row.get("Instrumentvaluta") or row.get("Transaktionsvaluta") or "").strip().upper(),
                "fee": _number(row.get("Courtage") or "") or 0.0,
            }
        )

    transactions.sort(key=lambda item: str(item["date"]))
    return transactions, skipped


def parse_symbol_mappings(raw_value: str) -> dict[str, str]:
    mappings: dict[str, str] = {}
    for line in (raw_value or "").splitlines():
        if "=" not in line:
            continue
        isin, symbol = line.split("=", 1)
        isin = isin.strip().upper()
        symbol = symbol.strip().upper()
        if isin and symbol:
            mappings[isin] = symbol
    return mappings