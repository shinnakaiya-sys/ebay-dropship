"""
lowest_scrape.py
================
商品マスタのM列URL（最安値順eBay検索）をSeleniumでスクレイピングし、
競合セラーの価格+送料を検索ページから直接取得して
競合最安値（J列）と自分の順位（N列）を更新する。

APIクォータ不要 — ページから price と shipping テキストを直接パース。

使い方:
  python3 lowest_scrape.py
  python3 lowest_scrape.py --limit 10
"""

import sys
sys.stdout.reconfigure(line_buffering=True)

import re
import time
import os
from config import CONFIG
from sheets_manager import SheetsManager

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


# ==========================================
# 送料テキスト → USD 変換
# ==========================================
def _parse_shipping_usd(text: str, jpy_rate: float) -> float:
    """
    "Free shipping"       → 0.0
    "+$4.99 shipping"     → 4.99
    "+JPY 4,659 shipping" → 4659 / jpy_rate
    """
    t = text.strip()
    if not t or "free" in t.lower():
        return 0.0
    m = re.search(r'\+\$([0-9,]+\.?\d*)', t)
    if m:
        return float(m.group(1).replace(",", ""))
    m = re.search(r'\+JPY\s*([0-9,]+)', t, re.I)
    if m:
        return round(float(m.group(1).replace(",", "")) / jpy_rate, 2)
    return 0.0


# ==========================================
# Chromeドライバー起動
# 環境変数 HEADLESS=1 でヘッドレスモード（VPS用）
# ==========================================
def _create_driver():
    import shutil
    from selenium import webdriver
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.chrome.options import Options

    headless = os.environ.get("HEADLESS", "0") == "1"

    options = Options()

    if headless:
        # VPS用: ヘッドレスモード
        options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument("--disable-software-rasterizer")
    else:
        # Mac用: ebay_sessionプロファイルでログイン状態を維持
        profile_path = os.path.join(BASE_DIR, "ebay_session")
        options.add_argument(f"--user-data-dir={profile_path}")

    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--lang=en-US")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                         "AppleWebKit/537.36 (KHTML, like Gecko) "
                         "Chrome/124.0.0.0 Safari/537.36")
    options.add_experimental_option("excludeSwitches", ["enable-automation", "enable-logging"])
    options.add_experimental_option("useAutomationExtension", False)
    options.add_experimental_option("prefs", {"intl.accept_languages": "en-US,en"})

    system_cd = shutil.which("chromedriver")
    if system_cd:
        service = Service(system_cd)
    else:
        from webdriver_manager.chrome import ChromeDriverManager
        os.environ.setdefault("WDM_LOCAL", "1")
        service = Service(ChromeDriverManager().install())

    driver = webdriver.Chrome(service=service, options=options)
    try:
        from selenium_stealth import stealth
        stealth(driver, languages=["en-US", "en"], vendor="Google Inc.",
                platform="Win32", webgl_vendor="Intel Inc.",
                renderer="Intel Iris OpenGL Engine", fix_hairline=True)
    except ImportError:
        pass
    return driver


# ==========================================
# eBay検索ページから競合価格+送料を取得
# ==========================================
def scrape_ebay_search(driver, url: str, my_seller_id: str, jpy_rate: float) -> dict:
    """
    M列URLをSeleniumで開き、競合セラーの価格+送料合計を検索ページから直接取得。
    Browse API 不要 — APIクォータを消費しない。

    Returns: {"lowest_price": float, "my_rank": int|None, "count": int}
    """
    from selenium.webdriver.common.by import By
    from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

    # 即購入のみ・新品のみ・価格昇順・日本発送のみ を強制付与
    parsed = urlparse(url)
    params = parse_qs(parsed.query, keep_blank_values=True)
    params["LH_BIN"]           = ["1"]
    params["LH_ItemCondition"] = ["1000"]
    params["_sop"]             = ["15"]
    params["LH_PrefLoc"]       = ["2"]
    new_query = urlencode({k: v[0] for k, v in params.items()})
    url = urlunparse(parsed._replace(query=new_query))

    driver.get(url)
    time.sleep(4)

    items = driver.find_elements(By.CSS_SELECTOR, "ul.srp-results li")
    if not items:
        return {"lowest_price": 0.0, "my_rank": None, "count": 0}

    my_seller_lower   = (my_seller_id or "").lower()
    competitor_totals = []
    my_rank           = None
    count             = 0

    for item in items:
        # アイテムIDをリンクURLから取得
        links = item.find_elements(By.CSS_SELECTOR, "a[href*='/itm/']")
        if not links:
            continue
        href = links[0].get_attribute("href") or ""
        m = re.search(r"/itm/(?:[^/?#]+/)?(\d{9,})", href)
        if not m:
            continue

        item_id = m.group(1)
        count  += 1

        # セラー名取得（"N watchers" / "N sold" を除外）
        seller = ""
        for sel_el in item.find_elements(By.CSS_SELECTOR, "span.su-styled-text.primary.large"):
            txt = sel_el.text.strip()
            if txt and not re.match(r"^\d[\d,]* (watchers?|sold)$", txt, re.I):
                seller = txt.lower()
                break

        is_mine = my_seller_lower and my_seller_lower in seller
        if is_mine:
            if my_rank is None:
                my_rank = count
            continue

        # 価格取得
        price = 0.0
        for pel in item.find_elements(By.CSS_SELECTOR, "[class*='s-card__price']"):
            m2 = re.search(r'\$([0-9,]+\.?\d*)', pel.text)
            if m2:
                price = float(m2.group(1).replace(",", ""))
                break
        if price <= 0:
            continue

        # 送料取得（"Free shipping" / "+$X.XX shipping" / "+JPY X,XXX shipping"）
        shipping = 0.0
        for sel_el in item.find_elements(By.CSS_SELECTOR, "span[class*='secondary']"):
            txt = sel_el.text.strip()
            if "ship" in txt.lower() or "free" in txt.lower():
                shipping = _parse_shipping_usd(txt, jpy_rate)
                break

        total = round(price + shipping, 2)
        print(f"    item {item_id}: ${price} + ship${shipping} = ${total}")
        competitor_totals.append(total)

    lowest_price = round(min(competitor_totals), 2) if competitor_totals else 0.0

    return {
        "lowest_price": lowest_price,
        "my_rank":      my_rank,
        "count":        count,
    }


# ==========================================
# メイン
# ==========================================
def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0, help="処理件数の上限（0=全件）")
    args = parser.parse_args()

    print("=" * 55)
    print("  競合最安値・順位チェック（Seleniumスクレイピング）")
    print("=" * 55)

    sheets    = SheetsManager(CONFIG["SHEET_ID"])
    products  = sheets.get_active_products()
    seller_id = CONFIG.get("EBAY_SELLER_ID", "")
    jpy_rate  = CONFIG.get("JPY_TO_USD", 150.0)

    print(f"  対象: {len(products)} 件  セラーID: {seller_id}  JPY/USD: {jpy_rate}")

    targets = [p for p in products if str(p.get("最安値順URL") or "").startswith("http")]
    print(f"  M列URL有り: {len(targets)} 件")

    print(f"  処理対象: {len(targets)} 件")
    print("=" * 55)

    if not targets:
        print("処理対象なし。")
        return

    if args.limit > 0:
        targets = targets[:args.limit]
        print(f"  ※ --limit {args.limit} 件のみ処理")

    print("[初期化] Chromeドライバー起動中...")
    driver = _create_driver()
    print("  ✅ 起動完了")

    done = skip = error = 0

    try:
        for i, product in enumerate(targets):
            asin = product.get("ASIN") or product.get("JANコード", "")
            name = product.get("商品名", "")
            url  = str(product.get("最安値順URL", "")).strip()

            print(f"\n[{i+1}/{len(targets)}] {name[:40]}")

            try:
                result = scrape_ebay_search(driver, url, seller_id, jpy_rate)

                if result["lowest_price"] > 0:
                    sheets.update_rival_price(asin, result["lowest_price"], result["count"])
                else:
                    # 競合なし → J列に "競合なし"、K列に出品数を書く
                    from sheets_manager import SHEET_MASTER
                    ws = sheets.sheet.worksheet(SHEET_MASTER)
                    cell = sheets._find_asin_cell(ws, asin)
                    if cell:
                        ws.update_cell(cell.row, 10, "競合なし")
                        ws.update_cell(cell.row, 11, result["count"])
                if result["my_rank"] is not None:
                    sheets.update_my_rank(asin, result["my_rank"])

                print(f"  競合最安値(価格+送料): ${result['lowest_price']}  "
                      f"出品数: {result['count']}件  "
                      f"自分の順位: {result['my_rank']}")
                done += 1

            except Exception as e:
                print(f"  ❌ エラー: {e}")
                error += 1

            time.sleep(1)

    finally:
        driver.quit()
        print("\n  ドライバーを終了しました。")

    print(f"\n{'=' * 55}")
    print(f"  完了: {done}件  スキップ: {skip}件  エラー: {error}件")
    print(f"{'=' * 55}")


if __name__ == "__main__":
    main()
