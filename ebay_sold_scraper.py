"""
ebay_sold_scraper.py - eBay販売履歴URLまたはセラーIDから商品情報を取得し利益計算

使い方:
  python3 ebay_sold_scraper.py <セラーID>
  python3 ebay_sold_scraper.py "<URL>"
  python3 ebay_sold_scraper.py <セラーID> --max 100 --account kaworu
  python3 ebay_sold_scraper.py "<URL>" --csv --dry-run

オプション:
  --max N        取得上限件数（デフォルト: 50）
  --account NAME アカウント名（kozuki/kaworu/dbz、デフォルト: kaworu）
  --force        jan_research.py の Terapeak をスキップ
  --dry-run      スプレッドシートへの書き込みをスキップ
  --all-jan      日本製以外のJANも対象にする（デフォルト: 45/49始まりのみ）
  --no-research  利益計算をスキップ（商品情報収集のみ）
  --csv          sold_scraper_*.csv にも保存
  --no-sheets    販売履歴をスプレッドシートに保存しない

例:
  python3 ebay_sold_scraper.py akibashipping --account kaworu
  python3 ebay_sold_scraper.py "https://www.ebay.com/sch/i.html?_nkw=aiwa&LH_Sold=1"

フロー:
  1. Selenium でItem IDリストを収集
  2. Browse API で商品情報（JAN含む）を取得
  3. JANコードを jan_research.py に渡して利益計算
  4. 結果をスプレッドシートに保存
"""

import os
import re
import sys
import csv
import time
import base64
import subprocess
import requests
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime
from dotenv import load_dotenv

BASE_DIR         = os.path.dirname(os.path.abspath(__file__))
SPREADSHEET_ID   = "1GEGnGQtb5Fb76W9Nyd5gGM-igQAe1-U9-W2nmhVjaB8"
JAN_BATCH_SIZE   = 30

_env_file = ".env.kaworu" if os.path.exists(os.path.join(BASE_DIR, ".env.kaworu")) else ".env"
load_dotenv(os.path.join(BASE_DIR, _env_file))

CLIENT_ID     = os.getenv("EBAY_CLIENT_ID") or os.getenv("EBAY_APP_ID", "")
CLIENT_SECRET = os.getenv("EBAY_CLIENT_SECRET", "")

_token_cache = {"value": None, "expires_at": 0}


# ------------------------------------------------------------------
# Browse API
# ------------------------------------------------------------------

def get_token():
    now = time.time()
    if _token_cache["value"] and now < _token_cache["expires_at"] - 300:
        return _token_cache["value"]
    try:
        r = requests.post(
            "https://api.ebay.com/identity/v1/oauth2/token",
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Authorization": "Basic " + base64.b64encode(
                    f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode(),
            },
            data={"grant_type": "client_credentials",
                  "scope": "https://api.ebay.com/oauth/api_scope"},
            timeout=10,
        )
        if r.ok:
            data = r.json()
            _token_cache["value"] = data["access_token"]
            _token_cache["expires_at"] = now + data.get("expires_in", 7200)
            return _token_cache["value"]
        print(f"  ⚠ トークン取得失敗 {r.status_code}: {r.text[:100]}")
    except Exception as e:
        print(f"  ⚠ トークン取得エラー: {e}")
    return None


def get_item_info(item_id, retry=3):
    """Browse API でアイテム詳細を取得（レートリミット時は指数バックオフでリトライ）"""
    token = get_token()
    if not token:
        return None
    for attempt in range(retry):
        try:
            r = requests.get(
                "https://api.ebay.com/buy/browse/v1/item/get_item_by_legacy_id",
                headers={"Authorization": f"Bearer {token}"},
                params={"legacy_item_id": item_id},
                timeout=10,
            )
            if r.status_code == 429:
                wait = 30 * (2 ** attempt)
                print(f"  ⚠ レートリミット。{wait}秒待機... ({attempt+1}/{retry})")
                time.sleep(wait)
                continue
            if not r.ok:
                print(f"    API エラー {r.status_code}: {r.text[:150]}")
                return None
            return r.json()
        except Exception as e:
            print(f"    リクエストエラー: {e}")
    return None


def parse_item(data, item_id):
    """APIレスポンスから必要な情報を抽出"""
    price = ""
    if "price" in data:
        price = data["price"].get("value", "")
    elif "currentBidPrice" in data:
        price = data["currentBidPrice"].get("value", "")

    gtin = (data.get("gtin") or "").strip()
    if not gtin:
        for spec in data.get("localizedAspects", []):
            name = spec.get("name", "").upper()
            if name in ("JAN", "EAN", "UPC", "GTIN"):
                gtin = spec.get("value", "").strip()
                break

    sold_date = ""
    if "itemEndDate" in data:
        try:
            dt = datetime.fromisoformat(data["itemEndDate"].replace("Z", "+00:00"))
            sold_date = dt.strftime("%Y/%m/%d")
        except Exception:
            sold_date = data["itemEndDate"][:10]

    specifics = {
        spec["name"]: spec["value"]
        for spec in data.get("localizedAspects", [])
    }

    return {
        "item_id":   item_id,
        "title":     data.get("title", ""),
        "price_usd": price,
        "condition": data.get("condition", ""),
        "gtin":      gtin,
        "mpn":       (data.get("mpn") or "").strip(),
        "category":  data.get("categoryPath", ""),
        "seller":    data.get("seller", {}).get("username", ""),
        "sold_date": sold_date,
        "url":       f"https://www.ebay.com/itm/{item_id}",
        **{f"spec_{k}": v for k, v in specifics.items()},
    }


# ------------------------------------------------------------------
# Selenium スクレイピング（Item ID収集）
# ------------------------------------------------------------------

def create_driver():
    from selenium import webdriver
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.chrome.options import Options
    from webdriver_manager.chrome import ChromeDriverManager

    options = Options()
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--lang=en-US")
    profile_path = os.path.join(BASE_DIR, "ebay_session")
    options.add_argument(f"--user-data-dir={profile_path}")
    options.add_experimental_option("prefs", {"intl.accept_languages": "en-US,en"})
    return webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)


def build_seller_url(seller_id):
    return (
        f"https://www.ebay.com/sch/i.html"
        f"?_nkw=&_armrs=1&_ssn={seller_id}&LH_Complete=1&LH_Sold=1&rt=nc"
    )


def scrape_item_ids(base_url, max_items):
    """販売履歴URLからItem IDリストを収集"""
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC

    if "LH_Sold" not in base_url:
        sep = "&" if "?" in base_url else "?"
        base_url += f"{sep}LH_Sold=1&LH_Complete=1"

    item_ids = []
    seen = set()
    page = 1
    driver = create_driver()

    print("Item IDを収集中...")
    try:
        while len(item_ids) < max_items:
            url = re.sub(r"[&?]_pgn=\d+", "", base_url)
            sep = "&" if "?" in url else "?"
            url += f"{sep}_pgn={page}&_ipg=240"

            driver.get(url)
            try:
                WebDriverWait(driver, 15).until(
                    EC.presence_of_element_located(
                        (By.CSS_SELECTOR, "ul.srp-results, li.s-item, .srp-river-results"))
                )
            except Exception:
                pass
            time.sleep(3)

            page_title = driver.title
            all_hrefs = set()
            for a in driver.find_elements(By.XPATH, "//a[contains(@href,'/itm/')]"):
                all_hrefs.add(a.get_attribute("href") or "")
            for a in driver.find_elements(By.CSS_SELECTOR, "li.s-item a.s-item__link"):
                all_hrefs.add(a.get_attribute("href") or "")

            found = 0
            for href in all_hrefs:
                m = re.search(r"/itm/(\d+)", href)
                if m:
                    iid = m.group(1)
                    if iid in ("123456", "000000") or len(iid) < 10:
                        continue
                    if iid not in seen:
                        item_ids.append(iid)
                        seen.add(iid)
                        found += 1

            print(f"  ページ {page}: {found}件 / 累計 {len(item_ids)}件 [タイトル: {page_title[:60]}]")

            if found == 0:
                ss_path = os.path.join(BASE_DIR, f"debug_sold_scraper_page{page}.png")
                driver.save_screenshot(ss_path)
                print(f"  ⚠ 0件 → スクリーンショット保存: {ss_path}")
                break

            next_btn = None
            for sel in ["a.pagination__next", "a[aria-label='Go to next search page']", ".pagination__next"]:
                try:
                    next_btn = driver.find_element(By.CSS_SELECTOR, sel)
                    if next_btn:
                        break
                except Exception:
                    pass
            if not next_btn:
                break
            page += 1
            time.sleep(1.5)
    finally:
        driver.quit()

    return item_ids[:max_items]


# ------------------------------------------------------------------
# JAN フィルタ・利益計算連携
# ------------------------------------------------------------------

def is_japan_jan(code):
    """日本のJANコード判定（45/49始まりの13桁）"""
    return len(code) == 13 and code.isdigit() and (
        code.startswith("45") or code.startswith("49"))


def flush_jans(jan_list, account, dry_run, force):
    """jan_research.py にJANコードを渡して利益計算を実行"""
    if not jan_list:
        return
    print(f"\n  → {len(jan_list)}件のJANコードで利益計算中...")
    cmd = [sys.executable, os.path.join(BASE_DIR, "jan_research.py"),
           "--account", account] + sorted(jan_list)
    if dry_run:
        cmd.append("--dry-run")
    if force:
        cmd.append("--force")
    subprocess.run(cmd, cwd=BASE_DIR)
    print(f"  → 利益計算完了\n")


# ------------------------------------------------------------------
# 出力
# ------------------------------------------------------------------

def print_item(i, total, item):
    print(f"\n[{i}/{total}] {item['title'][:70]}")
    print(f"  落札価格 : ${item['price_usd']}")
    print(f"  状態     : {item['condition']}")
    print(f"  JAN/GTIN : {item['gtin'] or '—'}")
    print(f"  MPN      : {item['mpn'] or '—'}")
    print(f"  カテゴリ : {item['category']}")
    print(f"  セラー   : {item['seller']}")
    print(f"  落札日   : {item['sold_date']}")
    print(f"  URL      : {item['url']}")


def get_fieldnames(items):
    base_fields = ["item_id", "title", "price_usd", "condition", "gtin", "mpn",
                   "category", "seller", "sold_date", "url"]
    spec_keys = []
    for item in items:
        for k in item:
            if k.startswith("spec_") and k not in spec_keys:
                spec_keys.append(k)
    return base_fields + spec_keys


def save_to_sheets(items):
    scope = ["https://spreadsheets.google.com/feeds",
             "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_name(
        os.path.join(BASE_DIR, "credentials.json"), scope)
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(SPREADSHEET_ID)

    tab_name = datetime.now().strftime("%Y/%m/%d %H:%M")
    fieldnames = get_fieldnames(items)

    ws = sh.add_worksheet(title=tab_name, rows=len(items) + 1, cols=len(fieldnames))
    rows = [fieldnames] + [[item.get(k, "") for k in fieldnames] for item in items]
    ws.update(rows)
    print(f"\nスプレッドシート保存: タブ「{tab_name}」")


def save_csv(items):
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(BASE_DIR, f"sold_scraper_{ts}.csv")
    fieldnames = get_fieldnames(items)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for item in items:
            w.writerow({k: item.get(k, "") for k in fieldnames})
    print(f"\nCSV保存: {path}")


# ------------------------------------------------------------------
# メイン
# ------------------------------------------------------------------

def main():
    args = sys.argv[1:]
    if not args or args[0].startswith("--"):
        print("使い方: python3 ebay_sold_scraper.py <セラーID or URL> [オプション]")
        print("オプション: --max N / --account NAME / --force / --dry-run / --all-jan / --no-research / --csv / --no-sheets")
        sys.exit(1)

    target      = args[0]
    max_items   = 500
    to_csv      = "--csv" in args
    to_sheets   = "--no-sheets" not in args
    dry_run     = "--dry-run" in args
    force       = "--force" in args
    japan_only  = "--all-jan" not in args
    no_research = "--no-research" in args
    account     = "kaworu"

    if "--max" in args:
        idx = args.index("--max")
        max_items = int(args[idx + 1])

    if "--account" in args:
        idx = args.index("--account")
        account = args[idx + 1]

    # URLかセラーIDかを判定
    if target.startswith("http://") or target.startswith("https://"):
        base_url = target
        label    = f"URL: {target}"
    else:
        base_url = build_seller_url(target)
        label    = f"セラーID: {target}"

    print(f"=== eBay販売履歴スクレイパー ===")
    print(f"{label}")
    print(f"上限: {max_items}件 / アカウント: {account}")
    if no_research:
        print("利益計算: スキップ（--no-research）")
    print()

    # Step 1: Item ID収集
    item_ids = scrape_item_ids(base_url, max_items)
    print(f"\n→ {len(item_ids)}件のItem IDを取得\n")

    if not item_ids:
        print("❌ Item IDが取得できませんでした。")
        sys.exit(1)

    # Step 2: Browse APIで商品情報取得 + JAN収集
    print("Browse APIで商品情報を取得中...")
    total       = len(item_ids)
    items       = []
    jan_set     = set()
    written_jans = set()

    for i, item_id in enumerate(item_ids, 1):
        data = get_item_info(item_id)
        if data:
            item = parse_item(data, item_id)
            items.append(item)
            print_item(i, total, item)

            gtin = item.get("gtin", "")
            if gtin and (not japan_only or is_japan_jan(gtin)):
                jan_set.add(gtin)
                print(f"  → JAN収集: {gtin} ✅")
            elif gtin:
                print(f"  → JAN収集: {gtin} (非日本製、スキップ)")
        else:
            print(f"[{i}/{total}] {item_id} → API取得失敗")

        # JAN_BATCH_SIZE件ごとに利益計算を実行
        if not no_research and (i % JAN_BATCH_SIZE == 0):
            new_jans = jan_set - written_jans
            flush_jans(new_jans, account, dry_run, force)
            written_jans |= new_jans

        time.sleep(1)

    print(f"\n{'='*60}")
    print(f"完了: {len(items)}/{total}件取得 / JAN収集: {len(jan_set)}件")

    # 残りのJANで利益計算
    if not no_research:
        remaining = jan_set - written_jans
        if remaining:
            flush_jans(remaining, account, dry_run, force)
        elif not jan_set:
            print("⚠ JANコードが取得できませんでした。利益計算をスキップします。")

    if to_sheets and items:
        save_to_sheets(items)
    if to_csv and items:
        save_csv(items)


if __name__ == "__main__":
    main()
