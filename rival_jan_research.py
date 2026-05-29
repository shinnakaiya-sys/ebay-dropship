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
  --force       jan_research.pyにもTeapeakスキップを指定
"""
import os, sys, time, re, base64, subprocess, requests
from dotenv import load_dotenv

load_dotenv(".env.kaworu" if os.path.exists(".env.kaworu") else ".env")
CLIENT_ID     = os.getenv("EBAY_CLIENT_ID") or os.getenv("EBAY_APP_ID", "")
CLIENT_SECRET = os.getenv("EBAY_CLIENT_SECRET", "")

BASE_DIR       = os.path.dirname(os.path.abspath(__file__))
HEADLESS       = "--headless" in sys.argv or os.getenv("HEADLESS") == "1"
FORCE_RESEARCH = "--force-research" in sys.argv or "--force" in sys.argv or HEADLESS

# Browse API トークンキャッシュ（フォールバック用）
_token_cache = {"value": None, "expires_at": 0}

DRIVER_RESTART_EVERY = 150  # このページ数ごとにドライバー再起動（メモリ対策）


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
            _token_cache["value"] = data.get("access_token")
            _token_cache["expires_at"] = now + data.get("expires_in", 7200)
            return _token_cache["value"]
    except Exception:
        pass
    return None


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
    options.add_argument("--window-size=1920,1080")
    options.add_argument(
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    options.add_experimental_option("prefs", {"intl.accept_languages": "en-US,en"})

    if HEADLESS:
        options.add_argument("--headless=new")
        options.add_argument("--disable-gpu")
    else:
        profile_path = os.path.join(BASE_DIR, "ebay_session")
        options.add_argument(f"--user-data-dir={profile_path}")

    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)

    try:
        from selenium_stealth import stealth
        stealth(driver,
                languages=["en-US", "en"],
                vendor="Google Inc.",
                platform="Win32",
                webgl_vendor="Intel Inc.",
                renderer="Intel Iris OpenGL Engine",
                fix_hairline=True)
    except ImportError:
        pass

    return driver


def scrape_sold_items(seller_id, max_items):
    """Seleniumを使いeBayのSOLD出品ページからItem IDリストを返す"""
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC

    item_ids = []
    seen = set()
    page = 1

    driver = create_driver()
    print(f"  SOLDリストをスクレイピング中: {seller_id}")
    try:
        while len(item_ids) < max_items:
            url = (
                f"https://www.ebay.com/sch/i.html"
                f"?_nkw=&_armrs=1&_ipg=240&_ssn={seller_id}"
                f"&LH_Complete=1&LH_Sold=1&rt=nc&_pgn={page}"
            )
            driver.get(url)
            try:
                WebDriverWait(driver, 15).until(
                    EC.presence_of_element_located(
                        (By.CSS_SELECTOR, "ul.srp-results, li.s-item, .srp-river-results"))
                )
            except Exception:
                pass
            time.sleep(3)

            title = driver.title
            page_src_snippet = driver.page_source[:500] if page == 1 else ""

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


def get_gtin_from_item_page(item_id, driver):
    """
    SeleniumでeBayアイテムページを開き GTIN・タイトル・MPN を取得する。
    Browse APIを使わないためレートリミットを回避できる。
    """
    from selenium.webdriver.common.by import By

    try:
        driver.get(f"https://www.ebay.com/itm/{item_id}")
        time.sleep(2)

        src = driver.page_source
        gtin, title, mpn = "", "", ""

        # ① ページソースの JSON-LD から GTIN（最速）
        m = re.search(r'"gtin(?:13)?"\s*:\s*"(\d{8,14})"', src)
        if m:
            gtin = m.group(1)

        # ② item specifics テーブルから UPC / EAN / JAN / MPN
        for row in driver.find_elements(By.CSS_SELECTOR, ".ux-labels-values"):
            lines = [l.strip() for l in row.text.split("\n") if l.strip()]
            if len(lines) < 2:
                continue
            label = lines[0].lower().rstrip(":")
            value = lines[1]
            if label in ("upc", "ean", "jan") and not gtin:
                gtin = value
            elif label == "mpn" and not mpn:
                mpn = value
            elif label in ("title", "name") and not title:
                title = value

        # ③ ページタイトルからタイトル補完
        if not title:
            page_title = driver.title
            if "|" in page_title:
                title = page_title.split("|")[0].strip()

        return gtin, title, mpn

    except Exception as e:
        print(f"    ⚠ ページ取得エラー ({item_id}): {e}")
        return "", "", ""


def is_japan_jan(code):
    return len(code) == 13 and code.isdigit() and (
        code.startswith("45") or code.startswith("49"))


def extract_model_from_title(title):
    patterns = [
        r'\b([A-Z]{2,}[-]?[0-9]{2,}[A-Z0-9\-]*)\b',
        r'\b([A-Z][0-9]{3,}[A-Z0-9\-]*)\b',
        r'\b([0-9]{2,}[A-Z]{2,}[0-9A-Z\-]*)\b',
    ]
    candidates = []
    for pat in patterns:
        candidates.extend(re.findall(pat, title.upper()))
    seen = set()
    result = []
    for c in candidates:
        if 4 <= len(c) <= 20 and not c.isdigit() and c not in seen:
            seen.add(c)
            result.append(c)
    return result


def search_jan_by_keyword(keyword):
    """Browse APIでキーワード検索（レートリミット時はスキップ）"""
    token = get_token()
    if not token:
        return ""
    try:
        r = requests.get(
            "https://api.ebay.com/buy/browse/v1/item_summary/search",
            headers={"Authorization": f"Bearer {token}"},
            params={"q": keyword, "limit": 10, "filter": "itemLocationCountry:JP"},
            timeout=10,
        )
        if r.status_code == 429:
            return ""  # レートリミット時は諦める
        if not r.ok:
            return ""
        for item in r.json().get("itemSummaries", []):
            gtin = item.get("gtin", "").strip()
            if gtin and is_japan_jan(gtin):
                return gtin
    except Exception:
        pass
    return ""


def get_jan_from_title_or_mpn(title, mpn):
    if mpn:
        jan = search_jan_by_keyword(mpn)
        if jan:
            print(f"    (MPN '{mpn}' から JAN取得: {jan})")
            return jan
    for model in extract_model_from_title(title)[:3]:
        jan = search_jan_by_keyword(model)
        if jan:
            print(f"    (型番 '{model}' から JAN取得: {jan})")
            return jan
    return ""


def run_one_seller(seller_id, account, max_items, dry_run, japan_only):
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
            cmd.append("--force")
        subprocess.run(cmd, cwd=BASE_DIR)
        print(f"  → 書き込み完了\n")

    print("SOLDリスト収集中...")
    item_ids = scrape_sold_items(seller_id, max_items)
    print(f"→ {len(item_ids)}件取得\n")

    if not item_ids:
        print("❌ アイテムが取得できませんでした。")
        return 0

    total = len(item_ids)
    print(f"JANコード取得中... (アイテムページ直接スクレイピング / 約{total * 2 // 60}分)")

    jan_set = set()
    written_jans = set()
    driver = create_driver()

    try:
        for i, item_id in enumerate(item_ids):
            # 一定件数ごとにドライバー再起動（メモリ対策）
            if i > 0 and i % DRIVER_RESTART_EVERY == 0:
                driver.quit()
                print(f"  [ドライバー再起動 {i}/{total}]")
                driver = create_driver()

            gtin, title, mpn = get_gtin_from_item_page(item_id, driver)

            # GTIN未取得かつMPN/タイトルありなら Browse API で補完（レートリミット時は無視）
            if not gtin and (title or mpn):
                gtin = get_jan_from_title_or_mpn(title, mpn)

            if gtin and (not japan_only or is_japan_jan(gtin)):
                jan_set.add(gtin)
                print(f"  [{i+1}/{total}] {item_id} → {gtin} ✅")
            else:
                reason = "非日本製" if gtin else "JAN未登録"
                print(f"  [{i+1}/{total}] {item_id} → {reason}")

            if (i + 1) % BATCH_SIZE == 0:
                new_jans = jan_set - written_jans
                flush_jans(new_jans)
                written_jans |= new_jans

    finally:
        driver.quit()

    new_jans = jan_set - written_jans
    if new_jans:
        print(f"\n残り{len(new_jans)}件のJANを書き込み中...")
        flush_jans(new_jans)
    elif not jan_set:
        print("❌ JANコードが取得できませんでした。")

    print(f"完了: {seller_id} → JAN合計 {len(jan_set)}件（重複除去済み）")
    return len(jan_set)


def load_seller_list(path):
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
    dry_run    = "--dry-run" in sys.argv
    japan_only = "--all-jan" not in sys.argv

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
                total_jans += run_one_seller(seller_id, account, 600, dry_run, japan_only)
            except Exception as e:
                print(f"⚠ {seller_id} でエラー: {e}")
                continue

        print(f"\n{'='*60}")
        print(f"全セラー完了: JAN合計 {total_jans}件")
        return

    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print("使い方: python3 rival_jan_research.py <セラーID> [アカウント名]")
        print("       python3 rival_jan_research.py --seller-list rival_sellers.txt")
        sys.exit(1)

    seller_id = args[0]
    account   = args[1] if len(args) > 1 else "kozuki"
    run_one_seller(seller_id, account, 600, dry_run, japan_only)


if __name__ == "__main__":
    main()
