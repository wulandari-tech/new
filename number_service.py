# ═══════════════════════════════════════════════════════════
# 📱 Number Service — Stock management & Excel import
# ═══════════════════════════════════════════════════════════

import os
import logging
import re

logger = logging.getLogger(__name__)


def _normalize_text(value):
    return str(value).strip() if value is not None else ""


def _parse_rate(value):
    if value in (None, ""):
        return 0.0
    cleaned = re.sub(r"[^0-9.,-]", "", str(value)).replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def _parse_phone_number(value):
    if value is None:
        return None
    digits = re.sub(r"\D", "", str(value))
    if not digits:
        return None
    return digits


def normalize_country_name(value):
    text = _normalize_text(value).upper()
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return ""
    tokens = text.split()
    while tokens and tokens[-1].isdigit():
        tokens.pop()
    prefixes = {"CTAX", "COUNTRY", "RANGE", "TITLE", "OPERATOR"}
    while tokens and tokens[0] in prefixes:
        tokens.pop(0)
    normalized = " ".join(tokens).strip()
    return normalized or text


def _extract_country(range_name):
    text = _normalize_text(range_name)
    if not text:
        return ""
    return normalize_country_name(text)


def _detect_header_map(rows):
    for index, row in enumerate(rows):
        normalized = [_normalize_text(cell).lower() for cell in row]
        if not any(normalized):
            continue

        range_idx = next(
            (i for i, cell in enumerate(normalized) if any(key in cell for key in ["range", "country", "operator"])),
            None,
        )
        number_idx = next(
            (i for i, cell in enumerate(normalized) if any(key in cell for key in ["number", "phone", "msisdn", "nomor"])),
            None,
        )
        rate_idx = next(
            (i for i, cell in enumerate(normalized) if any(key in cell for key in ["rate", "price", "tariff", "harga"])),
            None,
        )
        if range_idx is not None and number_idx is not None:
            return {
                "header_index": index,
                "range_idx": range_idx,
                "number_idx": number_idx,
                "rate_idx": rate_idx,
            }
    return None


def load_numbers_from_excel(filepath):
    """Parse Excel file dan return list of number dicts."""
    if not os.path.exists(filepath):
        logger.error(f"❌ File tidak ditemukan: {filepath}")
        return []

    try:
        import openpyxl
        wb = openpyxl.load_workbook(filepath, read_only=True)
        ws = wb.active
        rows = list(ws.iter_rows(values_only=True))
        header_map = _detect_header_map(rows)

        if header_map:
            start_index = header_map["header_index"] + 1
            range_idx = header_map["range_idx"]
            number_idx = header_map["number_idx"]
            rate_idx = header_map["rate_idx"]
        else:
            start_index = 3
            range_idx = 0
            number_idx = 1
            rate_idx = 2

        numbers = []
        for row in rows[start_index:]:
            if not row:
                continue

            if max(range_idx, number_idx, rate_idx or 0) >= len(row):
                continue

            range_name = row[range_idx]
            number = row[number_idx]
            rate = row[rate_idx] if rate_idx is not None and rate_idx < len(row) else None

            if not range_name or not number:
                continue

            phone_number = _parse_phone_number(number)
            if not phone_number:
                continue

            range_name_text = _normalize_text(range_name)
            if not range_name_text or range_name_text.lower() in {"range", "country", "operator"}:
                continue

            numbers.append({
                "phone_number": phone_number,
                "country": _extract_country(range_name_text),
                "range_name": range_name_text,
                "rate": _parse_rate(rate),
            })

        wb.close()
        logger.info(f"📱 Parsed {len(numbers)} numbers from {filepath}")
        return numbers

    except Exception as e:
        logger.error(f"❌ Error parsing Excel: {e}")
        return []


def init_stock(db, excel_path="numbers.xlsx"):
    """Initialize stock from Excel file if database is empty."""
    current_stock = db.get_total_stock()
    if current_stock > 0:
        logger.info(f"📦 Stock already has {current_stock} numbers")
        return current_stock

    numbers = load_numbers_from_excel(excel_path)
    if numbers:
        added = db.add_numbers(numbers)
        logger.info(f"✅ Initialized stock with {added} numbers")
        return added

    return 0
