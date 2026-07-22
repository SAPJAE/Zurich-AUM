import argparse
import asyncio
import io
import json
import math
import re
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

try:
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError
    from playwright.async_api import async_playwright
except ModuleNotFoundError:
    PlaywrightTimeoutError = TimeoutError
    async_playwright = None


SOURCE_URL = "https://digital.feprecisionplus.com/zilme/en-gb/ZILME"
SOURCE_ORIGIN = "https://digital.feprecisionplus.com"
DOWNLOAD_URL = "https://digital.feprecisionplus.com/zilme/en-gb/ZILME/DownloadTool?citicode={code}&historyType=price"
MIN_EXPECTED_FUNDS = 150
ACTIVE_PRICE_STALENESS_DAYS = 14
HISTORY_YEARS = 5


def numeric_from_display(value):
    match = re.search(r"[-+]?\d[\d,]*(?:\.\d+)?", str(value or ""))
    return float(match.group(0).replace(",", "")) if match else None


def parse_display_date(value):
    value = str(value or "").strip()
    for fmt in ("%d %b %Y", "%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            pass
    return None


def display_date(value):
    parsed = parse_display_date(value)
    if parsed:
        return parsed.strftime("%d %b %Y")
    try:
        return date.fromisoformat(str(value)).strftime("%d %b %Y")
    except ValueError:
        return str(value or "").strip()


def direct_request(url, *, method="GET", data=None, headers=None, timeout=120):
    default_headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"
        ),
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Referer": SOURCE_URL,
        "X-Requested-With": "XMLHttpRequest",
    }
    if headers:
        default_headers.update(headers)
    body = data.encode("utf-8") if isinstance(data, str) else data
    request = Request(url, data=body, headers=default_headers, method=method)
    with urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8")


def direct_request_bytes(url, *, method="GET", data=None, headers=None, timeout=120):
    default_headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"
        ),
        "Accept": "*/*",
        "Referer": SOURCE_URL,
        "X-Requested-With": "XMLHttpRequest",
    }
    if headers:
        default_headers.update(headers)
    body = data.encode("utf-8") if isinstance(data, str) else data
    request = Request(url, data=body, headers=default_headers, method=method)
    with urlopen(request, timeout=timeout) as response:
        return response.read()


def direct_json(url, *, method="GET", data=None, headers=None, timeout=120):
    return json.loads(direct_request(url, method=method, data=data, headers=headers, timeout=timeout))


def js_model_value(html, name):
    match = re.search(rf"{re.escape(name)}\s*:\s*'([^']*)'", html)
    if not match:
        return None
    return match.group(1)


def zurich_feed_model(html):
    model = {
        "GrsProjectId": js_model_value(html, "GrsProjectId"),
        "ProjectName": js_model_value(html, "ProjectName"),
        "ToolId": "16",
        "LanguageId": js_model_value(html, "InternalLanguageId") or js_model_value(html, "LanguageId"),
        "LanguageCode": js_model_value(html, "LanguageCode"),
        "forSaleIn": js_model_value(html, "forSaleIn") or "",
        "FSIexclCT": js_model_value(html, "FSIexclCT") or "",
        "DownloadToolFundOptionsUrl": js_model_value(html, "DownloadToolFundOptionsUrl"),
        "PriceHistoryForAllFundsUrl": js_model_value(html, "PriceHistoryForAllFundsUrl"),
        "CSVPriceHistoryUrl": js_model_value(html, "CSVPriceHistoryUrl"),
    }
    missing = [key for key, value in model.items() if value is None]
    if missing:
        raise RuntimeError(f"Zurich/FE page did not expose required model fields: {', '.join(missing)}")
    return model


def full_feed_url(path):
    return path if str(path).startswith("http") else f"{SOURCE_ORIGIN}{path}"


def fund_field(fund_info, *sections_and_keys):
    for section, key in sections_and_keys:
        section_value = fund_info.get(section) or {}
        value = section_value.get(key)
        if value not in (None, ""):
            return value
    return ""


def citi_code_filter(fund_info):
    sector = fund_field(
        fund_info,
        ("FundInfo", "SectorClassCode"),
        ("Documents", "SectorClassCode"),
        ("Common", "SectorClassCode"),
    )
    return {
        "CitiCode": fund_field(
            fund_info,
            ("FundInfo", "CitiCode"),
            ("Documents", "CitiCode"),
            ("Common", "CitiCode"),
        ),
        "Universe": sector.split(":")[0] if isinstance(sector, str) and sector.strip() else None,
        "FirstPriceDate": fund_field(
            fund_info,
            ("FundInfo", "FirstPriceDate"),
            ("Documents", "FirstPriceDate"),
            ("Common", "FirstPriceDate"),
            ("Price", "FirstPriceDate"),
        ),
        "TypeCode": fund_field(
            fund_info,
            ("FundInfo", "TypeCode"),
            ("Documents", "TypeCode"),
            ("Common", "TypeCode"),
        ),
        "Currency": fund_field(fund_info, ("Price", "Currency_UnitLevel")),
    }


def direct_fund_row(fund_info):
    return {
        "name": fund_field(fund_info, ("Common", "Name"), ("Documents", "Name")),
        "fundCentreCode": fund_field(
            fund_info,
            ("Common", "CitiCode"),
            ("Documents", "CitiCode"),
            ("FundInfo", "CitiCode"),
        ),
        "productType": "",
        "price": "",
        "priceDate": "",
        "changePct": "",
        "_bulkFilter": citi_code_filter(fund_info),
        "_priceType": fund_field(fund_info, ("Price", "PriceType")),
    }


def cell_column(cell_ref):
    match = re.match(r"([A-Z]+)", cell_ref or "")
    if not match:
        return ""
    return match.group(1)


def shared_strings_from_xlsx(zipped):
    try:
        xml = zipped.read("xl/sharedStrings.xml")
    except KeyError:
        return []
    root = ET.fromstring(xml)
    namespace = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    values = []
    for item in root.findall("x:si", namespace):
        values.append("".join(text.text or "" for text in item.findall(".//x:t", namespace)))
    return values


def parse_xlsx_price_history(content):
    namespace = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    with zipfile.ZipFile(io.BytesIO(content)) as zipped:
        shared_strings = shared_strings_from_xlsx(zipped)
        sheet = ET.fromstring(zipped.read("xl/worksheets/sheet1.xml"))

    rows = []
    for row in sheet.findall(".//x:sheetData/x:row", namespace):
        values = {}
        for cell in row.findall("x:c", namespace):
            value_node = cell.find("x:v", namespace)
            if value_node is None:
                continue
            value = value_node.text or ""
            if cell.get("t") == "s":
                index = int(value)
                value = shared_strings[index] if index < len(shared_strings) else ""
            values[cell_column(cell.get("r"))] = value
        price = numeric_from_display(values.get("A"))
        parsed_date = parse_display_date(values.get("B"))
        if price is None or not parsed_date:
            continue
        rows.append(
            {
                "price": price,
                "date": parsed_date.isoformat(),
                "currency": values.get("C", ""),
            }
        )
    return rows


def download_price_history_xlsx(model, fund):
    filter_model = fund["_bulkFilter"]
    history_filter = {
        "CitiCode": filter_model["CitiCode"],
        "Universe": filter_model["Universe"],
        "TypeCode": filter_model["TypeCode"],
        "FundName": fund["name"],
        "BaseCurrency": filter_model["Currency"],
        "PriceType": fund.get("_priceType") or "",
        "TimePeriod": str(HISTORY_YEARS * 12),
        "StartDate": None,
        "EndDate": None,
    }
    search_model = {
        "GrsProjectId": model["GrsProjectId"],
        "ProjectName": model["ProjectName"],
        "ToolId": model["ToolId"],
        "LanguageId": model["LanguageId"],
        "LanguageCode": model["LanguageCode"],
        "forSaleIn": model["forSaleIn"],
        "FSIexclCT": model["FSIexclCT"],
    }
    query = urlencode(
        {
            "modelString": json.dumps(search_model, separators=(",", ":")),
            "filtersString": json.dumps(history_filter, separators=(",", ":")),
        }
    )
    url = f"{full_feed_url(model['CSVPriceHistoryUrl'])}?{query}"
    content = direct_request_bytes(
        url,
        headers={"Accept": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,*/*"},
        timeout=120,
    )
    return parse_xlsx_price_history(content)


def trim_to_last_years(rows, latest_date):
    cutoff = latest_date - timedelta(days=365 * HISTORY_YEARS + 10)
    trimmed = [row for row in rows if date.fromisoformat(row["date"]) >= cutoff]
    return trimmed or rows


def fetch_direct_zurich_histories():
    html = direct_request(SOURCE_URL, headers={"Accept": "text/html,application/xhtml+xml"})
    model = zurich_feed_model(html)
    options_query = urlencode(
        {
            "GrsProjectId": model["GrsProjectId"],
            "ProjectName": model["ProjectName"],
            "ToolId": model["ToolId"],
            "LanguageId": model["LanguageId"],
            "LanguageCode": model["LanguageCode"],
            "FSIexclCT": model["FSIexclCT"],
            "forSaleIn": model["forSaleIn"],
            "referrerToolId": "7",
        }
    )
    fund_options = direct_json(f"{full_feed_url(model['DownloadToolFundOptionsUrl'])}?{options_query}")
    fund_infos = fund_options.get("FundInfo") or []
    funds = [direct_fund_row(fund_info) for fund_info in fund_infos]
    funds = [fund for fund in funds if fund["name"] and fund["fundCentreCode"] and fund["_bulkFilter"]["CitiCode"]]

    if len(funds) < MIN_EXPECTED_FUNDS:
        raise RuntimeError(f"Only {len(funds)} funds were found in the Zurich/FE FundOptions feed.")

    print(f"Found {len(funds)} Zurich/FE fund option records.", flush=True)
    history_by_code = {}
    errors = {}
    completed = 0
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(download_price_history_xlsx, model, fund): fund for fund in funds}
        for future in as_completed(futures):
            fund = futures[future]
            completed += 1
            try:
                history_by_code[fund["fundCentreCode"]] = future.result()
            except Exception as exc:
                errors[fund["fundCentreCode"]] = str(exc)
                history_by_code[fund["fundCentreCode"]] = []
            if completed % 20 == 0 or completed == len(funds):
                print(f"Fetched {completed}/{len(funds)} direct Zurich price histories.", flush=True)

    latest_dates = [
        max(date.fromisoformat(row["date"]) for row in rows)
        for rows in history_by_code.values()
        if rows
    ]
    if not latest_dates:
        raise RuntimeError("Zurich/FE direct price feed returned no usable price histories.")

    source_date = max(latest_dates)
    active_cutoff = source_date - timedelta(days=ACTIVE_PRICE_STALENESS_DAYS)
    histories = []
    inactive = 0
    for fund in funds:
        code = fund["fundCentreCode"]
        rows = sorted(history_by_code.get(code, []), key=lambda row: row["date"])
        if len(rows) < 2:
            if code in errors:
                print(f"History unavailable for {code}: {errors[code]}", flush=True)
            inactive += 1
            continue
        latest = rows[-1]
        latest_date = date.fromisoformat(latest["date"])
        if latest_date < active_cutoff:
            inactive += 1
            continue
        rows = trim_to_last_years(rows, latest_date)
        clean_fund = {key: value for key, value in fund.items() if not key.startswith("_")}
        histories.append(
            {
                **clean_fund,
                "price": latest["price"],
                "priceDate": display_date(latest["date"]),
                "history": rows,
                "historyAccepted": True,
                "historyStatus": "direct Zurich/FE downloadable price history",
            }
        )

    print(
        f"Kept {len(histories)} active funds with price dates since {active_cutoff.isoformat()}; "
        f"excluded {inactive} stale or empty histories.",
        flush=True,
    )
    return histories


def closest_on_or_before(rows, target_date):
    best = None
    for row in rows:
        row_date = date.fromisoformat(row["date"])
        if row_date <= target_date:
            best = row
        else:
            break
    return best


def return_since(rows, end, days):
    target_row = closest_on_or_before(rows, date.fromisoformat(end["date"]) - timedelta(days=days))
    if not target_row or not target_row.get("price"):
        return None
    return (end["price"] / target_row["price"] - 1) * 100


def previous_calendar_year_low(rows, end):
    previous_year = date.fromisoformat(end["date"]).year - 1
    candidates = [row for row in rows if date.fromisoformat(row["date"]).year == previous_year]
    if not candidates:
        return None
    return min(candidates, key=lambda row: row["price"])


def moving_average(rows, length, end_index=None):
    if end_index is None:
        end_index = len(rows)
    start_index = max(0, end_index - length)
    window = rows[start_index:end_index]
    if not window:
        return None
    return sum(row["price"] for row in window) / len(window)


def clamp(value, minimum=0, maximum=100):
    return max(minimum, min(maximum, value))


def investment_components(rows, end, previous_low):
    if not previous_low or not previous_low.get("price") or len(rows) < 20:
        return {
            "investmentScore": None,
            "lowOpportunityScore": None,
            "trendScore": None,
            "dipRecoveryScore": None,
            "movingAverageSignal": "n/a",
            "recentDipBelowPreviousLow": False,
            "recentDipDate": None,
            "recentDipPrice": None,
        }

    latest_price = end["price"]
    low_price = previous_low["price"]
    distance_pct = (latest_price / low_price - 1) * 100

    if distance_pct <= 0:
        low_score = 100
    else:
        low_score = clamp(100 - distance_pct * 2)

    ma20 = moving_average(rows, 20)
    ma60 = moving_average(rows, 60)
    prior_ma20 = moving_average(rows, 20, max(0, len(rows) - 20)) if len(rows) >= 40 else None
    prior_end = max(0, len(rows) - 60)
    prior_ma60 = moving_average(rows, 60, prior_end) if prior_end >= 20 else None
    trend_score = 0
    signal_parts = []

    if ma20 and ma60:
        alignment = (ma20 / ma60 - 1) * 100
        trend_score += clamp(10 + alignment * 5, 0, 20)
        signal_parts.append("20D>60D" if ma20 >= ma60 else "20D<60D")
    if ma20 and prior_ma20:
        slope20 = (ma20 / prior_ma20 - 1) * 100
        trend_score += clamp(20 + slope20 * 10, 0, 40)
        signal_parts.append("20D rising" if slope20 >= 0 else "20D falling")
    if ma60 and prior_ma60:
        slope = (ma60 / prior_ma60 - 1) * 100
        trend_score += clamp(20 + slope * 10, 0, 40)
        signal_parts.append("60D rising" if slope >= 0 else "60D falling")
    trend_score = clamp(trend_score)

    recent_rows = rows[-60:]
    below_low = [row for row in recent_rows if row["price"] <= low_price]
    recent_dip = min(below_low, key=lambda row: row["price"]) if below_low else None
    if recent_dip:
        recovery_pct = (latest_price / recent_dip["price"] - 1) * 100 if recent_dip["price"] else 0
        dip_score = clamp(70 + recovery_pct * 3)
    else:
        dip_score = clamp(60 - max(0, distance_pct) * 2)

    total = low_score * 0.45 + trend_score * 0.35 + dip_score * 0.20
    return {
        "investmentScore": round(total, 2),
        "lowOpportunityScore": round(low_score, 2),
        "trendScore": round(trend_score, 2),
        "dipRecoveryScore": round(dip_score, 2),
        "movingAverageSignal": " · ".join(signal_parts) if signal_parts else "n/a",
        "recentDipBelowPreviousLow": bool(recent_dip),
        "recentDipDate": recent_dip["date"] if recent_dip else None,
        "recentDipPrice": round(recent_dip["price"], 4) if recent_dip else None,
    }


def monthly_points(rows):
    by_month = {}
    for row in rows:
        by_month[row["date"][:7]] = row
    points = [by_month[key] for key in sorted(by_month)]
    if rows and (not points or points[0]["date"] != rows[0]["date"]):
        points.insert(0, rows[0])
    if rows and (not points or points[-1]["date"] != rows[-1]["date"]):
        points.append(rows[-1])
    return points


def weekly_points(rows):
    by_week = {}
    for row in rows:
        row_date = date.fromisoformat(row["date"])
        year, week, _ = row_date.isocalendar()
        by_week[f"{year}-W{week:02d}"] = row
    points = [by_week[key] for key in sorted(by_week)]
    if rows and (not points or points[0]["date"] != rows[0]["date"]):
        points.insert(0, rows[0])
    if rows and (not points or points[-1]["date"] != rows[-1]["date"]):
        points.append(rows[-1])
    return points


def normalized_points(rows, label_length=7):
    if not rows:
        return []
    first_price = rows[0]["price"]
    if not first_price:
        return []
    return [
        {
            "d": row["date"][:label_length],
            "v": round((row["price"] / first_price) * 100, 2),
            "p": round(row["price"], 4),
        }
        for row in rows
    ]


def classify_funds(raw_funds):
    funds = []
    unvalidated = []

    for fund in raw_funds:
        rows = sorted(
            [
                row
                for row in fund.get("history", [])
                if row.get("date") and row.get("price") is not None
            ],
            key=lambda row: row["date"],
        )
        accepted = bool(fund.get("historyAccepted")) and len(rows) > 1
        code = fund.get("fundCentreCode") or fund.get("code") or ""

        if not accepted:
            unvalidated.append(
                {
                    "code": code,
                    "name": fund.get("name", ""),
                    "price": fund.get("price", ""),
                    "priceDate": fund.get("priceDate", ""),
                    "reason": fund.get("historyStatus")
                    or fund.get("error")
                    or "history not validated",
                }
            )
            funds.append(
                {
                    "rank": None,
                    "investmentRank": None,
                    "band": "Unvalidated",
                    "code": code,
                    "name": fund.get("name", ""),
                    "currentPrice": fund.get("price", ""),
                    "priceDate": fund.get("priceDate", ""),
                    "currency": "",
                    "returnTotal": None,
                    "return1w": None,
                    "return1m": None,
                    "return3m": None,
                    "return6m": None,
                    "return1y": None,
                    "oneYearAgoPrice": None,
                    "oneYearAgoDate": None,
                    "previousYearLowPrice": None,
                    "previousYearLowDate": None,
                    "distanceFromPreviousYearLowPct": None,
                    "investmentScore": None,
                    "lowOpportunityScore": None,
                    "trendScore": None,
                    "dipRecoveryScore": None,
                    "movingAverageSignal": "n/a",
                    "recentDipBelowPreviousLow": False,
                    "recentDipDate": None,
                    "recentDipPrice": None,
                    "days": None,
                    "validated": False,
                    "points": [],
                    "weeklyPoints": [],
                    "dailyPoints": [],
                }
            )
            continue

        start = rows[0]
        end = rows[-1]
        start_date = date.fromisoformat(start["date"])
        end_date = date.fromisoformat(end["date"])
        return_total = (end["price"] / start["price"] - 1) * 100 if start["price"] else None
        one_year_row = closest_on_or_before(rows, end_date - timedelta(days=365))
        return_1y = (
            (end["price"] / one_year_row["price"] - 1) * 100
            if one_year_row and one_year_row.get("price")
            else None
        )
        return_1w = return_since(rows, end, 7)
        return_1m = return_since(rows, end, 30)
        return_3m = return_since(rows, end, 91)
        return_6m = return_since(rows, end, 182)
        previous_low = previous_calendar_year_low(rows, end)
        distance_from_previous_low = (
            (end["price"] / previous_low["price"] - 1) * 100
            if previous_low and previous_low.get("price")
            else None
        )
        invest = investment_components(rows, end, previous_low)
        monthly = monthly_points(rows)
        weekly = weekly_points(rows)
        points = normalized_points(monthly)
        weekly_chart_points = normalized_points(weekly)
        daily_chart_points = normalized_points(rows, label_length=10)

        funds.append(
            {
                "rank": None,
                "investmentRank": None,
                "band": "",
                "code": code,
                "name": fund.get("name", ""),
                "currentPrice": fund.get("price", ""),
                "priceDate": fund.get("priceDate", ""),
                "currency": end.get("currency", ""),
                "returnTotal": round(return_total, 2) if return_total is not None else None,
                "return1w": round(return_1w, 2) if return_1w is not None else None,
                "return1m": round(return_1m, 2) if return_1m is not None else None,
                "return3m": round(return_3m, 2) if return_3m is not None else None,
                "return6m": round(return_6m, 2) if return_6m is not None else None,
                "return1y": round(return_1y, 2) if return_1y is not None else None,
                "oneYearAgoPrice": round(one_year_row["price"], 4) if one_year_row else None,
                "oneYearAgoDate": one_year_row["date"] if one_year_row else None,
                "previousYearLowPrice": round(previous_low["price"], 4) if previous_low else None,
                "previousYearLowDate": previous_low["date"] if previous_low else None,
                "distanceFromPreviousYearLowPct": round(distance_from_previous_low, 2)
                if distance_from_previous_low is not None
                else None,
                **invest,
                "days": (end_date - start_date).days,
                "startDate": start["date"],
                "endDate": end["date"],
                "startPrice": round(start["price"], 4),
                "latestHistoryPrice": round(end["price"], 4),
                "validated": True,
                "points": points,
                "weeklyPoints": weekly_chart_points,
                "dailyPoints": daily_chart_points,
            }
        )

    ranked = sorted(
        [fund for fund in funds if fund["validated"] and fund["returnTotal"] is not None],
        key=lambda fund: fund["returnTotal"],
        reverse=True,
    )
    quartile_size = math.ceil(len(ranked) * 0.25)
    bottom_start = len(ranked) - quartile_size

    for index, fund in enumerate(ranked, start=1):
        fund["rank"] = index
        if index <= quartile_size:
            fund["band"] = "Best"
        elif index > bottom_start:
            fund["band"] = "Worst"
        else:
            fund["band"] = "Mediocre"

    investment_ranked = sorted(
        [
            fund
            for fund in funds
            if fund["validated"] and fund["investmentScore"] is not None
        ],
        key=lambda fund: (-fund["investmentScore"], fund["name"]),
    )
    for index, fund in enumerate(investment_ranked, start=1):
        fund["investmentRank"] = index

    funds.sort(
        key=lambda fund: (
            0 if fund["validated"] else 1,
            -(fund["returnTotal"] if fund["returnTotal"] is not None else -10**9),
            fund["name"],
        )
    )
    price_dates = sorted({fund.get("priceDate") for fund in funds if fund.get("priceDate")})
    summary = {
        "generatedOn": date.today().isoformat(),
        "refreshedAtUtc": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "sourceAsOf": price_dates[-1] if price_dates else "n/a",
        "liveFundCount": len(funds),
        "validatedChartCount": len(ranked),
        "unvalidatedCount": len(unvalidated),
        "classification": "Best = top quartile, Mediocre = middle half, Worst = bottom quartile, ranked by total return over the available FE fundinfo price-history window.",
        "source": "Zurich UAE fund centre powered by FE fundinfo Download Tool",
        "sourceUrl": SOURCE_URL,
    }
    return {"summary": summary, "funds": funds, "unvalidated": unvalidated}


async def scrape_live_funds(page):
    await page.goto(SOURCE_URL, wait_until="domcontentloaded", timeout=90_000)
    print(f"Loaded {page.url}", flush=True)
    print(f"Page title: {await page.title()}", flush=True)

    try:
        await page.wait_for_function(
            """
            () => [...document.querySelectorAll('select.downloadtool_funds option')]
              .filter(option => option.value && option.value !== 'AllFunds').length >= 150
            """,
            timeout=60_000,
        )
    except PlaywrightTimeoutError:
        print("Timed out waiting for Download Tool fund options; trying rendered fund rows.", flush=True)

    funds = await page.evaluate(
        """
        () => {
          const text = value => String(value || '').replace(/\\s+/g, ' ').trim();
          const optionRows = [...document.querySelectorAll('select.downloadtool_funds option')]
            .map(option => ({
              name: text(option.textContent),
              fundCentreCode: text(option.value),
              productType: '',
              price: '',
              priceDate: '',
              changePct: ''
            }))
            .filter(row => row.name && row.fundCentreCode && row.fundCentreCode !== 'AllFunds');

          if (optionRows.length) {
            const seenOptions = new Set();
            return optionRows.filter(row => {
              if (seenOptions.has(row.fundCentreCode)) return false;
              seenOptions.add(row.fundCentreCode);
              return true;
            });
          }

          const cardRows = [...document.querySelectorAll('.minFsData')].map(card => {
            const name = text(card.querySelector('.fe-column-name')?.innerText);
            const code = text(card.querySelector('[data-code]')?.getAttribute('data-code'));
            const rawText = card.innerText || '';
            const priceMatch = rawText.match(/[€£$]\\s?[-+]?\\d[\\d,]*(?:\\.\\d+)?/);
            const dateMatch = rawText.match(/\\d{1,2}\\s[A-Za-z]{3}\\s\\d{4}/);
            const changeMatch = rawText.match(/Change \\(%\\)\\s*([-+]?\\d+(?:\\.\\d+)?)/);
            return {
              name,
              fundCentreCode: code,
              productType: '',
              price: priceMatch ? priceMatch[0] : '',
              priceDate: dateMatch ? dateMatch[0] : '',
              changePct: changeMatch ? changeMatch[1] : ''
            };
          }).filter(row => row.name && row.fundCentreCode);

          if (cardRows.length) {
            const seenCards = new Set();
            return cardRows.filter(row => {
              if (seenCards.has(row.fundCentreCode)) return false;
              seenCards.add(row.fundCentreCode);
              return true;
            });
          }

          const rows = [...document.querySelectorAll('tr')].map(tr => {
            const cells = [...tr.querySelectorAll('td')].map(td => text(td.innerText));
            if (cells.length < 4) return null;
            const code = cells.find(cell => /^[A-Z0-9]{4,6}$/.test(cell));
            const price = cells.find(cell => /^[€£$]\\s?[-+]?\\d/.test(cell));
            const priceDate = cells.find(cell => /^\\d{1,2}\\s[A-Za-z]{3}\\s\\d{4}$/.test(cell));
            const changePct = cells.find(cell => /^[-+]?\\d+(?:\\.\\d+)?$/.test(cell));
            if (!code || !price || !priceDate) return null;
            const name = cells.find(cell =>
              cell !== code &&
              cell !== price &&
              cell !== priceDate &&
              cell.length > 3 &&
              !/Savings|Investment|Protection/.test(cell)
            );
            const productType = cells.find(cell => /Savings|Investment|Protection/.test(cell)) || '';
            return { name, fundCentreCode: code, productType, price, priceDate, changePct: changePct || '' };
          }).filter(row => row && row.name);

          const seen = new Set();
          return rows.filter(row => {
            if (seen.has(row.fundCentreCode)) return false;
            seen.add(row.fundCentreCode);
            return true;
          });
        }
        """
    )
    if len(funds) < MIN_EXPECTED_FUNDS:
        diagnostics = await page.evaluate(
            """
            () => ({
              bodyPreview: String(document.body?.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 1000),
              tableRows: document.querySelectorAll('tr').length,
              renderedCards: document.querySelectorAll('.minFsData').length,
              scripts: document.querySelectorAll('script').length,
              iframes: document.querySelectorAll('iframe').length,
              downloadOptions: [...document.querySelectorAll('select.downloadtool_funds option')]
                .filter(option => option.value && option.value !== 'AllFunds').length,
              likelyCodes: (String(document.body?.innerText || '').match(/\\b[A-Z0-9]{4,6}\\b/g) || []).slice(0, 25)
            })
            """
        )
        print("Zurich fund-list diagnostics:", json.dumps(diagnostics, ensure_ascii=False), flush=True)
        raise RuntimeError(f"Only {len(funds)} funds were found on the Zurich/FE fund centre page.")

    print(f"Found {len(funds)} live Zurich/FE funds.", flush=True)
    return funds


async def scrape_history(page, fund):
    code = fund["fundCentreCode"]
    target_price = numeric_from_display(fund.get("price"))

    try:
        await page.goto(DOWNLOAD_URL.format(code=code), wait_until="domcontentloaded", timeout=90_000)
        await page.wait_for_selector("#PriceHistoryTimePeriod", timeout=60_000)
        await page.select_option("#PriceHistoryTimePeriod", "36")
        await page.click("#btnPriceHistory")
        parsed = []
        accepted = False

        for _ in range(20):
            await page.wait_for_timeout(500)
            parsed = await page.evaluate(
                """
                () => {
                  const parsePrice = value => {
                    const match = String(value || '').match(/[-+]?\\d[\\d,]*(?:\\.\\d+)?/);
                    return match ? Number(match[0].replace(/,/g, '')) : null;
                  };
                  const parseDate = value => {
                    const match = String(value || '').match(/(\\d{2})\\/(\\d{2})\\/(\\d{4})/);
                    return match ? `${match[3]}-${match[2]}-${match[1]}` : null;
                  };
                  return [...document.querySelectorAll('#priceHtmlContainer tr')].map(tr => {
                    const cells = [...tr.querySelectorAll('td,th')].map(td => td.innerText.trim());
                    if (cells.length < 3 || cells[0] === 'Price') return null;
                    return { price: parsePrice(cells[0]), date: parseDate(cells[1]), currency: cells[2] };
                  }).filter(row => row && row.price != null && row.date);
                }
                """
            )
            if len(parsed) > 1:
                latest = parsed[0]
                price_ok = target_price is None or abs(latest["price"] - target_price) <= max(
                    0.04, target_price * 0.025
                )
                if price_ok:
                    accepted = True
                    break

        if parsed:
            latest = parsed[0]
            fund = {
                **fund,
                "price": fund.get("price") or latest["price"],
                "priceDate": fund.get("priceDate") or display_date(latest["date"]),
            }

        return {
            **fund,
            "history": list(reversed(parsed)),
            "historyAccepted": accepted,
            "targetPrice": target_price,
            **(
                {}
                if accepted
                else {"historyStatus": "latest price did not match source list or no rows returned"}
            ),
        }
    except PlaywrightTimeoutError as exc:
        return {**fund, "history": [], "historyAccepted": False, "error": str(exc)}


async def scrape_all(output_path):
    histories = fetch_direct_zurich_histories()
    if not histories:
        raise RuntimeError("No active Zurich/FE fund histories were found in the direct feed.")
    classified = classify_funds(histories)
    output_path.write_text(json.dumps(classified, indent=2, ensure_ascii=False), encoding="utf-8")
    return classified


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="data/funds.json")
    args = parser.parse_args()
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    classified = await scrape_all(output_path)
    output_path.write_text(json.dumps(classified, indent=2, ensure_ascii=False), encoding="utf-8")
    summary = classified["summary"]
    print(
        f"Wrote {output_path} with {summary['liveFundCount']} funds and {summary['validatedChartCount']} validated histories."
    )


if __name__ == "__main__":
    asyncio.run(main())
