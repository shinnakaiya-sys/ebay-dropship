"""
scrape_and_adjust_v2.py
========================
scrape_and_adjust.py の改善版。既存の scrape_and_adjust.py は変更せず、
新規ファイルとして並行運用・比較テストできるようにしたもの。

scrape_and_adjust.py からの変更点:
  1. Google Sheets API 呼び出しを大幅削減
     - ワークシートオブジェクト・行インデックスをキャッシュ（sheets_manager_v2.py）
     - J/K/N列、E/F/H列の更新をそれぞれ1バッチにまとめる
     - 設定シートの読み込みをループの外で1回だけ行う
  2. Chromeドライバーをバッチごとに再起動せず、実行全体で使い回す
  3. --dry-run がスクレイプ結果のシート書き込みにも効くようにした
     （旧版は eBay 更新だけ止まり、J/K/N列は dry-run でも書き換わっていた）
  4. Selenium のセッション切れ（WebDriverException）を検知してドライバーを
     自動再起動する（旧版は例外を握りつぶして残り全件がエラーになっていた）
  5. セラー判定を完全一致に変更、送料パースの取りこぼしを修正
     （ロジックは pricing_logic.py に分離してユニットテスト可能にした）
  6. --scrape-only / --adjust-only を排他オプションにした
  7. 数値セルのパースに to_float() を使い、"$1,234.56" のような
     記号付きの値でも ValueError にならないようにした

使い方（既存の scrape_and_adjust.py と同じインターフェース）:
  python3 scrape_and_adjust_v2.py
  python3 scrape_and_adjust_v2.py --limit 10
  python3 scrape_and_adjust_v2.py --dry-run
  python3 scrape_and_adjust_v2.py --scrape-only
  python3 scrape_and_adjust_v2.py --adjust-only
"""

import sys
import time
import argparse

from config import CONFIG
from sheets_manager_v2 import SheetsManagerV2
from sheets_manager import SHEET_MASTER
from ebay_checker import EbayChecker
from keepa_checker import KeepaChecker
from run import calc_sell_price

from pricing_logic import (
    RawItem,
    normalize_search_url,
    analyze_items,
    decide_new_price,
    has_rival_price,
    product_identifier,
    to_float,
)

SETTINGS_KEYS = ["TARGET_MARGIN", "EBAY_FEE_RATE", "TARIFF_RATE", "MIN_SELL_PRICE_USD"]


# ==========================================
# Chromeドライバー起動（scrape_and_adjust.py と同一設定）
# ==========================================
def create_driver():
    import os
    import shutil
    from selenium import webdriver
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.chrome.options import Options

    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
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
# eBay検索ページから RawItem のリストを抽出 → pricing_logic に集計させる
# ==========================================
def scrape_ebay_search(driver, url: str, my_seller_id: str, jpy_rate: float,
                       my_item_id: str = ""):
    from selenium.webdriver.common.by import By
    import re as _re

    driver.get(normalize_search_url(url))
    time.sleep(4)

    elements = driver.find_elements(By.CSS_SELECTOR, "ul.srp-results li")
    raw_items = []

    for el in elements:
        links = el.find_elements(By.CSS_SELECTOR, "a[href*='/itm/']")
        if not links:
            continue
        href = links[0].get_attribute("href") or ""
        m = _re.search(r"/itm/(?:[^/?#]+/)?(\d{9,})", href)
        if not m:
            continue
        item_id = m.group(1)

        seller_text = ""
        for a_el in el.find_elements(By.CSS_SELECTOR, "a[href*='/usr/']"):
            txt = a_el.text.strip()
            if txt:
                seller_text = txt
                break
        if not seller_text:
            for css in ("span.s-item__seller-info-text",
                        "span[class*='seller-info']",
                        "span.su-styled-text.primary.large"):
                for sel_el in el.find_elements(By.CSS_SELECTOR, css):
                    txt = sel_el.text.strip()
                    if txt:
                        seller_text = txt
                        break
                if seller_text:
                    break

        price_text = ""
        for pel in el.find_elements(By.CSS_SELECTOR, "[class*='s-card__price']"):
            if pel.text.strip():
                price_text = pel.text
                break

        shipping_text = ""
        for sel_el in el.find_elements(By.CSS_SELECTOR, "span[class*='secondary']"):
            txt = sel_el.text.strip()
            if "ship" in txt.lower() or "free" in txt.lower():
                shipping_text = txt
                break

        raw_items.append(RawItem(item_id=item_id, seller_text=seller_text,
                                 price_text=price_text, shipping_text=shipping_text))

    return analyze_items(raw_items, my_seller_id, my_item_id, jpy_rate)


# ==========================================
# Step 1: 競合最安値スクレイプ
# ==========================================
def run_scrape(driver_holder: dict, sheets: SheetsManagerV2, products: list,
               seller_id: str, jpy_rate: float, limit: int, dry_run: bool) -> None:
    targets = [p for p in products if str(p.get("最安値順URL") or "").startswith("http")]
    print(f"  M列URL有り: {len(targets)} 件")

    if limit > 0:
        targets = targets[:limit]
        print(f"  ※ --limit {limit} 件のみ処理")

    from selenium.common.exceptions import WebDriverException

    done = error = 0
    consecutive_driver_errors = 0

    for i, product in enumerate(targets):
        identifier = product_identifier(product)
        name       = product.get("商品名", "")
        url        = str(product.get("最安値順URL", "")).strip()
        ebay_id    = str(product.get("eBay商品ID", "")).strip()

        print(f"[スクレイプ {i+1}/{len(targets)}] {name[:40]}")

        try:
            result = scrape_ebay_search(driver_holder["driver"], url, seller_id,
                                        jpy_rate, my_item_id=ebay_id)

            if dry_run:
                print(f"  [DRY RUN] 競合最安値: ${result.lowest_price}  "
                      f"出品数: {result.count}件  順位: {result.my_rank}")
            else:
                sheets.apply_rival_result(identifier, result.lowest_price,
                                          result.count, result.my_rank)
                print(f"  競合最安値: ${result.lowest_price}  "
                      f"出品数: {result.count}件  順位: {result.my_rank}")
            done += 1
            consecutive_driver_errors = 0

        except WebDriverException as e:
            print(f"  ❌ ドライバー異常: {e}")
            error += 1
            consecutive_driver_errors += 1
            try:
                driver_holder["driver"].quit()
            except Exception:
                pass
            print("  🔄 ドライバーを再起動します...")
            driver_holder["driver"] = create_driver()
            if consecutive_driver_errors >= 3:
                print("  ⚠️  連続でドライバーエラーが発生したため中断します")
                break

        except Exception as e:
            print(f"  ❌ エラー: {e}")
            error += 1

        time.sleep(1)

    print(f"  スクレイプ完了: {done}件  エラー: {error}件\n")


# ==========================================
# Step 2: 競合価格連動 価格調整
# ==========================================
def run_adjust(sheets: SheetsManagerV2, ebay, keepa, dry_run: bool,
               limit: int = 0, filter_ids: list = None) -> None:
    products = sheets.get_active_products()

    if filter_ids:
        id_set   = set(filter_ids)
        products = [p for p in products if product_identifier(p) in id_set]

    targets = [p for p in products if has_rival_price(p)]

    if limit > 0:
        targets = targets[:limit]

    print(f"  価格調整対象: {len(targets)}件（J列に競合価格あり）\n")

    updated = skip = error = 0

    for i, product in enumerate(targets):
        identifier    = product_identifier(product)
        ebay_id       = str(product.get("eBay商品ID", "")).strip()
        name          = product.get("商品名", "")[:40]
        current_price = to_float(product.get("eBay売値(USD)"))
        rival_price   = to_float(product.get("競合最安値(USD)"))

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
            product_min = to_float(min_raw) if str(min_raw).strip() else None

            jan_code  = str(product.get("JANコード", "")).strip()
            asin_code = str(product.get("ASIN", "")).strip()
            weight_kg = sheets.get_weight_from_research(jan_code) if jan_code else None
            length_cm = width_cm = height_cm = 0
            if weight_kg is None and asin_code and keepa:
                weight_kg, length_cm, width_cm, height_cm = keepa.get_weight(asin_code)
                if weight_kg:
                    print(f"  📦 Keepaから重量取得: {weight_kg}kg ({length_cm:.0f}×{width_cm:.0f}×{height_cm:.0f}cm)")
            weight_kg = weight_kg or 1.0

            cost_floor = calc_sell_price(to_float(base_raw), product_config, weight_kg,
                                         min_price=product_min,
                                         length_cm=length_cm, width_cm=width_cm, height_cm=height_cm)
            decision = decide_new_price(rival_price, cost_floor)

            if decision.floor_applied:
                print(f"  競合${rival_price} - $0.01 = ${decision.undercut_price} "
                      f"→ 原価下限${decision.cost_floor}を適用")
            else:
                print(f"  競合${rival_price} - $0.01 = ${decision.new_price}")
            print(f"  現在: ${current_price}  →  新価格: ${decision.new_price}")

            if not dry_run:
                if ebay_id:
                    ebay.revise_price(ebay_id, decision.new_price)
                sheets.apply_price_update(identifier, to_float(base_raw), decision.new_price)
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
    sys.stdout.reconfigure(line_buffering=True)

    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="試算のみ（シート・eBay更新なし）")
    parser.add_argument("--limit",   type=int, default=0, help="処理件数上限（0=全件）")

    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--scrape-only", action="store_true", help="スクレイプのみ実行")
    mode_group.add_argument("--adjust-only", action="store_true", help="価格調整のみ実行")
    args = parser.parse_args()

    do_scrape = not args.adjust_only
    do_adjust = not args.scrape_only

    print("=" * 55)
    mode = []
    if args.dry_run:     mode.append("DRY RUN")
    if args.scrape_only: mode.append("スクレイプのみ")
    if args.adjust_only: mode.append("価格調整のみ")
    label = " / ".join(mode) if mode else "スクレイプ → 価格調整"
    print(f"  競合最安値チェック & 価格調整 v2  [{label}]")
    print("=" * 55)

    sheets    = SheetsManagerV2(CONFIG["SHEET_ID"], CONFIG.get("GSHEET_CRED_PATH", "credentials.json"))
    seller_id = CONFIG.get("EBAY_SELLER_ID", "")
    jpy_rate  = CONFIG.get("JPY_TO_USD", 150.0)

    # 設定シートはループの外で1回だけ読み込む（旧版はバッチごとに複数回読んでいた）
    if do_adjust:
        sheets.apply_settings_to(CONFIG, SETTINGS_KEYS)

    ebay  = EbayChecker(CONFIG["EBAY_TOKEN"]) if do_adjust else None
    keepa = (KeepaChecker(CONFIG["KEEPA_API_KEY"])
             if do_adjust and CONFIG.get("KEEPA_API_KEY") else None)

    BATCH_SIZE = 10
    driver_holder = {"driver": None}

    try:
        if do_scrape and do_adjust:
            products = sheets.get_active_products()
            targets  = [p for p in products
                        if str(p.get("最安値順URL") or "").startswith("http")]
            if args.limit > 0:
                targets = targets[:args.limit]

            print(f"  対象: {len(products)} 件  M列URL有り: {len(targets)} 件  "
                  f"セラーID: {seller_id}  JPY/USD: {jpy_rate}")

            if not targets:
                print("  ⚠️  対象商品がありません")
            else:
                print("[初期化] Chromeドライバー起動中...")
                driver_holder["driver"] = create_driver()
                print("  ✅ 起動完了\n")

                total_batches = (len(targets) + BATCH_SIZE - 1) // BATCH_SIZE
                for bi, i in enumerate(range(0, len(targets), BATCH_SIZE), start=1):
                    batch = targets[i:i + BATCH_SIZE]
                    print(f"\n{'='*55}")
                    print(f"  バッチ {bi}/{total_batches}"
                          f"（{i+1}〜{min(i+BATCH_SIZE, len(targets))}件目）")
                    print(f"{'='*55}")
                    print("-" * 55)
                    run_scrape(driver_holder, sheets, batch, seller_id, jpy_rate,
                              limit=0, dry_run=args.dry_run)
                    batch_ids = [product_identifier(p) for p in batch]
                    print("-" * 55)
                    print("  価格調整フェーズ")
                    print("-" * 55)
                    run_adjust(sheets, ebay, keepa, args.dry_run, filter_ids=batch_ids)

        elif do_scrape:
            products = sheets.get_active_products()
            print(f"  対象: {len(products)} 件  セラーID: {seller_id}  JPY/USD: {jpy_rate}")
            print("-" * 55)
            print("[初期化] Chromeドライバー起動中...")
            driver_holder["driver"] = create_driver()
            print("  ✅ 起動完了\n")
            run_scrape(driver_holder, sheets, products, seller_id, jpy_rate,
                      args.limit, dry_run=args.dry_run)

        elif do_adjust:
            print("-" * 55)
            print("  価格調整フェーズ開始")
            print("-" * 55)
            run_adjust(sheets, ebay, keepa, args.dry_run, limit=args.limit)

    finally:
        if driver_holder["driver"] is not None:
            driver_holder["driver"].quit()
            print("\n  ドライバーを終了しました。")

    print("=" * 55)
    print("  全処理完了")
    print("=" * 55)


if __name__ == "__main__":
    main()
