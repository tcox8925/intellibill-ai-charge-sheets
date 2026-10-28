from __future__ import annotations

import datetime as dt
import re


def parse_date(value: str | None) -> tuple[dt.date | None, str | None, list[str]]:
    raw = (value or "").strip()
    if not raw:
        return None, raw, []
    parts = [p for p in re.split(r"[^0-9]+", raw) if p]
    if len(parts) != 3:
        return None, raw, ["date_unparseable"]
    try:
        month, day, year = map(int, parts)
    except ValueError:
        return None, raw, ["date_unparseable"]
    normalized_year = False
    if len(parts[2]) <= 2:
        cur2 = dt.date.today().year % 100
        year = 2000 + year if year <= cur2 else 1900 + year
        normalized_year = True
    try:
        parsed = dt.date(year, month, day)
    except ValueError:
        return None, raw, ["date_impossible"]
    normalized = f"{month:02d}-{day:02d}-{year:04d}"
    flags = ["date_year_normalized"] if normalized_year else []
    return parsed, normalized, flags


def normalize_header_dates(header: dict) -> tuple[object | None, object | None, list[str]]:
    flags: list[str] = []
    svc, svc_norm, svc_flags = parse_date(header.get("date"))
    dob, dob_norm, dob_flags = parse_date(header.get("dob"))
    if svc_norm and svc:
        header["date"] = svc_norm
    if dob_norm and dob:
        header["dob"] = dob_norm
    flags.extend("service_" + f for f in svc_flags)
    flags.extend("dob_" + f for f in dob_flags)
    today = dt.date.today()
    if svc and svc > today:
        flags.append("service_date_future")
    if dob:
        if dob > today:
            flags.append("dob_future")
        elif dob.year < 1900 or (today - dob).days / 365.25 > 120:
            flags.append("dob_implausible")
        if svc and dob > svc:
            flags.append("dob_after_service_date")
    return svc, dob, flags
