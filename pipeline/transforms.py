# transforms.py

import pandas as pd
import re

THAI_MONTH_MAP = {
    "ม.ค.": 1, "ม.ค": 1, "มกราคม": 1, "มกรา": 1, "มกร": 1, "jan": 1, "january": 1, "1": 1, "01": 1,
    "ก.พ.": 2, "ก.พ": 2, "กุมภาพันธ์": 2, "กุมภา": 2, "กุมภ": 2, "feb": 2, "february": 2, "2": 2, "02": 2,
    "มี.ค.": 3, "มี.ค": 3, "มีนาคม": 3, "มีนา": 3, "mar": 3, "march": 3, "3": 3, "03": 3,
    "เม.ย.": 4, "เม.ย": 4, "เมษายน": 4, "เมษา": 4, "apr": 4, "april": 4, "4": 4, "04": 4,
    "พ.ค.": 5, "พ.ค": 5, "พฤษภาคม": 5, "พฤษภา": 5, "may": 5, "5": 5, "05": 5,
    "มิ.ย.": 6, "มิ.ย": 6, "มิถุนายน": 6, "มิถุนา": 6, "jun": 6, "june": 6, "6": 6, "06": 6,
    "ก.ค.": 7, "ก.ค": 7, "กรกฎาคม": 7, "กรกฎา": 7, "jul": 7, "july": 7, "7": 7, "07": 7, 'กรกฏาคม': 7,
    "ส.ค.": 8, "ส.ค": 8, "สิงหาคม": 8, "สิงหา": 8, "aug": 8, "august": 8, "8": 8, "08": 8,
    "ก.ย.": 9, "ก.ย": 9, "กันยายน": 9, "กันยา": 9, "sep": 9, "sept": 9, "september": 9, "9": 9, "09": 9,
    "ต.ค.": 10, "ต.ค": 10, "ตุลาคม": 10, "ตุลา": 10, "oct": 10, "october": 10, "10": 10,
    "พ.ย.": 11, "พ.ย": 11, "พฤศจิกายน": 11, "พฤศจิกา": 11, "nov": 11, "november": 11, "11": 11,
    "ธ.ค.": 12, "ธ.ค": 12, "ธันวาคม": 12, "ธันวา": 12, "dec": 12, "december": 12, "12": 12,
}

def try_datetime(series, threshold=0.7):
    total = series.dropna().shape[0]
    if total == 0:
        return None

    pattern = r"^\d{4}-\d{2}-\d{2}$"
    cleaned = series.astype(str).str.strip()
    match_ratio = cleaned.str.match(pattern).mean()

    if match_ratio < threshold:
        return None

    dt = pd.to_datetime(cleaned, format="%Y-%m-%d", errors="coerce")
    success_ratio = dt.notna().sum() / total

    if success_ratio >= threshold:
        return dt

    return None

def smart_cast_column(series, threshold=0.9):

    total = len(series.dropna())
    if total == 0:
        return series

    try_int = pd.to_numeric(series, errors="coerce")
    if (try_int.notna().sum() / total) >= threshold:
        return try_int.round(0).astype("Int64")

    try_float = pd.to_numeric(series, errors="coerce")
    if (try_float.notna().sum() / total) >= threshold:
        return try_float

    dt = try_datetime(series, threshold)
    if dt is not None:
        return dt

    lowered = series.astype(str).str.lower()
    if lowered.isin(["true", "false", "0", "1"]).mean() >= threshold:
        return lowered.map({"true": True, "false": False, "1": True, "0": False})

    return series.astype(str)

def transform_thai_month(df):
    for col in df.columns:
        sample = df[col].astype(str).str.strip().str.lower()
        mapped = sample.map(THAI_MONTH_MAP)
        if mapped.notna().mean() >= 0.8:
            df[f"{col}_NUM"] = mapped.astype("Int64")
    return df

def normalize_dtypes(df):
    for col in df.columns:
        df[col] = smart_cast_column(df[col])
    return df