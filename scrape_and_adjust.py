"""
scrape_and_adjust.py
====================
競合最安値スクレイピング → 価格調整 を一括実行する。

  Step 1: M列URLをSeleniumで開き、競合価格+送料をJ列・K列・N列に書き込む
  Step 2: J列の競合最安値をもとに自分の価格を $0.01 アンダーカットして更新する

使い方:
  python3 scrape_and_adjust.py              # 全件（スクレイプ→調整）
  python3 scrape_and_adjust.py --limit 10   # 最大10件
  python3 scrape_and_adjust.py --dry-run    # 試算のみ（eBay・シート更新なし）
  python3 scrape_and_adjust.py --scrape-only  # スクレイプのみ
  python3 scrape_and_adjust.py --adjust-only  # 価格調整のみ
"""

import sys
sys.stdout.reconfigure(line_buffering=True)

import re
import time
import os
import argparse
from config import CONFIG
from sheets_manager import SheetsManager, SHEET_MASTER
from ebay_checker import EbayChecker
from run import calc_sell_price

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


# ==========================================
# 送料テキスト → USD 変換
# ==========================================
def _parse_shipping_usd(text: str, jpy_rate: float) -> float:
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
        options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument("--disable-software-rasterizer")
    else:
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
    from selenium.webdriver.common.by import By
    from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

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
        links = item.find_elements(By.CSS_SELECTOR, "a[href*='/itm/']")
        if not links:
            continue
        href = links[0].get_attribute("href") or ""
        m = re.search(r"/itm/(?:[^/?#]+/)?(\d{9,})", href)
        if not m:
            continue

        item_id = m.group(1)
        count  += 1

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

        price = 0.0
        for pel in item.find_elements(By.CSS_SELECTOR, "[class*='s-card__price']"):
            m2 = re.search(r'\$([0-9,]+\.?\d*)', pel.text)
            if m2:
                price = float(m2.group(1).replace(",", ""))
                break
        if price <= 0:
            continue

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
    return {"lowest_price": lowest_price, "my_rank": my_rank, "count": count}


# ==========================================
# Step 1: 競合最安値スクレイプ
# ==========================================
def run_scrape(sheets: SheetsManager, products: list, seller_id: str,
               jpy_rate: float, limit: int) -> None:
    targets = [p for p in products if str(p.get("最安値順URL") or "").startswith("http")]
    print(f"  M列URL有り: {len(targets)} 件")

    if limit > 0:
        targets = targets[:limit]
        print(f"  ※ --limit {limit} 件のみ処理")

    print("[初期化] Chromeドライバー起動中...")
    driver = _create_driver()
    print("  ✅ 起動完了\n")

    done = error = 0
    try:
        for i, product in enumerate(targets):
            asin = product.get("ASIN") or product.get("JANコード", "")
            name = product.get("商品名", "")
            url  = str(product.get("最安値順URL", "")).strip()

            print(f"[スクレイプ {i+1}/{len(targets)}] {name[:40]}")

            try:
                result = scrape_ebay_search(driver, url, seller_id, jpy_rate)

                if result["lowest_price"] > 0:
                    sheets.update_rival_price(asin, result["lowest_price"], result["count"])
                else:
                    ws   = sheets.sheet.worksheet(SHEET_MASTER)
                    cell = sheets._find_asin_cell(ws, asin)
                    if cell:
                        ws.update_cell(cell.row, 10, "競合なし")
                        ws.update_cell(cell.row, 11, result["count"])

                if result["my_rank"] is not None:
                    sheets.update_my_rank(asin, result["my_rank"])

                print(f"  競合最安値: ${result['lowest_price']}  "
                      f"出品数: {result['count']}件  順位: {result['my_rank']}")
                done += 1

            except Exception as e:
                print(f"  ❌ エラー: {e}")
                error += 1

            time.sleep(1)

    finally:
        driver.quit()
        print("\n  ドライバーを終了しました。")

    print(f"  スクレイプ完了: {done}件  エラー: {error}件\n")


# ==========================================
# Step 2: 競合価格連動 価格調整
# ==========================================
def run_adjust(sheets: SheetsManager, dry_run: bool, limit: int) -> None:
    for key in ["TARGET_MARGIN", "EBAY_FEE_RATE", "TARIFF_RATE",
                "MIN_SELL_PRICE_USD", "PRICE_CHANGE_THRESHOLD"]:
        val = sheets.get_settings().get(key)
        if val:
            CONFIG[key] = val

    ebay     = EbayChecker(CONFIG["EBAY_TOKEN"])
    products = sheets.get_active_products()

    # J列が数値の商品のみ（"競合なし" は除外）
    targets = []
    for p in products:
        j = str(p.get("競合最安値(USD)") or "").strip()
        if j and j != "競合なし":
            try:
                float(j)
                targets.append(p)
            except ValueError:
                pass

    if limit > 0:
        targets = targets[:limit]

    print(f"  価格調整対象: {len(targets)}件（J列に競合価格あり）\n")

    updated = skip = error = 0

    for i, product in enumerate(targets):
        identifier    = product.get("ASIN") or product.get("JANコード", "")
        ebay_id       = str(product.get("eBay商品ID", "")).strip()
        name          = product.get("商品名", "")[:40]
        current_price = float(product.get("eBay売値(USD)") or 0)
        rival_price   = float(product.get("競合最安値(USD)") or 0)

        print(f"[調整 {i+1}/{len(targets)}] {name}")

        base_raw = product.get("仕入れ基準価格", "")
        if not str(base_raw).strip():
            print("  ⚠️  仕入れ基準価格未入力 → スキップ")
            skip += 1
            continue

        try:
            product_config = CONFIG.copy()
            margin_raw = product.get("利益率", "")
            if str(margin_raw).strip():
                try:
                    product_config["TARGET_MARGIN"] = float(str(margin_raw).strip())
                except ValueError:
                    pass
            min_raw     = product.get("下限価格(USD)", "")
            product_min = float(min_raw) if str(min_raw).strip() else None
            cost_floor  = calc_sell_price(float(base_raw), product_config, min_price=product_min)

            target    = round(rival_price - 0.01, 2)
            new_price = max(target, cost_floor)

            if new_price > target:
                print(f"  競合${rival_price} - $0.01 = ${target} → 原価下限${cost_floor}を適用")
            else:
                print(f"  競合${rival_price} - $0.01 = ${new_price}")
            print(f"  現在: ${current_price}  →  新価格: ${new_price}")

            if current_price > 0:
                diff = abs(new_price - current_price) / current_price
                if diff <= CONFIG["PRICE_CHANGE_THRESHOLD"]:
                    print(f"  → 変動率 {diff:.1%} ≤ 閾値 → スキップ")
                    skip += 1
                    continue

            if not dry_run:
                if ebay_id:
                    ebay.revise_price(ebay_id, new_price)
                sheets.update_price(identifier, float(base_raw), new_price)
                print("  ✅ 更新完了")
            else:
                print("  [DRY RUN] 更新対象")

            updated += 1

        except Exception as e:
            print(f"  ❌ エラー: {e}")
            error += 1

        time.sleep(0.5)

    print(f"\n  価格調整完了: 更新 {updated}件  スキップ {skip}件  エラー {error}件")
    if dry_run:
        print("  ※ DRY RUN のため実際の更新なし")


# ==========================================
# メイン
# ==========================================
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run",      action="store_true", help="試算のみ（更新しない）")
    parser.add_argument("--limit",        type=int, default=0, help="処理件数上限（0=全件）")
    parser.add_argument("--scrape-only",  action="store_true", help="スクレイプのみ実行")
    parser.add_argument("--adjust-only",  action="store_true", help="価格調整のみ実行")
    args = parser.parse_args()

    do_scrape = not args.adjust_only
    do_adjust = not args.scrape_only

    print("=" * 55)
    mode = []
    if args.dry_run:     mode.append("DRY RUN")
    if args.scrape_only: mode.append("スクレイプのみ")
    if args.adjust_only: mode.append("価格調整のみ")
    label = " / ".join(mode) if mode else "スクレイプ → 価格調整"
    print(f"  競合最安値チェック & 価格調整  [{label}]")
    print("=" * 55)

    sheets    = SheetsManager(CONFIG["SHEET_ID"])
    seller_id = CONFIG.get("EBAY_SELLER_ID", "")
    jpy_rate  = CONFIG.get("JPY_TO_USD", 150.0)

    if do_scrape:
        products = sheets.get_active_products()
        print(f"  対象: {len(products)} 件  セラーID: {seller_id}  JPY/USD: {jpy_rate}")
        print("-" * 55)
        run_scrape(sheets, products, seller_id, jpy_rate, args.limit)

    if do_adjust:
        print("-" * 55)
        print("  価格調整フェーズ開始")
        print("-" * 55)
        run_adjust(sheets, args.dry_run, args.limit)

    print("=" * 55)
    print("  全処理完了")
    print("=" * 55)


if __name__ == "__main__":
    main()
