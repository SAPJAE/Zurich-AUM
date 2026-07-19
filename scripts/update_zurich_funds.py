import argparse
import asyncio
import json
import math
import re
from datetime import date, datetime, timezone, timedelta
from pathlib import Path

try:
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError
    from playwright.async_api import async_playwright
except ModuleNotFoundError:
    PlaywrightTimeoutError = TimeoutError
    async_playwright = None


SOURCE_URL = "https://digital.feprecisionplus.com/zilme/en-gb/ZILME"
DOWNLOAD_URL = "https://digital.feprecisionplus.com/zilme/en-gb/ZILME/DownloadTool?citicode={code}&historyType=price"


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
            if fund["validated"] and fund["distanceFromPreviousYearLowPct"] is not None
        ],
        key=lambda fund: (fund["distanceFromPreviousYearLowPct"], fund["name"]),
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
    await page.wait_for_timeout(5_000)

    return await page.evaluate(
        """
        () => {
          const text = value => String(value || '').replace(/\\s+/g, ' ').trim();
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
    if async_playwright is None:
        raise RuntimeError("Playwright is required to refresh live Zurich fund data.")
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(viewport={"width": 1440, "height": 1100})
        funds = await scrape_live_funds(page)
        if not funds:
            raise RuntimeError("No funds were found on the Zurich/FE fund centre page.")

        histories = []
        for index, fund in enumerate(funds, start=1):
            print(f"[{index}/{len(funds)}] {fund['fundCentreCode']} {fund['name']}", flush=True)
            histories.append(await scrape_history(page, fund))
            output_path.write_text(json.dumps(classify_funds(histories), indent=2, ensure_ascii=False), encoding="utf-8")

        await browser.close()
        return classify_funds(histories)


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
