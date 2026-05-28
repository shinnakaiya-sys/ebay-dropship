"""
rival_jan_research.py - ライバルセラーのSOLD出品からJANを収集しjan_research.pyに連携

使い方（単体）:
  python3 rival_jan_research.py <セラーID> [アカウント名]
  python3 rival_jan_research.py akibashipping kaworu

使い方（リスト一括）:
  python3 rival_jan_research.py --seller-list rival_sellers.txt

オプション:
  --headless    ブラウザを非表示で実行（VPS用）
  --dry-run     スプレッドシートへの書き込みをスキップ
  --all-jan     日本製以外のJANも収集（デフォルトは日本製のみ）
"""
import os, sys, time, re, base64, subprocess, requests
from dotenv import load_dotenv

load_dotenv(".env.kaworu" if os.path.exists(".env.kaworu") else ".env")
CLIENT_ID     = os.getenv("EBAY_CLIENT_ID") or os.getenv("EBAY_APP_ID", "")
CLIENT_SECRET = os.getenv("EBAY_CLIENT_SECRET", "")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
HEADLESS      = "--headless" in sys.argv or os.getenv("HEADLESS") == "1"
FORCE_RESEARCH = "--force-research" in sys.argv or HEADLESS  # VPS時はTerapeak不要のため自動有効


def get_token():
    r = requests.post(
        "https://api.ebay.com/identity/v1/oauth2/token",
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "Authorization": "Basic " + base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()},
        data={"grant_type": "client_credentials", "scope": "https://api.ebay.com/oauth/api_scope"}
    )
    return r.json().get("access_token") if r.ok else None


def create_driver():
    from selenium import webdriver
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.chrome.options import Options
    from webdriver_manager.chrome import ChromeDriverManager
    os.environ.setdefault("WDM_LOCAL", "1")

    options = Options()
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--lang=en-US")
    options.add_experimental_option("prefs", {"intl.accept_languages": "en-US,en"})

    if HEADLESS:
        # VPS用ヘッドレスモード（プロファイルなし）
        options.add_argument("--headless=new")
        options.add_argument("--window-size=1920,1080")
        options.add_argument("--disable-gpu")
    else:
        # ローカル用（セッションプロファイルあり）
        profile_path = os.path.join(BASE_DIR, "ebay_session")
        options.add_argument(f"--user-data-dir={profile_path}")

    return webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)


def set_ship_to_us(driver):
    """eBayのお届け先をアメリカに設定し、未ログインなら手動ログインを待機する"""
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC

    try:
        driver.get("https://www.ebay.com")
        time.sleep(2)

        # ログイン状態を確認
        page_src = driver.page_source
        is_logged_in = 'id="gh-ug"' in page_src or 'data-location="gh-ug"' in page_src \
                       or ("Sign in" not in page_src and "ログイン" not in page_src[:3000])
        # より確実な判定: ユーザー名要素の存在
        try:
            driver.find_element(By.CSS_SELECTOR, "#gh-ug a, .gh-ug a, [data-marko='gh-ug'] a")
            is_logged_in = True
        except Exception:
            is_logged_in = False

        if not is_logged_in and not HEADLESS:
            print("  ⚠ eBayにログインしていません。")
            print("  → ブラウザで kaworu アカウントにログインしてください。")
            print("  → ログイン後、Enterキーを押して続行します...")
            input()
            driver.get("https://www.ebay.com")
            time.sleep(2)

        # お届け先ボタンを探してクリック
        ship_selectors = [
            "#gh-shipto-click",
            ".gh-shipto-flyout-btn",
            ".gh-shipto",
            "[data-marko='GhShipTo']",
            "button[aria-label*='ship' i]",
            "a[aria-label*='ship' i]",
        ]
        clicked = False
        for sel in ship_selectors:
            try:
                btn = WebDriverWait(driver, 3).until(
                    EC.element_to_be_clickable((By.CSS_SELECTOR, sel))
                )
                driver.execute_script("arguments[0].click();", btn)
                clicked = True
                break
            except Exception:
                continue

        if clicked:
            time.sleep(1.5)
            for us_sel in [
                "a[href*='country=US']",
                "[data-country='US']",
                "[data-value='US']",
                "//a[normalize-space()='United States']",
                "//span[normalize-space()='United States']",
                "//li[normalize-space()='United States']",
            ]:
                try:
                    by = By.XPATH if us_sel.startswith("//") else By.CSS_SELECTOR
                    el = WebDriverWait(driver, 3).until(
                        EC.element_to_be_clickable((by, us_sel))
                    )
                    driver.execute_script("arguments[0].click();", el)
                    time.sleep(1)
                    print("  お届け先: United States ✅")
                    return
                except Exception:
                    continue

        print("  ⚠ お届け先の自動設定スキップ（セッション設定を使用）")
    except Exception as e:
        print(f"  ⚠ お届け先設定エラー: {e}")


def scrape_sold_items(seller_id, max_items):
    """Seleniumを使いeBayのSOLD出品ページからItem IDリストを返す"""
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC

    item_ids = []
    seen = set()
    page = 1

    driver = create_driver()
    set_ship_to_us(driver)
    print(f"  SOLDリストをスクレイピング中: {seller_id}")
    try:
        while len(item_ids) < max_items:
            # ログイン済みなら _armrs=1&_ssn が正常動作する（元の実績ある形式）
            url = (
                f"https://www.ebay.com/sch/i.html"
                f"?_nkw=&_armrs=1&_ipg=240&_ssn={seller_id}"
                f"&LH_Complete=1&LH_Sold=1&rt=nc&_pgn={page}"
            )
            driver.get(url)
            try:
                WebDriverWait(driver, 15).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "ul.srp-results, li.s-item, .srp-river-results"))
                )
            except Exception:
                pass
            time.sleep(3)

            title = driver.title
            page_src_snippet = driver.page_source[:500] if page == 1 else ""

            all_hrefs = set()

            links = driver.find_elements(By.XPATH, "//a[contains(@href,'/itm/')]")
            for a in links:
                href = a.get_attribute("href") or ""
                all_hrefs.add(href)

            items = driver.find_elements(By.CSS_SELECTOR, "li.s-item a.s-item__link")
            for a in items:
                href = a.get_attribute("href") or ""
                all_hrefs.add(href)

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

            print(f"  ページ {page}: {found}件 / 累計 {len(item_ids)}件 [タイトル: {title[:60]}]")

            if found == 0:
                ss_path = os.path.join(BASE_DIR, f"debug_{seller_id}_page{page}.png")
                driver.save_screenshot(ss_path)
                print(f"  ⚠ 0件 → スクリーンショット保存: {ss_path}")
                if page_src_snippet:
                    print(f"  ページソース冒頭: {page_src_snippet[:200]}")
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


def get_item_details(item_id, token):
    """アイテム詳細（GTIN, タイトル, MPN）を返す"""
    api_id = item_id if item_id.startswith("v1|") else f"v1|{item_id}|0"
    for attempt in range(5):
        r = requests.get(f"https://api.ebay.com/buy/browse/v1/item/{api_id}",
                         headers={"Authorization": f"Bearer {token}"})
        if r.ok:
            data = r.json()
            gtin = data.get("gtin", "").strip()
            title = data.get("title", "").strip()
            mpn = ""
            for a in data.get("localizedAspects", []):
                name = a.get("name", "").lower()
                if name in ["upc", "ean", "jan"] and not gtin:
                    gtin = a.get("value", "").strip()
                if name in ["mpn", "model", "part number", "manufacturer part number", "型番", "モデル番号"]:
                    mpn = a.get("value", "").strip()
            return gtin, title, mpn
        if r.status_code == 429:
            time.sleep(60 * (attempt + 1))
        else:
            return "", "", ""
    return "", "", ""


def extract_model_from_title(title):
    """タイトルから型番らしき文字列を抽出する"""
    patterns = [
        r'\b([A-Z]{2,}[-]?[0-9]{2,}[A-Z0-9\-]*)\b',
        r'\b([A-Z][0-9]{3,}[A-Z0-9\-]*)\b',
        r'\b([0-9]{2,}[A-Z]{2,}[0-9A-Z\-]*)\b',
    ]
    candidates = []
    for pat in patterns:
        candidates.extend(re.findall(pat, title.upper()))
    # 短すぎる・長すぎるもの、純粋な数字のみを除外
    seen = set()
    result = []
    for c in candidates:
        if 4 <= len(c) <= 20 and not c.isdigit() and c not in seen:
            seen.add(c)
            result.append(c)
    return result


def search_jan_by_keyword(keyword, token):
    """キーワードでeBay商品を検索し、日本製JANを持つ商品のGTINを返す"""
    url = "https://api.ebay.com/buy/browse/v1/item_summary/search"
    params = {"q": keyword, "limit": 10, "filter": "itemLocationCountry:JP"}
    try:
        r = requests.get(url, headers={"Authorization": f"Bearer {token}"}, params=params, timeout=10)
        if not r.ok:
            return ""
        for item in r.json().get("itemSummaries", []):
            gtin = item.get("gtin", "").strip()
            if gtin and is_japan_jan(gtin):
                return gtin
    except Exception:
        pass
    return ""


def get_jan_from_title_or_mpn(title, mpn, token):
    """タイトルまたはMPNからJANコードをフォールバック検索する"""
    # 1. MPNで検索
    if mpn:
        jan = search_jan_by_keyword(mpn, token)
        if jan:
            print(f"    (MPN '{mpn}' から JAN取得: {jan})")
            return jan

    # 2. タイトルから型番を抽出して検索（最大3パターン）
    for model in extract_model_from_title(title)[:3]:
        jan = search_jan_by_keyword(model, token)
        if jan:
            print(f"    (型番 '{model}' から JAN取得: {jan})")
            return jan

    return ""


def is_japan_jan(code):
    return len(code) == 13 and code.isdigit() and (code.startswith("45") or code.startswith("49"))


def run_one_seller(seller_id, account, max_items, dry_run, japan_only, token):
    """1セラー分のリサーチを実行"""
    print(f"\n{'='*60}")
    print(f"セラー: {seller_id} / アカウント: {account} / 上限: {max_items}件")
    print(f"{'='*60}")

    BATCH_SIZE = 30

    def flush_jans(new_jans):
        if not new_jans:
            return
        jan_list = sorted(new_jans)
        print(f"\n  → {len(jan_list)}件のJANをスプレッドシートに書き込み中...")
        cmd = [sys.executable, os.path.join(BASE_DIR, "jan_research.py"),
               "--account", account] + jan_list
        if dry_run:
            cmd.append("--dry-run")
        if FORCE_RESEARCH:
            cmd.append("--force")  # VPS: Terapeak不要（Chrome session不要）
        subprocess.run(cmd, cwd=BASE_DIR)
        print(f"  → 書き込み完了\n")

    print("SOLDリスト収集中...")
    item_ids = scrape_sold_items(seller_id, max_items)
    print(f"→ {len(item_ids)}件取得\n")

    print("JANコード取得中...")
    jan_set = set()
    written_jans = set()
    for i, item_id in enumerate(item_ids):
        gtin, title, mpn = get_item_details(item_id, token)

        # GTINが取得できなかった場合、タイトル/MPNからフォールバック検索
        if not gtin and (title or mpn):
            gtin = get_jan_from_title_or_mpn(title, mpn, token)

        if gtin and (not japan_only or is_japan_jan(gtin)):
            jan_set.add(gtin)
            print(f"  [{i+1}/{len(item_ids)}] {item_id} → {gtin} ✅")
        else:
            print(f"  [{i+1}/{len(item_ids)}] {item_id} → {'非日本製' if gtin else 'JAN未登録'}")
        time.sleep(0.5)

        if (i + 1) % BATCH_SIZE == 0:
            new_jans = jan_set - written_jans
            flush_jans(new_jans)
            written_jans |= new_jans

    new_jans = jan_set - written_jans
    if new_jans:
        print(f"\n残り{len(new_jans)}件のJANを書き込み中...")
        flush_jans(new_jans)
    elif not jan_set:
        print("❌ JANコードが取得できませんでした。")

    print(f"完了: {seller_id} → JAN合計 {len(jan_set)}件（重複除去済み）")
    return len(jan_set)


def load_seller_list(path):
    """
    rival_sellers.txt を読み込む
    フォーマット: seller_id,account_name  （#から始まる行はコメント）
    """
    sellers = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = [p.strip() for p in line.split(",")]
            seller_id = parts[0]
            account   = parts[1] if len(parts) > 1 else "kozuki"
            sellers.append((seller_id, account))
    return sellers


def main():
    flags = set(sys.argv[1:])
    dry_run    = "--dry-run" in flags
    japan_only = "--all-jan" not in flags

    token = get_token()
    if not token:
        print("❌ トークン取得失敗"); sys.exit(1)

    # ---- セラーリストモード ----
    if "--seller-list" in sys.argv:
        idx = sys.argv.index("--seller-list")
        list_file = sys.argv[idx + 1]
        if not os.path.exists(list_file):
            print(f"❌ セラーリストが見つかりません: {list_file}"); sys.exit(1)

        sellers = load_seller_list(list_file)
        if not sellers:
            print("❌ セラーリストが空です"); sys.exit(1)

        print(f"セラーリスト読み込み: {len(sellers)}件")
        total_jans = 0
        for seller_id, account in sellers:
            try:
                total_jans += run_one_seller(seller_id, account, 600, dry_run, japan_only, token)
            except Exception as e:
                print(f"⚠ {seller_id} でエラー: {e}")
                continue

        print(f"\n{'='*60}")
        print(f"全セラー完了: JAN合計 {total_jans}件")
        return

    # ---- 単体モード ----
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print("使い方: python3 rival_jan_research.py <セラーID> [アカウント名]")
        print("       python3 rival_jan_research.py --seller-list rival_sellers.txt")
        sys.exit(1)

    seller_id = args[0]
    account   = args[1] if len(args) > 1 else "kozuki"
    run_one_seller(seller_id, account, 600, dry_run, japan_only, token)


if __name__ == "__main__":
    main()
