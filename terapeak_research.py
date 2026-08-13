"""
terapeak_research.py
====================
eBay Terapeak（Seller Hub Research）から売れ筋商品データを取得するスクリプト。

【使い方】
  # カテゴリIDで実行（内部マッピングでキーワード・カテゴリ名を自動設定）
  python3 terapeak_research.py --category 1188

  # キーワードとカテゴリ名を手動指定
  python3 terapeak_research.py --keywords watch --category-name Wristwatches

  # 期間・最低価格も指定
  python3 terapeak_research.py --category 1188 --days 30 --min-price 10

【カテゴリID例（内部マッピング済み）】
  1188  : Watches  → keyword=watch, category=Wristwatches
  267   : Books    → keyword=book, category=Books
  870   : Cameras  → keyword=camera, category=Digital Cameras
  11450 : Clothing → keyword=shirt, category=Tops
  58058 : Toys     → keyword=toy, category=Action Figures
  139   : Games    → keyword=game, category=Video Games

【前提】
  Chrome プロファイル（ebay_session_kaworu）で eBay にログイン済みであること
"""

import os
import csv
import time
import argparse
from datetime import datetime
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager

# ==========================================
# 設定
# ==========================================
BASE_DIR     = os.path.dirname(os.path.abspath(__file__))
PROFILE_NAME = "ebay_session_kaworu"
PROFILE_DIR  = os.path.join(os.path.expanduser("~"), "Library", "Application Support",
                            "Google", "Chrome", PROFILE_NAME)
OUTPUT_DIR   = os.path.join(BASE_DIR, "terapeak_results")
LIMIT        = 50


# カテゴリID → eBayカテゴリ名（ドロップダウンのパンくずリストと照合）
CATEGORY_NAMES = {
    "1188":  "Models & Kits",
    "267":   "Books",
    "870":   "Cameras & Photo",
    "11450": "Clothing",
    "58058": "Toys & Hobbies",
    "139":   "Video Games",
    "220":   "Guitars",
    "625":   "Cameras & Photo",
    "293":   "Laptops & Netbooks",
    "31387": "Wristwatches",
    "237":   "Men's Clothing",
    "15709": "Jewelry",
}

# 日数 → UI ラベル
DAY_LABEL_MAP = {
    7:   "Last 7 days",
    30:  "Last 30 days",
    90:  "Last 90 days",
    180: "Last 6 months",
    365: "Last year",
}


# ==========================================
# ブラウザ起動
# ==========================================
def create_driver():
    opts = Options()
    opts.add_argument(f"--user-data-dir={PROFILE_DIR}")
    opts.add_argument("--profile-directory=Default")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    driver = webdriver.Chrome(
        service=Service(ChromeDriverManager().install()), options=opts
    )
    driver.execute_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )
    driver.execute_cdp_cmd("Network.setCacheDisabled", {"cacheDisabled": True})
    return driver


# ==========================================
# Ship to = United States
# ==========================================
def set_ship_to_us(driver):
    print("  [Ship to] United States に設定中...")
    driver.get("https://www.ebay.com/")
    time.sleep(4)
    try:
        btn = WebDriverWait(driver, 15).until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, ".gh-ship-to__menu"))
        )
        driver.execute_script("arguments[0].click();", btn)
        time.sleep(2)
    except Exception as e:
        print(f"  [Ship to] ボタン未発見: {e}")
        return
    result = driver.execute_script("""
        var items = document.querySelectorAll('li, .menu-button__item, [role="menuitem"]');
        for (var i = 0; i < items.length; i++) {
            if (items[i].innerText && items[i].innerText.trim() === 'United States') {
                items[i].click(); return 'clicked';
            }
        }
        return 'not found';
    """)
    print(f"  [Ship to] US: {result}")
    time.sleep(2)
    done = driver.execute_script("""
        var btns = document.querySelectorAll('button, input[type="submit"]');
        for (var i = 0; i < btns.length; i++) {
            var t = btns[i].innerText ? btns[i].innerText.trim() : btns[i].value || '';
            if (t === 'Done' || t === 'Save' || t === 'Apply') {
                btns[i].click(); return 'clicked: ' + t;
            }
        }
        return 'not found';
    """)
    print(f"  [Ship to] Done: {done}")
    time.sleep(3)
    print("  [Ship to] 完了")


# ==========================================
# Terapeak リサーチ実行（UI 操作）
# ==========================================
def do_research(driver, keywords, days, min_price, category_id):
    """
    1. Terapeak ページへ遷移
    2. キーワード入力
    3. 期間選択
    4. Research クリック
    5. カテゴリドロップダウンをIDで自動深掘り選択
    6. 最低価格を設定
    """
    print(f"  [遷移] Terapeak へ...")
    driver.get("https://www.ebay.com/sh/research?marketplace=EBAY-US&tabName=SOLD")
    time.sleep(6)

    # --- キーワード入力 ---
    try:
        kw_input = WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "input.textbox__control"))
        )
        kw_input.clear()
        kw_input.send_keys(keywords)
        time.sleep(0.5)
        print(f"  [keyword] '{keywords}'")
    except Exception as e:
        print(f"  [keyword] エラー: {e}")

    # --- 期間選択 ---
    period_label = DAY_LABEL_MAP.get(days)
    if period_label:
        try:
            date_btn = WebDriverWait(driver, 10).until(
                EC.element_to_be_clickable((By.CSS_SELECTOR,
                    ".search-input-panel__date-dropdown .menu-button__button"))
            )
            driver.execute_script("arguments[0].click();", date_btn)
            time.sleep(1)
            selected = driver.execute_script("""
                var items = document.querySelectorAll('.menu-button__item');
                for (var i = 0; i < items.length; i++) {
                    var txt = items[i].innerText ? items[i].innerText.trim() : '';
                    if (txt === arguments[0]) { items[i].click(); return 'selected: ' + txt; }
                }
                return 'not found: ' + arguments[0];
            """, period_label)
            print(f"  [period] {selected}")
            time.sleep(1)
        except Exception as e:
            print(f"  [period] エラー: {e}")

    # --- Research クリック ---
    try:
        research_btn = WebDriverWait(driver, 15).until(
            EC.element_to_be_clickable((By.CSS_SELECTOR,
                "button.search-input-panel__research-button"))
        )
        driver.execute_script("arguments[0].click();", research_btn)
        print("  [Research] クリック")
        time.sleep(8)
    except Exception as e:
        print(f"  [Research] エラー: {e}")

    # --- カテゴリ選択（IDで自動深掘り）---
    if category_id:
        _select_category_by_id(driver, category_id)

    # --- 最低価格設定 ---
    if min_price > 0:
        _set_min_price(driver, min_price)


def _select_category_by_id(driver, category_id):
    """
    カテゴリIDでドロップダウンから一致するカテゴリを選択する。
    Teapeakのドロップダウンは検索結果から生成されるフラットなリストで、
    各アイテムに名前（.category-dropdown__name）と
    パンくずリスト（.category-dropdown__breadcrumb）が表示される。
    カテゴリIDに対応するカテゴリ名で照合する。
    """
    target_id = str(category_id)
    target_name = CATEGORY_NAMES.get(target_id, "").lower()

    if not target_name:
        print(f"  [category] カテゴリID {target_id} はCATEGORY_NAMESに未登録。スキップ。")
        return

    print(f"  [category] カテゴリID {target_id} → '{CATEGORY_NAMES[target_id]}' を検索中...")

    try:
        # ドロップダウンを開く
        cat_btn = WebDriverWait(driver, 15).until(
            EC.element_to_be_clickable((By.CSS_SELECTOR,
                "button.category-dropdown__category-button"))
        )
        driver.execute_script("arguments[0].click();", cat_btn)
        time.sleep(2)

        # 全アイテムを取得してパンくずリスト・名前で照合
        result = driver.execute_script("""
            var targetName = arguments[0];
            var items = document.querySelectorAll('.filter-menu-button__item');
            var bestIdx = -1;
            var bestName = '';
            var bestScore = 0;

            for (var i = 0; i < items.length; i++) {
                var nameEl = items[i].querySelector('.category-dropdown__name');
                var breadEl = items[i].querySelector('.category-dropdown__breadcrumb');
                var name = nameEl ? nameEl.innerText.trim() : '';
                var bread = breadEl ? breadEl.innerText.trim() : '';
                var combined = (name + ' ' + bread).toLowerCase();

                // パンくずリストの末尾がカテゴリ名と一致（最優先）
                if (bread.toLowerCase().endsWith(targetName)) {
                    bestIdx = i; bestName = name; bestScore = 3; break;
                }
                // パンくずリストにカテゴリ名が含まれる
                if (bestScore < 2 && bread.toLowerCase().includes(targetName)) {
                    bestIdx = i; bestName = name; bestScore = 2;
                }
                // 名前がカテゴリ名と一致
                if (bestScore < 1 && name.toLowerCase().includes(targetName)) {
                    bestIdx = i; bestName = name; bestScore = 1;
                }
            }

            if (bestIdx >= 0) {
                items[bestIdx].click();
                return {found: true, name: bestName, score: bestScore};
            }

            // 見つからない場合は全カテゴリ一覧を返す
            var all = [];
            items.forEach(function(item) {
                var n = item.querySelector('.category-dropdown__name');
                var b = item.querySelector('.category-dropdown__breadcrumb');
                all.push((n ? n.innerText.trim() : '') + ' | ' + (b ? b.innerText.trim() : ''));
            });
            return {found: false, available: all};
        """, target_name)

        if result['found']:
            print(f"  [category] '{result['name']}' を選択（スコア: {result['score']}）")
            time.sleep(1)
            applied = driver.execute_script("""
                var btns = document.querySelectorAll('button');
                for (var b of btns) {
                    if (b.innerText.trim() === 'Apply') { b.click(); return 'apply clicked'; }
                }
                return 'apply not found';
            """)
            print(f"  [category] {applied}")
            time.sleep(5)
        else:
            print(f"  [category] '{CATEGORY_NAMES[target_id]}' がドロップダウンに見つかりませんでした")
            print(f"  [category] 表示中のカテゴリ:")
            for a in result.get('available', []):
                print(f"    - {a}")
            # ドロップダウンを閉じる
            driver.execute_script("arguments[0].click();", cat_btn)

    except Exception as e:
        print(f"  [category] エラー: {e}")


def _set_min_price(driver, min_price):
    """最低価格フィルターを設定する"""
    try:
        price_input = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.CSS_SELECTOR,
                "input[aria-label='min price filter'], input[placeholder='$']"))
        )
        price_input.clear()
        price_input.send_keys(str(min_price))
        time.sleep(0.5)
        # Enter で適用
        from selenium.webdriver.common.keys import Keys
        price_input.send_keys(Keys.RETURN)
        print(f"  [price] min=${min_price} 設定")
        time.sleep(3)
    except Exception as e:
        print(f"  [price] エラー: {e}")


# ==========================================
# データ抽出
# ==========================================
def extract_rows(driver, timeout=60) -> list[dict]:
    """ページから商品データを抽出する"""
    results = []

    # tr.research-table-row が表示されるまで待つ
    try:
        WebDriverWait(driver, timeout).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "tr.research-table-row"))
        )
        time.sleep(3)
    except Exception:
        print("  [WARN] テーブル行が見つかりません")
        return results

    # ヘッダーのインデックスマップを取得
    idx_map = driver.execute_script("""
        var ths = document.querySelectorAll('th');
        var map = {price: 3, ship: 4, sold: 5, sales: 6, last: 7};
        ths.forEach(function(th, i) {
            var t = th.innerText.toLowerCase();
            if (t.includes('avg sold price')) map.price = i;
            else if (t.includes('avg shipping')) map.ship = i;
            else if (t.includes('total sold')) map.sold = i;
            else if (t.includes('item sales') || t.includes('total sales')) map.sales = i;
            else if (t.includes('last sold')) map.last = i;
        });
        return map;
    """)

    raw = driver.execute_script("""
        var rows = document.querySelectorAll('tr.research-table-row');
        var idxMap = arguments[0];
        var data = [];
        rows.forEach(function(row) {
            // タイトル
            var title = '';
            var sp = row.querySelector('span[data-item-id]');
            if (sp) { title = sp.innerText.trim(); }
            else {
                var a = row.querySelector('a.research-table-row__link');
                if (a) title = a.innerText.trim();
                else {
                    var info = row.querySelector('[class*="product-info"]');
                    if (info) title = info.innerText.trim();
                }
            }
            title = title.split('\\n')[0].trim();

            var tds = row.querySelectorAll('td');
            function getCell(idx) {
                if (idx >= 0 && idx < tds.length) {
                    var lines = tds[idx].innerText.split('\\n')
                        .map(function(s){return s.trim();}).filter(Boolean);
                    return lines[0] || '';
                }
                return '';
            }
            data.push({
                title:       title,
                avg_price:   getCell(idxMap.price),
                avg_ship:    getCell(idxMap.ship),
                sold_count:  getCell(idxMap.sold),
                total_sales: getCell(idxMap.sales),
                last_sold:   getCell(idxMap.last),
            });
        });
        return data;
    """, idx_map)

    print(f"  取得行数: {len(raw)}")
    for item in raw:
        title = item.get("title", "").strip()
        if not title and not item.get("sold_count"):
            continue
        results.append({
            "タイトル":         title,
            "平均販売価格(USD)": item.get("avg_price", ""),
            "平均送料(USD)":    item.get("avg_ship", ""),
            "総販売数":         item.get("sold_count", ""),
            "総売上金額(USD)":  item.get("total_sales", ""),
            "最終販売日":       item.get("last_sold", ""),
        })
    return results


def get_total_count(driver) -> int:
    try:
        import re
        text = driver.execute_script("""
            var el = document.querySelector('[class*="results-header"]');
            return el ? el.innerText : '';
        """)
        nums = re.findall(r'[\d,]+', text or "")
        if nums:
            return int(nums[0].replace(",", ""))
    except Exception:
        pass
    return 0


# ==========================================
# ページネーション
# ==========================================
def go_next_page(driver, page_num):
    """「次のページ」ボタンをクリックする"""
    try:
        next_btn = driver.execute_script("""
            var btns = document.querySelectorAll('button, a');
            for (var i = 0; i < btns.length; i++) {
                var txt = (btns[i].innerText || btns[i].getAttribute('aria-label') || '').trim();
                if (txt === 'Next' || txt === 'Next page' || txt === '>') {
                    btns[i].click(); return 'clicked';
                }
            }
            return 'not found';
        """)
        print(f"  [next page] {next_btn}")
        time.sleep(6)
        return next_btn == 'clicked'
    except Exception:
        return False


# ==========================================
# メイン
# ==========================================
def main():
    parser = argparse.ArgumentParser(description="eBay Terapeak リサーチツール")
    parser.add_argument("--category",     default="",  help="カテゴリID（例: 1188）")
    parser.add_argument("--keywords",     required=True, help="検索キーワード（必須）")
    parser.add_argument("--days",         type=int, default=7,  help="集計期間（日数）デフォルト: 7")
    parser.add_argument("--min-price",    type=int, default=0,  help="最低価格（USD）")
    parser.add_argument("--pages",        type=int, default=1,  help="取得ページ数")
    parser.add_argument("--output",       default="",  help="出力ファイル名")
    parser.add_argument("--skip-ship-to", action="store_true", help="Ship to 設定をスキップ")
    args = parser.parse_args()

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    timestamp   = datetime.now().strftime("%Y%m%d_%H%M")
    label       = args.category or "all"
    output_file = args.output or os.path.join(
        OUTPUT_DIR, f"terapeak_{label}_{timestamp}.csv"
    )

    print("=" * 55)
    print("  eBay Terapeak リサーチ")
    print("=" * 55)
    print(f"  カテゴリID   : {args.category or '（未指定）'}")
    print(f"  キーワード   : {args.keywords}")
    print(f"  期間         : {args.days}日間 ({DAY_LABEL_MAP.get(args.days, '?')})")
    print(f"  最低価格     : ${args.min_price}")
    print(f"  取得ページ   : {args.pages}ページ")
    print(f"  出力先       : {output_file}")
    print()

    driver = create_driver()
    all_results = []

    try:
        if not args.skip_ship_to:
            set_ship_to_us(driver)

        # 1ページ目: UI でリサーチ実行
        print("[ページ 1] UI でリサーチ実行中...")
        do_research(
            driver,
            keywords=args.keywords,
            days=args.days,
            min_price=args.min_price,
            category_id=args.category,
        )

        total = get_total_count(driver)
        if total:
            print(f"  検索結果: 約 {total:,} 件")

        rows = extract_rows(driver)
        if rows:
            all_results.extend(rows)
            print(f"  累計: {len(all_results)}件")
        else:
            print("  データなし")

        # 2ページ目以降
        for page in range(1, args.pages):
            if len(rows) < LIMIT:
                print("  最終ページに達しました")
                break
            print(f"[ページ {page + 1}]")
            success = go_next_page(driver, page + 1)
            if not success:
                print("  次ページボタンが見つかりません")
                break
            rows = extract_rows(driver)
            if not rows:
                print("  データなし")
                break
            all_results.extend(rows)
            print(f"  累計: {len(all_results)}件")
            time.sleep(3)

    finally:
        driver.quit()

    if not all_results:
        print("\nデータが取得できませんでした")
        return

    with open(output_file, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=all_results[0].keys())
        writer.writeheader()
        writer.writerows(all_results)

    print(f"\n保存完了: {output_file}")
    print(f"取得件数: {len(all_results)}件")
    print("=" * 55)


if __name__ == "__main__":
    main()
