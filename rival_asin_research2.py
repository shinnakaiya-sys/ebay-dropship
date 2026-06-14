"""
rival_asin_research2.py - ライバルセラーのSOLD出品からJAN/ASINを収集しjan_research2.pyに連携

処理フロー:
  1. eBayのSOLDリストからItem IDを収集
  2. 各アイテムのGTIN(JAN)を取得（Browse API優先 → Selenium）
  3. JANあり   → jan_research2.py へ（従来通り）
  4. JANなし   → MPN/型番で Amazon.co.jp を検索 → ASIN候補をタイトル照合で特定
     → ASINをそのまま jan_research2.py へ渡す
       （jan_research2.py側がKeepaにasin=で問い合わせ、eanListからJANを復元して
         既存の4ステップリサーチ・利益計算を実行する。トークン消費はJANの場合と同じ1件1トークン）

特定したASINは asin_results_<セラーID>.csv にも記録（Amazonタイトル・URL付き、確認用）

使い方:
  python3 rival_asin_research2.py <セラーID> [アカウント名]
  python3 rival_asin_research2.py --seller-list rival_sellers.txt

オプション:
  --headless / --dry-run / --all-jan / --force / --max-items N / --no-api / --no-resume
  --no-amazon            Amazon ASIN検索をスキップ（従来のJAN収集のみ）
  --min-similarity 0.4   eBay/Amazonタイトル照合のしきい値（0〜1）
"""
import os
import sys
import csv
import time
import re
import base64
import logging
import argparse
import subprocess
import requests
from urllib.parse import quote_plus
from dotenv import load_dotenv

load_dotenv(".env.kaworu" if os.path.exists(".env.kaworu") else ".env")
CLIENT_ID     = os.getenv("EBAY_CLIENT_ID") or os.getenv("EBAY_APP_ID", "")
CLIENT_SECRET = os.getenv("EBAY_CLIENT_SECRET", "")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

RESEARCH_SCRIPT      = "jan_research2.py"  # JAN/ASIN両対応版
DRIVER_RESTART_EVERY = 150   # このページ数ごとにドライバー再起動（メモリ対策）
BATCH_SIZE           = 30    # この件数ごとにjan_research2.pyへ書き込み
API_COOLDOWN_SEC     = 600   # 429を受けたらこの秒数だけBrowse APIを休止
AMAZON_COOLDOWN_SEC  = 900   # Amazon CAPTCHA検出時の休止秒数
AMAZON_WAIT_SEC      = 2.5   # Amazon検索ページの読み込み待ち

# ---------------------------------------------------------------- logging
logger = logging.getLogger("rival_asin")
logger.setLevel(logging.INFO)
_sh = logging.StreamHandler()
_sh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S"))
logger.addHandler(_sh)
_fh = logging.FileHandler(os.path.join(BASE_DIR, "rival_asin_research.log"), encoding="utf-8")
_fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
logger.addHandler(_fh)

# ---------------------------------------------------------------- token / cooldown
_token_cache  = {"value": None, "expires_at": 0}
_api_state    = {"disabled_until": 0}   # eBay Browse API 429クールダウン
_amazon_state = {"disabled_until": 0}   # Amazon CAPTCHAクールダウン


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


def api_available():
    return time.time() >= _api_state["disabled_until"]


def api_cooldown():
    _api_state["disabled_until"] = time.time() + API_COOLDOWN_SEC
    logger.warning(f"  Browse APIレートリミット → {API_COOLDOWN_SEC}秒休止しSeleniumに切替")


def amazon_available():
    return time.time() >= _amazon_state["disabled_until"]


def amazon_cooldown():
    _amazon_state["disabled_until"] = time.time() + AMAZON_COOLDOWN_SEC
    logger.warning(f"  ⚠ Amazon CAPTCHA検出 → {AMAZON_COOLDOWN_SEC}秒間Amazon検索を休止")


# ---------------------------------------------------------------- selenium
def create_driver(headless):
    from selenium import webdriver
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.chrome.options import Options
    from webdriver_manager.chrome import ChromeDriverManager
    os.environ.setdefault("WDM_LOCAL", "1")

    options = Options()
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--lang=ja-JP")
    options.add_argument("--window-size=1920,1080")
    options.add_argument(
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    options.add_experimental_option("prefs", {"intl.accept_languages": "ja-JP,ja,en-US,en"})

    if headless:
        options.add_argument("--headless=new")
        options.add_argument("--disable-gpu")
    else:
        profile_path = os.path.join(BASE_DIR, "ebay_session")
        options.add_argument(f"--user-data-dir={profile_path}")

    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)

    try:
        from selenium_stealth import stealth
        stealth(driver,
                languages=["ja-JP", "ja", "en-US", "en"],
                vendor="Google Inc.",
                platform="Win32",
                webgl_vendor="Intel Inc.",
                renderer="Intel Iris OpenGL Engine",
                fix_hairline=True)
    except ImportError:
        pass

    return driver


def safe_quit(driver):
    if driver is None:
        return
    try:
        driver.quit()
    except Exception:
        pass


# ---------------------------------------------------------------- SOLDリスト
def scrape_sold_items(seller_id, max_items, headless):
    """Seleniumを使いeBayのSOLD出品ページからItem IDリストを返す"""
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC

    item_ids = []
    seen = set()
    page = 1

    driver = create_driver(headless)
    logger.info(f"  SOLDリストをスクレイピング中: {seller_id}")
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

            logger.info(f"  ページ {page}: {found}件 / 累計 {len(item_ids)}件 [タイトル: {title[:60]}]")

            if found == 0:
                ss_path = os.path.join(BASE_DIR, f"debug_{seller_id}_page{page}.png")
                try:
                    driver.save_screenshot(ss_path)
                    logger.warning(f"  ⚠ 0件 → スクリーンショット保存: {ss_path}")
                except Exception:
                    pass
                if page_src_snippet:
                    logger.warning(f"  ページソース冒頭: {page_src_snippet[:200]}")
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
        safe_quit(driver)

    return item_ids[:max_items]


# ---------------------------------------------------------------- GTIN取得（eBay側）
def get_gtin_from_api(item_id):
    """Browse API getItemByLegacyId でGTIN・タイトル・MPNを取得"""
    if not api_available():
        return "", "", "", False
    token = get_token()
    if not token:
        return "", "", "", False
    try:
        r = requests.get(
            "https://api.ebay.com/buy/browse/v1/item/get_item_by_legacy_id",
            headers={"Authorization": f"Bearer {token}"},
            params={"legacy_item_id": item_id},
            timeout=10,
        )
        if r.status_code == 429:
            api_cooldown()
            return "", "", "", False
        if not r.ok:
            return "", "", "", False
        data = r.json()
        gtin  = (data.get("gtin") or "").strip()
        title = (data.get("title") or "").strip()
        mpn   = (data.get("mpn") or "").strip()
        return gtin, title, mpn, True
    except Exception:
        return "", "", "", False


def get_gtin_from_item_page(item_id, driver):
    """SeleniumでeBayアイテムページからGTIN・タイトル・MPNを取得（リダイレクト検証付き）"""
    from selenium.webdriver.common.by import By

    try:
        driver.get(f"https://www.ebay.com/itm/{item_id}")
        time.sleep(2)

        if item_id not in driver.current_url:
            logger.info(f"    リダイレクト検出（終了済み/削除済み？）: {item_id}")
            return "", "", ""

        gtin, title, mpn = "", "", ""

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

        if not gtin:
            m = re.search(r'"gtin(?:13)?"\s*:\s*"(\d{8,14})"', driver.page_source)
            if m:
                gtin = m.group(1)

        if not title:
            page_title = driver.title
            if "|" in page_title:
                title = page_title.split("|")[0].strip()

        return gtin, title, mpn

    except Exception as e:
        logger.warning(f"    ⚠ ページ取得エラー ({item_id}): {e}")
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
    """eBay Browse APIでキーワード検索しJANを探す（従来のフォールバック）"""
    if not api_available():
        return ""
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
            api_cooldown()
            return ""
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
            logger.info(f"    (MPN '{mpn}' から JAN取得: {jan})")
            return jan
    for model in extract_model_from_title(title)[:3]:
        jan = search_jan_by_keyword(model)
        if jan:
            logger.info(f"    (型番 '{model}' から JAN取得: {jan})")
            return jan
    return ""


# ---------------------------------------------------------------- Amazon ASIN検索
def _norm_tokens(text):
    return set(re.findall(r"[a-z0-9]+", text.lower().replace("-", "")))


def title_similarity(ebay_title, amazon_title):
    """英数字トークンの重なり率で簡易類似度（0〜1）"""
    ta = _norm_tokens(ebay_title)
    tb = _norm_tokens(amazon_title)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / min(len(ta), len(tb))


def search_amazon_asin(keyword, ebay_title, driver, min_similarity):
    """
    Amazon.co.jpで型番/MPNを検索し、最も信頼できるASINを返す。
    照合条件: 検索キーワード（型番）がAmazonタイトルに含まれる、
              または eBayタイトルとの類似度 >= min_similarity
    戻り値: (asin, amazon_title) / 見つからなければ ("", "")
    """
    from selenium.webdriver.common.by import By

    if not amazon_available():
        return "", ""

    try:
        driver.get(f"https://www.amazon.co.jp/s?k={quote_plus(keyword)}")
        time.sleep(AMAZON_WAIT_SEC)

        src_lower = driver.page_source.lower()
        if "captcha" in src_lower or "robot check" in src_lower:
            amazon_cooldown()
            return "", ""

        kw_norm = keyword.lower().replace("-", "")
        best = ("", "", 0.0)  # (asin, title, score)

        results = driver.find_elements(
            By.CSS_SELECTOR, "div[data-component-type='s-search-result']")
        for res in results[:8]:
            asin = (res.get_attribute("data-asin") or "").strip()
            if not asin or not re.fullmatch(r"[A-Z0-9]{10}", asin):
                continue
            # スポンサー枠は誤マッチが多いのでスキップ
            try:
                if res.find_elements(By.CSS_SELECTOR, "[data-component-type='sp-sponsored-result']"):
                    continue
            except Exception:
                pass
            a_title = ""
            for sel in ("h2 a span", "h2 span"):
                try:
                    el = res.find_element(By.CSS_SELECTOR, sel)
                    a_title = el.text.strip()
                    if a_title:
                        break
                except Exception:
                    continue
            if not a_title:
                continue

            model_hit = kw_norm in a_title.lower().replace("-", "")
            sim = title_similarity(ebay_title, a_title)
            score = sim + (0.5 if model_hit else 0.0)

            if (model_hit or sim >= min_similarity) and score > best[2]:
                best = (asin, a_title, score)

        return best[0], best[1]

    except Exception as e:
        logger.warning(f"    ⚠ Amazon検索エラー ('{keyword}'): {e}")
        return "", ""


def find_asin(title, mpn, driver, min_similarity):
    """MPN → タイトル抽出型番の順でAmazonを検索しASINを特定"""
    keywords = []
    if mpn:
        keywords.append(mpn)
    keywords.extend(m for m in extract_model_from_title(title)[:3] if m != mpn)

    for kw in keywords:
        asin, a_title = search_amazon_asin(kw, title, driver, min_similarity)
        if asin:
            logger.info(f"    (Amazon検索 '{kw}' → ASIN: {asin})")
            return asin, a_title, kw
        if not amazon_available():
            break  # CAPTCHAクールダウン中は以降のキーワードも諦める
        time.sleep(1.5)
    return "", "", ""


# ---------------------------------------------------------------- ASIN記録CSV
ASIN_CSV_HEADER = ["seller_id", "item_id", "ebay_title", "keyword",
                   "asin", "amazon_title", "amazon_url"]


def append_asin_csv(seller_id, item_id, ebay_title, keyword, asin, amazon_title):
    safe = re.sub(r"[^A-Za-z0-9_\-]", "_", seller_id)
    path = os.path.join(BASE_DIR, f"asin_results_{safe}.csv")
    new_file = not os.path.exists(path)
    with open(path, "a", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(ASIN_CSV_HEADER)
        w.writerow([seller_id, item_id, ebay_title, keyword, asin, amazon_title,
                    f"https://www.amazon.co.jp/dp/{asin}"])
    return path


# ---------------------------------------------------------------- レジューム
def processed_path(seller_id):
    safe = re.sub(r"[^A-Za-z0-9_\-]", "_", seller_id)
    return os.path.join(BASE_DIR, f"processed_{safe}.txt")


def load_processed(seller_id):
    path = processed_path(seller_id)
    if not os.path.exists(path):
        return set()
    with open(path, encoding="utf-8") as f:
        return {line.strip() for line in f if line.strip()}


def mark_processed(seller_id, item_id):
    with open(processed_path(seller_id), "a", encoding="utf-8") as f:
        f.write(item_id + "\n")


# ---------------------------------------------------------------- メイン処理
def flush_codes(new_codes, account, args):
    """jan_research2.pyにJAN/ASIN混在リストを渡す。成功時のみTrueを返す。"""
    if not new_codes:
        return True
    code_list = sorted(new_codes)
    logger.info(f"\n  → {len(code_list)}件（JAN/ASIN）をリサーチに送信中...")
    cmd = [sys.executable, os.path.join(BASE_DIR, RESEARCH_SCRIPT),
           "--account", account] + code_list
    if args.dry_run:
        cmd.append("--dry-run")
    if args.force:
        cmd.append("--force")
    result = subprocess.run(cmd, cwd=BASE_DIR)
    if result.returncode != 0:
        logger.error(f"  ❌ {RESEARCH_SCRIPT} が終了コード {result.returncode} で失敗 → 後で再試行")
        return False
    logger.info("  → 送信完了\n")
    return True


def run_one_seller(seller_id, account, args, global_written):
    logger.info(f"\n{'='*60}")
    logger.info(f"セラー: {seller_id} / アカウント: {account} / 上限: {args.max_items}件")
    logger.info(f"{'='*60}")

    logger.info("SOLDリスト収集中...")
    item_ids = scrape_sold_items(seller_id, args.max_items, args.headless)
    logger.info(f"→ {len(item_ids)}件取得")

    if not item_ids:
        logger.error("❌ アイテムが取得できませんでした。")
        return 0

    processed = set() if args.no_resume else load_processed(seller_id)
    if processed:
        before = len(item_ids)
        item_ids = [i for i in item_ids if i not in processed]
        logger.info(f"  レジューム: 処理済み{before - len(item_ids)}件をスキップ → 残り{len(item_ids)}件")
        if not item_ids:
            logger.info("  すべて処理済みです。")
            return 0

    total = len(item_ids)
    use_api    = not args.no_api
    use_amazon = not args.no_amazon
    logger.info(f"JAN/ASIN収集中... (Amazon ASIN検索: {'ON' if use_amazon else 'OFF'})")

    code_set = set()       # JANとASINの混在
    written_codes = set()
    jan_count = 0
    asin_count = 0
    driver = None
    selenium_count = 0

    try:
        for i, item_id in enumerate(item_ids):
            gtin, title, mpn = "", "", ""

            # ① Browse API getItemByLegacyId（高速）
            if use_api:
                gtin, title, mpn, ok = get_gtin_from_api(item_id)
            else:
                ok = False

            # ② API失敗/休止中はSeleniumでページ取得
            if not ok:
                if driver is None:
                    driver = create_driver(args.headless)
                elif selenium_count > 0 and selenium_count % DRIVER_RESTART_EVERY == 0:
                    safe_quit(driver)
                    logger.info(f"  [ドライバー再起動 {i}/{total}]")
                    driver = None
                    try:
                        driver = create_driver(args.headless)
                    except Exception as e:
                        logger.error(f"  ❌ ドライバー再起動失敗: {e} → 30秒後にリトライ")
                        time.sleep(30)
                        driver = create_driver(args.headless)
                selenium_count += 1
                gtin, title, mpn = get_gtin_from_item_page(item_id, driver)

            # ③ eBay Browse API検索で補完（従来のフォールバック）
            if not gtin and (title or mpn):
                gtin = get_jan_from_title_or_mpn(title, mpn)

            # 日本製フィルタ（JANのみ対象）
            if gtin and not args.all_jan and not is_japan_jan(gtin):
                logger.info(f"  [{i+1}/{total}] {item_id} → 非日本製")
                gtin = ""
                title = ""  # 非日本製はASIN検索もしない
                mpn = ""

            code = gtin  # 渡す識別子（JAN優先）

            # ④ JAN未登録 → Amazon ASIN検索 → ASINをそのまま渡す
            if not code and use_amazon and (title or mpn):
                if driver is None:
                    driver = create_driver(args.headless)
                asin, a_title, kw = find_asin(title, mpn, driver, args.min_similarity)
                if asin:
                    code = asin
                    append_asin_csv(seller_id, item_id, title, kw, asin, a_title)

            # ⑤ 判定・記録
            if code:
                if code in global_written:
                    logger.info(f"  [{i+1}/{total}] {item_id} → {code}（書き込み済み）")
                else:
                    code_set.add(code)
                    if code == gtin:
                        jan_count += 1
                        logger.info(f"  [{i+1}/{total}] {item_id} → JAN {code} ✅")
                    else:
                        asin_count += 1
                        logger.info(f"  [{i+1}/{total}] {item_id} → ASIN {code} ✅")
            elif gtin == "" and not title and not mpn:
                pass  # 非日本製で除外済み（ログ出力済み）
            else:
                logger.info(f"  [{i+1}/{total}] {item_id} → JAN/ASIN未特定")

            if not args.no_resume:
                mark_processed(seller_id, item_id)

            if (i + 1) % BATCH_SIZE == 0:
                new_codes = code_set - written_codes
                if flush_codes(new_codes, account, args):
                    written_codes |= new_codes
                    global_written |= new_codes

    finally:
        safe_quit(driver)

    new_codes = code_set - written_codes
    if new_codes:
        logger.info(f"\n残り{len(new_codes)}件を送信中...")
        if flush_codes(new_codes, account, args):
            written_codes |= new_codes
            global_written |= new_codes
        else:
            fallback = os.path.join(BASE_DIR, f"failed_codes_{seller_id}.txt")
            with open(fallback, "a", encoding="utf-8") as f:
                f.write("\n".join(sorted(new_codes)) + "\n")
            logger.error(f"  ❌ 未送信のJAN/ASINを退避: {fallback}")
    elif not code_set:
        logger.error("❌ JAN/ASINが取得できませんでした。")

    logger.info(f"完了: {seller_id} → 合計 {len(code_set)}件 "
                f"(JAN {jan_count}件 / ASIN {asin_count}件)")
    return len(code_set)


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


def parse_args():
    parser = argparse.ArgumentParser(
        description="ライバルセラーのSOLD出品からJAN/ASINを収集しjan_research2.pyに連携")
    parser.add_argument("seller", nargs="?", help="セラーID")
    parser.add_argument("account", nargs="?", default="kozuki", help="アカウント名（デフォルト: kozuki）")
    parser.add_argument("--seller-list", metavar="FILE", help="セラーリストファイル（1行: セラーID[,アカウント]）")
    parser.add_argument("--headless", action="store_true", help="ブラウザ非表示（VPS用）")
    parser.add_argument("--dry-run", action="store_true", help="スプレッドシート書き込みをスキップ")
    parser.add_argument("--all-jan", action="store_true", help="日本製以外のJANも収集")
    parser.add_argument("--force", "--force-research", action="store_true", dest="force",
                        help="jan_research2.pyにTeapeakスキップを指定")
    parser.add_argument("--max-items", type=int, default=600, help="1セラーあたりの上限（デフォルト600）")
    parser.add_argument("--no-api", action="store_true", help="Browse API getItemを使わない")
    parser.add_argument("--no-resume", action="store_true", help="処理済みIDスキップを無効化")
    parser.add_argument("--no-amazon", action="store_true", help="Amazon ASIN検索をスキップ")
    parser.add_argument("--min-similarity", type=float, default=0.4,
                        help="eBay/Amazonタイトル照合しきい値 0〜1（デフォルト0.4）")
    args = parser.parse_args()

    if os.getenv("HEADLESS") == "1":
        args.headless = True
    if args.headless:
        args.force = True
    return args


def main():
    args = parse_args()
    global_written = set()

    if args.seller_list:
        if not os.path.exists(args.seller_list):
            logger.error(f"❌ セラーリストが見つかりません: {args.seller_list}")
            sys.exit(1)
        sellers = load_seller_list(args.seller_list)
        if not sellers:
            logger.error("❌ セラーリストが空です")
            sys.exit(1)

        logger.info(f"セラーリスト読み込み: {len(sellers)}件")
        total = 0
        for seller_id, account in sellers:
            try:
                total += run_one_seller(seller_id, account, args, global_written)
            except Exception as e:
                logger.exception(f"⚠ {seller_id} でエラー: {e}")
                continue

        logger.info(f"\n{'='*60}")
        logger.info(f"全セラー完了: JAN/ASIN合計 {total}件")
        return

    if not args.seller:
        logger.error("使い方: python3 rival_asin_research2.py <セラーID> [アカウント名]")
        logger.error("       python3 rival_asin_research2.py --seller-list rival_sellers.txt")
        sys.exit(1)

    run_one_seller(args.seller, args.account, args, global_written)


if __name__ == "__main__":
    main()