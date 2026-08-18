"""
eBay 不振リスティング抽出モジュール

【フロー】
  1. Trading API GetMyeBaySelling(ActiveList) で出品中の全アイテムを取得（Watchers数込み）
  2. Sell Analytics API getTrafficReport(dimension=LISTING) で impression/view 等を取得
  3. ItemID でマージし、views/watchers/掲載経過日数などの条件でフィルタ
  4. 結果を標準出力＋任意でCSVに出力

【GetSellerListではなくGetMyeBaySellingを使う理由】
  GetSellerListはStartTime(またはEndTime)の範囲指定が必須で、1回のリクエストで
  指定できる幅は最大120日。GTC(自動更新)の出品は120日超前からのStartTimeを持つ
  ことが多く、単純な期間指定では取りこぼす。GetMyeBaySelling(ActiveList)は
  期間指定なしで「現在出品中の全アイテム」をページングだけで取得できるため、
  「全出品を取得」という目的に対してより確実。

【事前準備】
  .env に以下が必要（EBAY_TOKEN, EBAY_APP_ID, EBAY_CLIENT_SECRETは既存のものを流用）:
    EBAY_REFRESH_TOKEN  - sell.analytics.readonly スコープを含むユーザートークンのrefresh_token

  EBAY_REFRESH_TOKEN が未取得、または analytics スコープを含まない場合は
  ebay_oauth_authorize.py で再認可すること。

【使い方】
  python ebay_traffic_filter.py                                   # デフォルト条件で抽出
  python ebay_traffic_filter.py --max-views 0 --max-watchers 0 --min-days-listed 14
  python ebay_traffic_filter.py --days 30 --csv fuseki.csv
  python ebay_traffic_filter.py --sheet                            # 結果をGoogle Sheets(シート5)に書き出す
"""

import argparse
import csv as _csv
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import gspread
import requests
from google.oauth2.service_account import Credentials

from config import CONFIG
from ebay_oauth import SCOPE_ANALYTICS, get_user_token

NS = "{urn:ebay:apis:eBLBaseComponents}"
EBAY_API_URL = "https://api.ebay.com/ws/api.dll"
ANALYTICS_URL = "https://api.ebay.com/sell/analytics/v1/traffic_report"

GSHEET_SCOPES = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
RESULT_WORKSHEET = "不振リスティング"
RESULT_FIELDNAMES = ["item_id", "title", "views", "impressions", "watch_count",
                      "days_listed", "start_time", "price_usd", "quantity"]
RESULT_HEADER = ["ItemID", "タイトル", "views", "impressions", "watchers",
                  "経過日数", "出品日時", "価格(USD)", "在庫数"]

# getTrafficReportで取得するmetric一覧
METRICS = [
    "LISTING_IMPRESSION_TOTAL",
    "LISTING_VIEWS_TOTAL",
    "CLICK_THROUGH_RATE",
    "SALES_CONVERSION_RATE",
    "TRANSACTION",
]

# ──────────────────────────────────────────────────────────
# 1. Trading API: 出品中の全アイテムを取得（Watchers数込み）
# ──────────────────────────────────────────────────────────

def get_all_active_listings(trading_token: str, entries_per_page: int = 200) -> list[dict]:
    """GetMyeBaySelling(ActiveList) で出品中の全アイテムをページングしながら取得する"""
    headers = {
        "X-EBAY-API-SITEID":              "0",    # 0=US
        "X-EBAY-API-COMPATIBILITY-LEVEL": "967",
        "X-EBAY-API-CALL-NAME":           "GetMyeBaySelling",
        "X-EBAY-API-IAF-TOKEN":           trading_token,
        "Content-Type":                   "text/xml",
    }
    listings: list[dict] = []
    page = 1
    total_pages = 1

    while page <= total_pages:
        xml_body = f"""<?xml version="1.0" encoding="utf-8"?>
<GetMyeBaySellingRequest xmlns="urn:ebay:apis:eBLBaseComponents">
  <RequesterCredentials><eBayAuthToken>{trading_token}</eBayAuthToken></RequesterCredentials>
  <ActiveList>
    <Sort>TimeLeft</Sort>
    <Pagination>
      <EntriesPerPage>{entries_per_page}</EntriesPerPage>
      <PageNumber>{page}</PageNumber>
    </Pagination>
    <IncludeWatchCount>true</IncludeWatchCount>
  </ActiveList>
  <DetailLevel>ReturnAll</DetailLevel>
</GetMyeBaySellingRequest>"""

        resp = requests.post(EBAY_API_URL, headers=headers, data=xml_body.encode("utf-8"), timeout=30)
        root = ET.fromstring(resp.text)
        ack = root.findtext(f"{NS}Ack") or ""
        if ack not in ("Success", "Warning"):
            msgs = [e.findtext(f"{NS}LongMessage") for e in root.findall(f"{NS}Errors")]
            print(f"  ⚠️  GetMyeBaySelling失敗(page {page}): {msgs}")
            break

        active = root.find(f"{NS}ActiveList")
        if active is None:
            break

        item_array = active.find(f"{NS}ItemArray")
        page_count = 0
        if item_array is not None:
            for item in item_array.findall(f"{NS}Item"):
                watch_raw = item.findtext(f"{NS}WatchCount")
                listings.append({
                    "item_id":    item.findtext(f"{NS}ItemID") or "",
                    "title":      item.findtext(f"{NS}Title") or "",
                    "start_time": item.findtext(f"{NS}ListingDetails/{NS}StartTime") or "",
                    "watch_count": int(watch_raw) if watch_raw not in (None, "") else None,
                    "price_usd":  item.findtext(f"{NS}SellingStatus/{NS}CurrentPrice") or "",
                    "quantity":   item.findtext(f"{NS}QuantityAvailable") or item.findtext(f"{NS}Quantity") or "",
                })
                page_count += 1

        pagination = active.find(f"{NS}PaginationResult")
        if pagination is not None:
            total_pages = int(pagination.findtext(f"{NS}TotalNumberOfPages") or 1)

        print(f"  📄 GetMyeBaySelling page {page}/{total_pages}: {page_count}件")
        page += 1
        time.sleep(0.3)

    return listings


# ──────────────────────────────────────────────────────────
# 2. Sell Analytics API: impression/view等を取得
# ──────────────────────────────────────────────────────────

def get_traffic_report(user_token: str, listing_ids: list[str], marketplace: str = "EBAY_US",
                        days: int = 30) -> dict[str, dict]:
    """
    getTrafficReport(dimension=LISTING) を listing_ids 200件ずつのチャンクで呼び出し、
    {item_id: {metric_name: value}} の辞書を返す
    """
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=min(days, 90))
    date_range = f"{start.strftime('%Y%m%d')}..{end.strftime('%Y%m%d')}"

    result: dict[str, dict] = {}
    headers = {"Authorization": f"Bearer {user_token}"}
    CHUNK = 200

    for i in range(0, len(listing_ids), CHUNK):
        chunk = listing_ids[i:i + CHUNK]
        filt = f"marketplace_ids:{{{marketplace}}},date_range:[{date_range}],listing_ids:{{{'|'.join(chunk)}}}"
        params = {
            "dimension": "LISTING",
            "filter":    filt,
            "metric":    ",".join(METRICS),
        }
        resp = requests.get(ANALYTICS_URL, headers=headers, params=params, timeout=30)
        if resp.status_code != 200:
            print(f"  ⚠️  getTrafficReport失敗: HTTP {resp.status_code} {resp.text[:300]}")
            continue

        data = resp.json()
        metric_keys = [m.get("key") for m in data.get("header", {}).get("metrics", [])]
        records = data.get("records", [])
        for rec in records:
            dim_values = rec.get("dimensionValues", [])
            if not dim_values:
                continue
            item_id = dim_values[0].get("value", "")
            row = {}
            for key, val in zip(metric_keys, rec.get("metricValues", [])):
                try:
                    row[key] = float(val.get("value", 0))
                except (TypeError, ValueError):
                    row[key] = 0.0
            result[item_id] = row

        print(f"  📊 traffic_report: {len(chunk)}件中 {len(records)}件取得")
        time.sleep(0.3)

    return result


# ──────────────────────────────────────────────────────────
# 3. マージ＆フィルタ
# ──────────────────────────────────────────────────────────

def merge_and_filter(listings: list[dict], traffic: dict[str, dict],
                      max_views: float = 0, max_watchers: int = 0,
                      min_days_listed: int = 0) -> list[dict]:
    now = datetime.now(timezone.utc)
    rows = []

    for item in listings:
        t = traffic.get(item["item_id"], {})
        views = t.get("LISTING_VIEWS_TOTAL", 0.0)
        impressions = t.get("LISTING_IMPRESSION_TOTAL", 0.0)
        watchers = item.get("watch_count") or 0

        days_listed = None
        if item["start_time"]:
            try:
                st = datetime.fromisoformat(item["start_time"].replace("Z", "+00:00"))
                days_listed = (now - st).days
            except ValueError:
                pass

        if views > max_views:
            continue
        if watchers > max_watchers:
            continue
        if min_days_listed and (days_listed is None or days_listed < min_days_listed):
            continue

        rows.append({
            **item,
            "views": views,
            "impressions": impressions,
            "days_listed": days_listed,
        })

    rows.sort(key=lambda r: r["days_listed"] or 0, reverse=True)
    return rows


def write_to_sheet(rows: list[dict], sheet_id: str, cred_path: str) -> None:
    """結果をGoogle Sheetsの指定シートに書き出す（既存内容は洗い替え）"""
    creds = Credentials.from_service_account_file(cred_path, scopes=GSHEET_SCOPES)
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(sheet_id)
    try:
        ws = sh.worksheet(RESULT_WORKSHEET)
    except gspread.exceptions.WorksheetNotFound:
        ws = sh.add_worksheet(RESULT_WORKSHEET, rows=max(len(rows) + 10, 100), cols=len(RESULT_HEADER))

    values = [RESULT_HEADER] + [[r.get(k, "") for k in RESULT_FIELDNAMES] for r in rows]
    ws.clear()
    ws.update("A1", values)
    print(f"  📊 Google Sheets「{RESULT_WORKSHEET}」に書き出し: {len(rows)}行")


def main():
    parser = argparse.ArgumentParser(description="views=0・watchers=0など不振リスティングを抽出")
    parser.add_argument("--days", type=int, default=30, help="トラフィックレポートの集計期間（日数、最大90）")
    parser.add_argument("--max-views", type=float, default=0, help="この値以下のviewsを抽出（デフォルト0）")
    parser.add_argument("--max-watchers", type=int, default=0, help="この値以下のwatchersを抽出（デフォルト0）")
    parser.add_argument("--min-days-listed", type=int, default=14, help="出品からこの日数以上経過したものだけ抽出")
    parser.add_argument("--marketplace", default="EBAY_US")
    parser.add_argument("--csv", default="", help="結果をCSVに出力するパス")
    parser.add_argument("--sheet", action="store_true", help="結果をGoogle Sheets（シート5）に書き出す")
    args = parser.parse_args()

    print("🔍 出品中の全アイテムを取得中...")
    listings = get_all_active_listings(CONFIG["EBAY_TOKEN"])
    print(f"  ✅ {len(listings)}件取得")
    if not listings:
        return

    print("🔑 sell.analytics.readonly ユーザートークンを取得中...")
    user_token = get_user_token(CONFIG, SCOPE_ANALYTICS)
    if not user_token:
        print("  ❌ ユーザートークン取得に失敗したため終了します")
        sys.exit(1)

    print(f"📊 トラフィックレポートを取得中（直近{args.days}日）...")
    item_ids = [l["item_id"] for l in listings]
    traffic = get_traffic_report(user_token, item_ids, marketplace=args.marketplace, days=args.days)

    print("🧮 マージ＆フィルタ中...")
    result = merge_and_filter(
        listings, traffic,
        max_views=args.max_views,
        max_watchers=args.max_watchers,
        min_days_listed=args.min_days_listed,
    )

    print(f"\n{'=' * 80}\n不振リスティング: {len(result)}件 / 全{len(listings)}件"
          f"（views<={args.max_views}, watchers<={args.max_watchers}, 経過>={args.min_days_listed}日）\n{'=' * 80}")
    for r in result:
        print(f"  {r['item_id']}  views={r['views']:.0f}  watchers={r['watch_count'] or 0}"
              f"  経過{r['days_listed']}日  {r['title'][:50]}")

    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8-sig") as f:
            writer = _csv.DictWriter(f, fieldnames=RESULT_FIELDNAMES)
            writer.writeheader()
            for r in result:
                writer.writerow({k: r.get(k, "") for k in RESULT_FIELDNAMES})
        print(f"\n💾 CSV出力: {args.csv}")

    if args.sheet:
        write_to_sheet(result, CONFIG["SHEET_ID"], CONFIG["GSHEET_CRED_PATH"])


if __name__ == "__main__":
    main()
