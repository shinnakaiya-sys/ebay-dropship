"""
mpn_research.py
===============
ライバルセラーの販売履歴からMPNを自動収集し、4ステップでリサーチを一括実行する。

=== Phase 1（--seller / --seller-list 指定時）: eBay販売履歴からMPN収集 ===
  1a. Selenium で販売済み商品のItem IDリストを収集
  1b. eBay Browse API で各商品のMPN・GTINを取得

=== Phase 2: MPNリサーチ（4ステップ）===
Step 1: Terapeak で過去30日の販売数確認
        → SOLD_THRESHOLD 未満なら ❌ 販売実績不足 としてスキップ
Step 2: Keepa でMPN→ASIN→Amazon.co.jp 仕入れ価格取得
        （/search でASIN照合 → /product で価格・重量取得）
Step 3: eBay Active Listings 最安値・URL取得（新品のみ）
Step 4: 利益計算 → GO/No-Go判定

使い方:
  # MPN直接指定
  python3 mpn_research.py --account kaworu ABC-1234 XYZ-9999

  # セラーIDからMPN自動収集 → リサーチ
  python3 mpn_research.py --seller akibashipping --account kaworu
  python3 mpn_research.py --seller akibashipping --max 100 --dry-run

  # セラーリストから一括処理（rival_sellers.txt形式: seller_id,account）
  python3 mpn_research.py --seller-list rival_sellers.txt --max 100

オプション:
  --account kozuki/kaworu/dbz  アカウント指定（省略時: kozuki）
  --seller <id_or_url>         セラーIDまたはURL（MPNを自動収集）
  --seller-list <file>         セラーリストファイル（seller_id,account 形式）
  --max N                      セラー1人あたりのSOLD収集上限（デフォルト: 50）
  --dry-run                    スプレッドシート書き込みをスキップ
  --force                      Step 1（Terapeak）をスキップ
  --sold-count N               --force 時の手動販売数指定
"""

import os
import re
import sys
import math
import time
import base64
import requests
from datetime import datetime
from dotenv import dotenv_values, load_dotenv
load_dotenv()
import gspread
from oauth2client.service_account import ServiceAccountCredentials
try:
    from terapeak_research import create_driver, do_research, extract_rows, set_ship_to_us
    TERAPEAK_AVAILABLE = True
except ImportError:
    TERAPEAK_AVAILABLE = False

# ==========================================
# 設定
# ==========================================
BASE_DIR       = os.path.dirname(os.path.abspath(__file__))
CLIENT_ID      = os.getenv("EBAY_CLIENT_ID") or os.getenv("EBAY_APP_ID", "")
CLIENT_SECRET  = os.getenv("EBAY_CLIENT_SECRET", "")
SPREADSHEET_ID = "1GEGnGQtb5Fb76W9Nyd5gGM-igQAe1-U9-W2nmhVjaB8"
TAB_NAME       = "MPN_リサーチ"
JSON_FILE      = os.path.join(BASE_DIR, "credentials.json")
SCOPE          = ["https://spreadsheets.google.com/feeds",
                  "https://www.googleapis.com/auth/drive"]

SOLD_THRESHOLD = 1      # Step 1: 最低販売数（これ未満はスキップ）
EBAY_FEE_RATE  = 0.15   # eBay手数料 15%
CUSTOMS_RATE   = 0.15   # 関税（仕入れ価格の15%）
MIN_PROFIT_JPY = 10     # GOと判定する最低利益ライン（円）

# eBayでよく入力される「意味のないMPN」— これらはスキップ
GENERIC_MPN = {
    "DOESNOTAPPLY", "NOTAPPLY", "NA", "NONE", "UNKNOWN", "GENERIC",
    "BLACK", "WHITE", "RED", "BLUE", "SILVER", "GOLD", "JAPAN", "ORIGINAL",
}

# ==========================================
# SpeedPAK Economy Japan 送料表（USA本土48州）
# 出典: .company/ebay-research/speedpak-economy-rates.md（2026年3月25日改定）
# ==========================================
_SPEEDPAK_US48 = [
    (0.1, 1227), (0.2, 1367), (0.3, 1581), (0.4, 1778), (0.5, 2060),
    (0.6, 2222), (0.7, 2321), (0.8, 2703), (0.9, 2820), (1.0, 3020),
    (1.1, 3136), (1.2, 3250), (1.3, 3366), (1.4, 3704), (1.5, 3816),
    (1.6, 3935), (1.7, 4046), (1.8, 4165), (1.9, 5056), (2.0, 5245),
    (2.5, 5582), (3.0, 6333), (3.5, 6958), (4.0, 7704), (4.5, 9135),
    (5.0, 11733), (5.5, 12500), (6.0, 13335), (6.5, 14160), (7.0, 15209),
    (7.5, 16058), (8.0, 16893), (8.5, 17562), (9.0, 18152), (9.5, 19106),
    (10.0, 19639), (10.5, 20276), (11.0, 20864), (11.5, 21565), (12.0, 22199),
    (12.5, 22887), (13.0, 23466), (13.5, 24054), (14.0, 24869), (14.5, 25200),
    (15.0, 25988), (15.5, 26656), (16.0, 28149), (16.5, 28775), (17.0, 29495),
    (17.5, 30196), (18.0, 30902), (18.5, 31478), (19.0, 32204), (19.5, 32936),
    (20.0, 33947), (20.5, 34655), (21.0, 35426), (21.5, 36145), (22.0, 36859),
    (22.5, 37602), (23.0, 38516), (23.5, 39084), (24.0, 39678), (24.5, 40374),
    (25.0, 40955),
]

US_CUSTOMS_CLEARANCE_FEE   = 245    # 米国輸入通関手数料（円/件）
US_CUSTOMS_PROCESSING_RATE = 0.021  # 米国関税処理手数料（関税額の2.1%）
DEFAULT_WEIGHT_KG          = 0.5    # 重量不明時のデフォルト（kg）

AMAZON_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ja-JP,ja;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


# ==========================================
# MPN バリデーション
# ==========================================
def _norm(s: str) -> str:
    """型番比較用の正規化（大文字化・空白/ハイフン等を除去）"""
    return re.sub(r"[\s\-_/.]", "", (s or "").upper())


def is_searchable_mpn(mpn: str) -> bool:
    """Keepa/Terapeak検索する価値のあるMPNか判定"""
    if not mpn or len(mpn) < 4:
        return False
    if mpn.isdigit():
        return False
    if _norm(mpn) in GENERIC_MPN:
        return False
    return True


# ==========================================
# SpeedPAK 送料取得
# ==========================================
def get_speedpak_rate_us48(weight_kg: float) -> int:
    """請求重量(kg)からSpeedPAK Economy USA本土48州の基本送料(JPY)を返す。"""
    weight_kg = math.ceil(weight_kg * 1000) / 1000
    for limit, price in _SPEEDPAK_US48:
        if weight_kg <= limit:
            return price
    return _SPEEDPAK_US48[-1][1]


# ==========================================
# Phase 1: eBay Browse API（MPN収集用）
# ==========================================
_browse_token_cache = {"value": None, "expires_at": 0}


def _get_browse_token() -> str | None:
    """eBay Browse API 用 OAuth トークンを取得（キャッシュ付き）"""
    now = time.time()
    if _browse_token_cache["value"] and now < _browse_token_cache["expires_at"] - 300:
        return _browse_token_cache["value"]
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
            _browse_token_cache["value"]      = data["access_token"]
            _browse_token_cache["expires_at"] = now + data.get("expires_in", 7200)
            return _browse_token_cache["value"]
        print(f"  ⚠ eBayトークン取得失敗 {r.status_code}: {r.text[:80]}")
    except Exception as e:
        print(f"  ⚠ eBayトークン取得エラー: {e}")
    return None


def _browse_get_item(item_id: str, retry: int = 3) -> dict | None:
    """Browse API でアイテム詳細を取得（429は指数バックオフ）"""
    token = _get_browse_token()
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
                print(f"  ⚠ Browse APIレートリミット。{wait}秒待機... ({attempt+1}/{retry})")
                time.sleep(wait)
                continue
            if r.status_code == 401:
                _browse_token_cache["value"] = None
                token = _get_browse_token()
                if not token:
                    return None
                continue
            if not r.ok:
                return None
            return r.json()
        except Exception as e:
            print(f"  ⚠ Browse APIエラー: {e}")
    return None


def _extract_mpn_from_item(data: dict) -> tuple[str, str]:
    """Browse APIレスポンスからMPNとGTINを抽出して返す。"""
    mpn = (data.get("mpn") or "").strip()
    if not mpn:
        for spec in data.get("localizedAspects", []):
            name = spec.get("name", "").upper()
            if name in ("MPN", "MANUFACTURER PART NUMBER", "PART NUMBER", "MODEL NUMBER"):
                mpn = spec.get("value", "").strip()
                break

    gtin = (data.get("gtin") or "").strip()
    if not gtin:
        for spec in data.get("localizedAspects", []):
            if spec.get("name", "").upper() in ("JAN", "EAN", "UPC", "GTIN"):
                gtin = spec.get("value", "").strip()
                break

    return mpn, gtin


def _scrape_sold_ids(seller_id_or_url: str, max_items: int) -> list[str]:
    """Selenium でセラーの販売済みItem IDリストを収集する。"""
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC

    if seller_id_or_url.startswith("http"):
        base_url = seller_id_or_url
        if "LH_Sold" not in base_url:
            sep = "&" if "?" in base_url else "?"
            base_url += f"{sep}LH_Sold=1&LH_Complete=1"
    else:
        base_url = (
            f"https://www.ebay.com/sch/i.html"
            f"?_nkw=&_armrs=1&_ssn={seller_id_or_url}&LH_Complete=1&LH_Sold=1&rt=nc"
        )

    item_ids = []
    seen     = set()
    page     = 1
    driver   = _create_ebay_driver()

    print(f"  SOLDリスト収集中: {seller_id_or_url}")
    try:
        while len(item_ids) < max_items:
            url = re.sub(r"[&?]_pgn=\d+", "", base_url)
            sep  = "&" if "?" in url else "?"
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
            all_hrefs  = set()
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

            print(f"  ページ {page}: {found}件 / 累計 {len(item_ids)}件 [{page_title[:55]}]")

            if found == 0:
                ss = os.path.join(BASE_DIR, f"debug_mpn_{seller_id_or_url[:20]}_p{page}.png")
                driver.save_screenshot(ss)
                print(f"  ⚠ 0件 → スクリーンショット保存: {ss}")
                break

            next_btn = None
            for sel in ["a.pagination__next",
                        "a[aria-label='Go to next search page']",
                        ".pagination__next"]:
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


def scrape_mpns_from_seller(seller_id_or_url: str, max_items: int = 50) -> list[str]:
    """
    セラーの販売履歴からMPNを収集して返す。
      Phase 1a: Selenium で Item ID リスト収集
      Phase 1b: Browse API で各アイテムの MPN を取得
    有効なMPN（重複除去済み）のリストを返す。
    """
    print(f"\n{'─'*55}")
    print(f"  [Phase 1a] Item ID収集: {seller_id_or_url}")
    print(f"{'─'*55}")

    item_ids = _scrape_sold_ids(seller_id_or_url, max_items)
    print(f"  → {len(item_ids)}件のItem IDを取得")

    if not item_ids:
        print("  ❌ Item IDが取得できませんでした。")
        return []

    print(f"\n  [Phase 1b] Browse APIでMPN取得中... ({len(item_ids)}件)")
    mpn_set   = {}   # mpn → (title, gtin) — 重複管理しつつ情報も保持
    total     = len(item_ids)

    for i, item_id in enumerate(item_ids, 1):
        data = _browse_get_item(item_id)
        if not data:
            print(f"  [{i}/{total}] {item_id} → API取得失敗")
            time.sleep(0.8)
            continue

        mpn, gtin = _extract_mpn_from_item(data)
        title     = data.get("title", "")[:50]

        if mpn and is_searchable_mpn(mpn):
            if mpn not in mpn_set:
                mpn_set[mpn] = (title, gtin)
            label = f"MPN:{mpn}"
            if gtin:
                label += f" / GTIN:{gtin}"
            print(f"  [{i}/{total}] {title[:40]}  → {label} ✅")
        else:
            reason = f"MPN:{mpn}" if mpn else "MPN未登録"
            print(f"  [{i}/{total}] {title[:40]}  → {reason} (スキップ)")

        time.sleep(0.8)

    unique_mpns = list(mpn_set.keys())
    print(f"\n  → MPN収集完了: {len(item_ids)}件中 {len(unique_mpns)}件の有効MPN")
    for mpn, (title, gtin) in mpn_set.items():
        gtin_note = f" (GTIN:{gtin})" if gtin else ""
        print(f"    {mpn}{gtin_note}  ← {title[:40]}")

    return unique_mpns


# ==========================================
# セラーリスト読み込み
# ==========================================
def load_seller_list(path: str) -> list[tuple[str, str]]:
    """rival_sellers.txt形式（seller_id[,account]）を読み込む。"""
    sellers = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts   = [p.strip() for p in line.split(",")]
            sid     = parts[0]
            account = parts[1] if len(parts) > 1 else "kozuki"
            sellers.append((sid, account))
    return sellers


# ==========================================
# Keepa API キー取得
# ==========================================
def _get_keepa_api_key() -> str | None:
    for env_file in [".env.kozuki", ".env.kaworu", ".env.dbz"]:
        env = dotenv_values(os.path.join(BASE_DIR, env_file))
        key = env.get("KEEPA_API_KEY", "")
        if key:
            return key
    return os.getenv("KEEPA_API_KEY")


# ==========================================
# 為替レート取得（USD→JPY）
# ==========================================
def get_usd_jpy_rate() -> float:
    try:
        resp = requests.get(
            "https://api.frankfurter.app/latest?from=USD&to=JPY",
            timeout=5
        )
        return float(resp.json()["rates"]["JPY"])
    except Exception:
        return 155.0


# ==========================================
# Keepa: ASIN から商品詳細取得（価格・重量）
# ==========================================
def _keepa_product_by_asin(asin: str, api_key: str) -> tuple[str | None, str | None, float | None]:
    """Keepa /product でASIN→(商品名, 価格文字列, 重量kg)を返す。"""
    try:
        resp = requests.get(
            "https://api.keepa.com/product",
            params={
                "key":     api_key,
                "domain":  5,
                "asin":    asin,
                "stats":   1,
                "history": 0,
            },
            timeout=15,
        )
        resp.raise_for_status()
        products = resp.json().get("products", [])
        if not products:
            return None, None, None

        p         = products[0]
        title     = p.get("title") or None
        current   = (p.get("stats") or {}).get("current") or []
        price_raw = -1
        for idx in [0, 1]:
            if len(current) > idx and current[idx] and current[idx] > 0:
                price_raw = current[idx]
                break
        price_str = f"¥{price_raw:,}" if price_raw > 0 else None

        weight_kg  = None
        pkg_weight = p.get("packageWeight")
        pkg_length = p.get("packageLength")
        pkg_width  = p.get("packageWidth")
        pkg_height = p.get("packageHeight")

        actual_kg = pkg_weight / 1000.0 if pkg_weight and pkg_weight > 0 else None
        vol_kg    = None
        if pkg_length and pkg_width and pkg_height and pkg_length > 0:
            vol_kg = (pkg_length / 10) * (pkg_width / 10) * (pkg_height / 10) / 8000

        if actual_kg is not None and vol_kg is not None:
            weight_kg = max(actual_kg, vol_kg)
        elif actual_kg is not None:
            weight_kg = actual_kg
        elif vol_kg is not None:
            weight_kg = vol_kg

        if weight_kg is not None:
            weight_kg = math.ceil(weight_kg * 1000) / 1000

        return title, price_str, weight_kg

    except Exception as e:
        print(f"  [Keepa/product] エラー: {e}")
        return None, None, None


# ==========================================
# Step 2: Keepa でMPN→商品情報取得
# ==========================================
def get_keepa_info_by_mpn(mpn: str, api_key: str) -> tuple[str | None, str | None, str | None, float | None, str]:
    """
    Keepa /search でMPN照合→ASIN取得し、/product で価格・重量を取得する。
    戻り値: (商品名, 価格文字列, Amazon URL, 重量kg, 照合ステータス)
    """
    try:
        resp = requests.get(
            "https://api.keepa.com/search",
            params={"key": api_key, "domain": 5, "type": "product", "term": mpn},
            timeout=15,
        )
        resp.raise_for_status()
        data     = resp.json()
        products = data.get("products") or []
        tokens   = data.get("tokensLeft", "?")
    except Exception as e:
        print(f"  [Keepa/search] エラー: {e}")
        return None, None, None, None, ""

    if not products:
        print(f"  — Keepa: MPN '{mpn}' → 該当なし  (残トークン: {tokens})")
        return None, None, None, None, ""

    q            = _norm(mpn)
    matched      = None
    match_status = "MPN不一致(要確認)"
    for p in products[:5]:
        pn    = _norm(p.get("partNumber", ""))
        model = _norm(p.get("model", ""))
        if q and (
            q == pn or q == model
            or (pn    and (q in pn    or pn    in q))
            or (model and (q in model or model in q))
        ):
            matched      = p
            match_status = "MPN一致"
            break

    product = matched or products[0]
    asin    = product.get("asin", "")
    if not asin:
        return None, None, None, None, ""

    print(f"  🔍 Keepa: MPN '{mpn}' → ASIN {asin} [{match_status}]  (残トークン: {tokens})")
    time.sleep(1)

    title, price_str, weight_kg = _keepa_product_by_asin(asin, api_key)
    url = f"https://www.amazon.co.jp/dp/{asin}"
    return title, price_str, url, weight_kg, match_status


# ==========================================
# Step 1: Terapeak で販売実績確認
# ==========================================
def get_terapeak_sold_count(mpn: str, driver) -> tuple[int, str, float]:
    """
    Terapeak（Seller Hub Research）でMPNキーワードを検索し、
    過去30日の販売数・代表タイトル・加重平均価格(USD)を返す。
    """
    print(f"           Teapeakキーワード: {mpn}")
    try:
        do_research(driver, keywords=mpn, days=30, min_price=0, category_id="")
        rows = extract_rows(driver)
        if not rows:
            return 0, "", 0.0

        first_title        = rows[0].get("タイトル", "")
        total              = 0
        weighted_sum       = 0.0
        total_sold_for_avg = 0
        for row in rows:
            sold_n = int(re.sub(r"[^\d]", "", row.get("総販売数", "0")) or 0)
            total += sold_n
            m = re.search(r'[\d.]+', row.get("平均販売価格(USD)", ""))
            if m and sold_n > 0:
                weighted_sum       += float(m.group()) * sold_n
                total_sold_for_avg += sold_n

        avg_usd = weighted_sum / total_sold_for_avg if total_sold_for_avg > 0 else 0.0
        return total, first_title, avg_usd
    except Exception as e:
        print(f"  [Terapeak] 検索エラー: {e}")
        return 0, "", 0.0


# ==========================================
# Step 3: eBay Active Listings 最安値取得
# ==========================================
def _ascii_keywords(title: str) -> str:
    """日本語タイトルからASCII英数字トークンを抽出してeBay検索用文字列を返す。"""
    tokens = re.findall(r"[A-Za-z0-9][A-Za-z0-9+\-\.]*", title)
    tokens = [t for t in tokens if len(t) >= 2]
    return " ".join(tokens[:8])


def _create_ebay_driver():
    import shutil
    from selenium import webdriver
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.chrome.options import Options

    headless = os.environ.get("HEADLESS", "0") == "1"
    options  = Options()

    if headless:
        options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
    else:
        profile_path = os.path.join(BASE_DIR, "ebay_session")
        options.add_argument(f"--user-data-dir={profile_path}")

    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--lang=en-US")
    options.add_argument("--window-size=1920,1080")
    options.add_argument(
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
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


def _ebay_search_lowest(driver, keyword: str) -> tuple[float, str]:
    """eBay検索ページから新品最安値(USD)とURLをSeleniumで取得。"""
    from selenium.webdriver.common.by import By
    from urllib.parse import quote_plus
    url = (f"https://www.ebay.com/sch/i.html"
           f"?_nkw={quote_plus(keyword)}&_sacat=0"
           f"&LH_BIN=1&LH_ItemCondition=1000&LH_PrefLoc=2&_sop=15")
    try:
        driver.get(url)
        time.sleep(4)

        items = driver.find_elements(By.CSS_SELECTOR, "ul.srp-results li")
        if not items:
            print(f"           [eBay] 検索結果なし（キーワード: {keyword[:40]}）")
            return 0.0, ""

        for item in items:
            links = item.find_elements(By.CSS_SELECTOR, "a[href*='/itm/']")
            if not links:
                continue
            href = links[0].get_attribute("href") or ""
            if not re.search(r"/itm/(?:[^/?#]+/)?(\d{9,})", href):
                continue

            price = 0.0
            for sel in ["[class*='s-card__price']", "span.s-item__price"]:
                for pel in item.find_elements(By.CSS_SELECTOR, sel):
                    m = re.search(r'\$([0-9,]+\.?\d*)', pel.text)
                    if m:
                        price = float(m.group(1).replace(",", ""))
                        break
                if price > 0:
                    break
            if price <= 0:
                continue

            return price, href.split("?")[0]

    except Exception as e:
        print(f"  [eBay Selenium] エラー: {e}")
    return 0.0, ""


def get_ebay_active_lowest(mpn: str, ebay_driver, title_kw: str = "") -> tuple[float, str]:
    """
    新品かつ最安値のeBay出品価格(USD)と出品URLを取得。
    検索順: ① MPN → ② 商品名英数字キーワード
    """
    if ebay_driver is None:
        return 0.0, ""

    if mpn:
        price, url = _ebay_search_lowest(ebay_driver, mpn)
        if price:
            return price, url

    if title_kw:
        price, url = _ebay_search_lowest(ebay_driver, title_kw)
        if price:
            print(f"           (商品名キーワード検索: '{title_kw}')")
            return price, url

    return 0.0, ""


# ==========================================
# Step 4: 利益計算
# ==========================================
def parse_jpy(price_str: str) -> int:
    """'¥6,645' → 6645"""
    if not price_str:
        return 0
    return int(re.sub(r"[^\d]", "", price_str) or 0)


def calc_profit(ebay_usd: float, amazon_jpy: int, rate: float,
                weight_kg: float | None = None) -> dict:
    if weight_kg is None or weight_kg <= 0:
        weight_kg = DEFAULT_WEIGHT_KG

    revenue_jpy    = ebay_usd * rate
    ebay_fee_jpy   = revenue_jpy * EBAY_FEE_RATE
    customs_jpy    = amazon_jpy * CUSTOMS_RATE
    base_shipping  = get_speedpak_rate_us48(weight_kg)
    us_processing  = round(customs_jpy * US_CUSTOMS_PROCESSING_RATE)
    total_shipping = base_shipping + US_CUSTOMS_CLEARANCE_FEE + us_processing
    profit_jpy     = revenue_jpy - amazon_jpy - ebay_fee_jpy - customs_jpy - total_shipping

    return {
        "revenue_jpy":   round(revenue_jpy),
        "ebay_fee_jpy":  round(ebay_fee_jpy),
        "customs_jpy":   round(customs_jpy),
        "weight_kg":     weight_kg,
        "base_shipping": base_shipping,
        "us_clearance":  US_CUSTOMS_CLEARANCE_FEE,
        "us_processing": us_processing,
        "shipping_jpy":  total_shipping,
        "profit_jpy":    round(profit_jpy),
        "is_go":         profit_jpy >= MIN_PROFIT_JPY,
    }


# ==========================================
# スプレッドシート書き込み
# ==========================================
def write_to_sheet(ws, mpn: str, result: dict, dry_run: bool):
    """
    「MPN_リサーチ」タブに1行追記する。
    列構成（A〜U）:
      A=MPN, B=商品名, C=タイトル(eBay), D=カテゴリ, E=コンディション,
      F=販売価格(USD), G=送料, H=返品, I=仕入れ先, J=仕入れ価格(JPY),
      K=月間Sold数, L=判定, M=Keepa照合, N=請求重量(kg), O=送料(JPY),
      P=利益(JPY), Q=還付金額(JPY), R=アイテムスペシフィクス,
      S=作成日, T=ステータス, U=仕入れ先URL
    """
    today      = datetime.now().strftime("%Y-%m-%d")
    judgment   = "✅ GO" if result["is_go"] else "❌ No-Go"
    ebay_usd   = f"${result['ebay_usd']:.2f}" if result["ebay_usd"] else ""
    profit     = result.get("profit") or {}
    amazon_jpy = parse_jpy(result.get("amazon_price_str", ""))
    refund_jpy = round(amazon_jpy * 0.1)

    row_data = [
        mpn,                                        # A: MPN
        result.get("amazon_title", ""),             # B: 商品名
        "",                                         # C: タイトル(eBay)
        "",                                         # D: カテゴリ
        "New",                                      # E: コンディション
        ebay_usd,                                   # F: 販売価格(USD)
        "Free Shipping",                            # G: 送料
        "30 Days Returns",                          # H: 返品
        "Amazon.co.jp",                             # I: 仕入れ先
        result.get("amazon_price_str", ""),         # J: 仕入れ価格(JPY)
        result.get("sold_count", ""),               # K: 月間Sold数
        judgment,                                   # L: 判定
        result.get("keepa_match", ""),              # M: Keepa照合
        profit.get("weight_kg", ""),                # N: 請求重量(kg)
        profit.get("shipping_jpy", ""),             # O: 送料(JPY)
        profit.get("profit_jpy", ""),               # P: 利益(JPY)
        refund_jpy if amazon_jpy else "",           # Q: 還付金額（仕入れの10%）
        "",                                         # R: アイテムスペシフィクス
        today,                                      # S: 作成日
        "リサーチ完了",                               # T: ステータス
        result.get("amazon_url", ""),               # U: 仕入れ先URL
    ]

    if dry_run:
        print(f"  [DRY-RUN] 書き込み予定: MPN={mpn} | 判定={judgment} | "
              f"利益=¥{profit.get('profit_jpy', 0):,}")
        return

    next_row = len(ws.get_all_values()) + 1
    if next_row > ws.row_count:
        ws.add_rows(100)
    ws.update([row_data], f'A{next_row}', value_input_option='USER_ENTERED')
    time.sleep(0.5)


# ==========================================
# Phase 2: 1件のMPNをリサーチ
# ==========================================
def research_one(mpn: str, rate: float, ws, dry_run: bool,
                 account: str = "kozuki", force: bool = False,
                 manual_sold: int = 0, driver=None, ebay_driver=None) -> dict:
    print(f"\n{'─'*55}")
    print(f"  MPN: {mpn}")
    print(f"{'─'*55}")

    if not is_searchable_mpn(mpn):
        print(f"  → ⚠️  汎用/無効なMPN → スキップ")
        return {"status": "skipped", "reason": "汎用/無効なMPN"}

    # ── Keepa: MPN→商品情報取得 ────────────────────────
    keepa_key    = _get_keepa_api_key()
    amazon_title = amazon_price_str = amazon_url = weight_kg = None
    keepa_match  = ""
    if keepa_key:
        print("  [Keepa] MPN検索中...")
        amazon_title, amazon_price_str, amazon_url, weight_kg, keepa_match = \
            get_keepa_info_by_mpn(mpn, keepa_key)
        if amazon_title:
            print(f"          商品名: {amazon_title[:60]}")
        if amazon_price_str:
            print(f"          価格 : {amazon_price_str}")
        if keepa_match:
            print(f"          照合 : {keepa_match}")
        if weight_kg:
            print(f"          請求重量: {weight_kg:.3f} kg")
        else:
            print(f"          請求重量: 不明 → デフォルト {DEFAULT_WEIGHT_KG} kg を使用")
    else:
        print("  ⚠️  KEEPA_API_KEY未設定")

    # ── Step 1: Terapeak 販売実績確認 ────────────────────
    terapeak_avg_usd = 0.0
    ebay_title       = ""
    if force:
        sold_count = manual_sold if manual_sold > 0 else SOLD_THRESHOLD
        print(f"  [Step 1] ⏭  スキップ（--force 指定）販売数: {sold_count}個として処理")
    else:
        print("  [Step 1] Terapeak販売実績確認（過去30日・MPNキーワード検索）...")
        sold_count, ebay_title, terapeak_avg_usd = get_terapeak_sold_count(mpn, driver)
        print(f"           販売数: {sold_count}個")
        if terapeak_avg_usd:
            print(f"           Terapeak平均販売価格: ${terapeak_avg_usd:.2f}")

        if sold_count < SOLD_THRESHOLD:
            print(f"  → ❌ 販売実績不足（{sold_count}個 < {SOLD_THRESHOLD}個）スキップ")
            return {"status": "skipped", "reason": "販売実績不足", "sold_count": sold_count}

    print(f"  → ✅ 販売実績OK（{sold_count}個）")

    # ── Step 2: 仕入れ価格確認 ───────────────────────────
    print("  [Step 2] 仕入れ価格確認（Keepa）...")
    if not amazon_price_str:
        print("  → ❌ Keepa価格取得失敗。スキップ")
        return {"status": "skipped", "reason": "Keepa価格取得失敗"}

    amazon_jpy = parse_jpy(amazon_price_str)
    print(f"           商品名: {(amazon_title or '(不明)')[:50]}")
    print(f"           価格: {amazon_price_str}")

    # ── Step 3: eBay Active Listings 最安値取得 ───────────
    print("  [Step 3] eBay最安値（Active Listings・新品）取得...")
    ebay_usd, ebay_url = get_ebay_active_lowest(
        mpn, ebay_driver, title_kw=_ascii_keywords(amazon_title or ebay_title or ""))

    price_source = "eBay Active"
    if ebay_usd:
        print(f"           最安値: ${ebay_usd:.2f}")
        print(f"           URL: {ebay_url[:70] if ebay_url else '(なし)'}")
    elif terapeak_avg_usd:
        ebay_usd     = terapeak_avg_usd
        ebay_url     = ""
        price_source = "Terapeak販売履歴(平均)"
        print(f"           → eBay出品なし。Terapeak平均価格で利益計算: ${ebay_usd:.2f}")
    else:
        print("           → eBay出品なし・Terapeak価格も取得不可")

    # ── Step 4: 利益計算 ──────────────────────────────────
    print(f"  [Step 4] 利益計算... (レート: ¥{rate:.1f}/USD, 価格ソース: {price_source})")
    profit      = calc_profit(ebay_usd, amazon_jpy, rate, weight_kg)
    used_weight = profit["weight_kg"]

    print(f"           売上   : ¥{profit['revenue_jpy']:>8,}")
    print(f"           仕入れ : ¥{amazon_jpy:>8,}  (−)")
    print(f"           eBay手数料: ¥{profit['ebay_fee_jpy']:>6,}  (−)")
    print(f"           関税   : ¥{profit['customs_jpy']:>8,}  (−)")
    print(f"           送料   : ¥{profit['shipping_jpy']:>8,}  (−)  "
          f"[{used_weight:.3f}kg: 基本¥{profit['base_shipping']:,} + "
          f"通関¥{profit['us_clearance']} + 関税処理¥{profit['us_processing']}]")
    print(f"           {'─'*30}")
    judgment = "✅ GO" if profit["is_go"] else "❌ No-Go"
    print(f"           利益   : ¥{profit['profit_jpy']:>8,}  → {judgment}")

    SHIPPING_LIMIT_JPY = 5000
    if profit["shipping_jpy"] >= SHIPPING_LIMIT_JPY:
        print(f"  → ❌ 送料上限超過（¥{profit['shipping_jpy']:,} ≥ ¥{SHIPPING_LIMIT_JPY:,}）スキップ")
        return {"status": "skipped", "reason": f"送料上限超過（¥{profit['shipping_jpy']:,}）"}

    result = {
        "status":           "processed",
        "sold_count":       sold_count,
        "amazon_title":     amazon_title or ebay_title or mpn,
        "amazon_price_str": amazon_price_str,
        "amazon_url":       amazon_url or "",
        "keepa_match":      keepa_match,
        "ebay_usd":         ebay_usd,
        "ebay_url":         ebay_url,
        "profit":           profit,
        "is_go":            profit["is_go"],
    }

    write_to_sheet(ws, mpn, result, dry_run)
    return result


# ==========================================
# Phase 2: 全MPNをリサーチ（ドライバー管理含む）
# ==========================================
def run_research(mpn_codes: list[str], rate: float, ws, dry_run: bool,
                 account: str, force: bool, manual_sold: int) -> dict:
    """MPNリストに対してリサーチを実行し、サマリー辞書を返す。"""

    # Terapeak ドライバー起動
    t_driver = None
    if not force and not TERAPEAK_AVAILABLE:
        print("[Phase 2] Terapeak未インストール → --force モードで続行")
        force = True
    elif not force:
        print("[Phase 2] Teapeakドライバー起動中...")
        try:
            t_driver = create_driver()
            set_ship_to_us(t_driver)
            print("  ✅ Teapeakドライバー起動完了")
        except Exception as e:
            print(f"  ⚠️  Teapeakドライバー起動失敗: {e}")
            raise

    # eBay検索ドライバー起動
    ebay_driver = None
    print("[Phase 2] eBay検索ドライバー起動中...")
    try:
        ebay_driver = _create_ebay_driver()
        print("  ✅ eBay検索ドライバー起動完了")
    except Exception as e:
        print(f"  ⚠️  eBay検索ドライバー起動失敗: {e}")
        print("     Step 3をスキップします")

    summary = {"go": [], "no_go": [], "skipped": []}
    try:
        for mpn in mpn_codes:
            mpn = mpn.strip()
            result = research_one(mpn, rate, ws, dry_run, account=account,
                                  force=force, manual_sold=manual_sold,
                                  driver=t_driver, ebay_driver=ebay_driver)

            if result["status"] == "skipped":
                summary["skipped"].append(mpn)
            elif result.get("is_go"):
                summary["go"].append(mpn)
            else:
                summary["no_go"].append(mpn)

            time.sleep(1)
    finally:
        if t_driver:
            t_driver.quit()
            print("\n  Teapeakドライバーを終了しました。")
        if ebay_driver:
            try:
                ebay_driver.quit()
            except Exception:
                pass
            print("  eBay検索ドライバーを終了しました。")

    return summary


# ==========================================
# メイン
# ==========================================
def main():
    args    = sys.argv[1:]
    dry_run = "--dry-run" in args
    force   = "--force"   in args

    # --account 解析
    account = "kozuki"
    for i, arg in enumerate(args):
        if arg == "--account" and i + 1 < len(args):
            account = args[i + 1]; break
        if arg.startswith("--account="):
            account = arg.split("=", 1)[1]; break

    valid_accounts = {"kozuki", "kaworu", "dbz"}
    if account not in valid_accounts:
        print(f"❌ 無効なアカウント名: {account}（kozuki / kaworu / dbz）")
        sys.exit(1)

    # --sold-count 解析
    manual_sold = 0
    for i, arg in enumerate(args):
        if arg == "--sold-count" and i + 1 < len(args):
            try: manual_sold = int(args[i + 1])
            except ValueError: pass
            break
        if arg.startswith("--sold-count="):
            try: manual_sold = int(arg.split("=", 1)[1])
            except ValueError: pass
            break

    # --max 解析
    max_items = 50
    for i, arg in enumerate(args):
        if arg == "--max" and i + 1 < len(args):
            try: max_items = int(args[i + 1])
            except ValueError: pass
            break
        if arg.startswith("--max="):
            try: max_items = int(arg.split("=", 1)[1])
            except ValueError: pass
            break

    # --seller 解析
    seller_id = None
    for i, arg in enumerate(args):
        if arg == "--seller" and i + 1 < len(args):
            seller_id = args[i + 1]; break
        if arg.startswith("--seller="):
            seller_id = arg.split("=", 1)[1]; break

    # --seller-list 解析
    seller_list_file = None
    for i, arg in enumerate(args):
        if arg == "--seller-list" and i + 1 < len(args):
            seller_list_file = args[i + 1]; break
        if arg.startswith("--seller-list="):
            seller_list_file = arg.split("=", 1)[1]; break

    # MPN直接指定（--で始まらないもの、かつ seller/account/max/sold-count の値以外）
    skip_args = {"--account", "--sold-count", "--max", "--seller", "--seller-list"}
    skip_next = False
    mpn_codes = []
    for arg in args:
        if skip_next:
            skip_next = False
            continue
        if arg in skip_args:
            skip_next = True
            continue
        if arg.startswith("--"):
            continue
        mpn_codes.append(arg)

    # ── 入力検証 ──────────────────────────────────────────
    mode = "[DRY-RUN]" if dry_run else "[本番]"
    if not seller_id and not seller_list_file and not mpn_codes:
        print("使い方:")
        print("  # MPN直接指定")
        print("  python3 mpn_research.py --account kaworu ABC-1234 XYZ-9999")
        print()
        print("  # セラーIDからMPN自動収集")
        print("  python3 mpn_research.py --seller akibashipping --account kaworu --max 100")
        print()
        print("  # セラーリストから一括処理")
        print("  python3 mpn_research.py --seller-list rival_sellers.txt --max 100")
        sys.exit(1)

    # ── 共通初期化 ────────────────────────────────────────
    print(f"\n{'='*55}")
    print(f"  MPN リサーチ {mode}")
    print(f"  アカウント: {account}")
    if force:
        print(f"  Step1スキップ: ON（--force）")
    print(f"{'='*55}")

    print("[初期化] 為替レート取得中...")
    rate = get_usd_jpy_rate()
    print(f"  ✅ USD/JPY: ¥{rate:.1f}")

    print("[初期化] スプレッドシート接続中...")
    creds  = ServiceAccountCredentials.from_json_keyfile_name(JSON_FILE, SCOPE)
    client = gspread.authorize(creds)
    sh     = client.open_by_key(SPREADSHEET_ID)
    try:
        ws = sh.worksheet(TAB_NAME)
    except gspread.exceptions.WorksheetNotFound:
        ws = sh.add_worksheet(title=TAB_NAME, rows=500, cols=21)
        header = ["MPN", "商品名", "タイトル(eBay)", "カテゴリ", "コンディション",
                  "販売価格(USD)", "送料", "返品", "仕入れ先", "仕入れ価格(JPY)",
                  "月間Sold数", "判定", "Keepa照合", "請求重量(kg)", "送料(JPY)",
                  "利益(JPY)", "還付金額(JPY)", "アイテムスペシフィクス",
                  "作成日", "ステータス", "仕入れ先URL"]
        ws.update([header], "A1")
    print("  ✅ 接続成功")

    total_summary = {"go": [], "no_go": [], "skipped": []}

    def _merge(s):
        for k in total_summary:
            total_summary[k].extend(s.get(k, []))

    # ── モード分岐 ────────────────────────────────────────

    # ① セラーリスト一括
    if seller_list_file:
        if not os.path.exists(seller_list_file):
            print(f"❌ セラーリストが見つかりません: {seller_list_file}")
            sys.exit(1)
        sellers = load_seller_list(seller_list_file)
        if not sellers:
            print("❌ セラーリストが空です")
            sys.exit(1)
        print(f"[セラーリスト] {len(sellers)}件読み込み")

        for sid, acct in sellers:
            print(f"\n{'='*55}")
            print(f"  セラー: {sid} / アカウント: {acct}")
            print(f"{'='*55}")
            try:
                mpns = scrape_mpns_from_seller(sid, max_items)
                if mpns:
                    s = run_research(mpns, rate, ws, dry_run, acct, force, manual_sold)
                    _merge(s)
            except Exception as e:
                print(f"  ⚠️  {sid} でエラー: {e}")
                continue

    # ② 単一セラー指定
    elif seller_id:
        print(f"[セラー指定] {seller_id}  (上限: {max_items}件)")
        mpns = scrape_mpns_from_seller(seller_id, max_items)
        if not mpns:
            print("❌ 有効なMPNが取得できませんでした。")
            sys.exit(1)
        print(f"\n[Phase 2] {len(mpns)}件のMPNをリサーチします")
        s = run_research(mpns, rate, ws, dry_run, account, force, manual_sold)
        _merge(s)

    # ③ MPN直接指定
    else:
        print(f"[直接指定] {len(mpn_codes)}件のMPN")
        s = run_research(mpn_codes, rate, ws, dry_run, account, force, manual_sold)
        _merge(s)

    # ── サマリー ──────────────────────────────────────────
    print(f"\n{'='*55}")
    print(f"  完了サマリー")
    print(f"{'='*55}")
    print(f"  ✅ GO      : {len(total_summary['go'])}件  {total_summary['go']}")
    print(f"  ❌ No-Go   : {len(total_summary['no_go'])}件  {total_summary['no_go']}")
    print(f"  ⏭  スキップ : {len(total_summary['skipped'])}件  {total_summary['skipped']}")
    if not dry_run and (total_summary["go"] or total_summary["no_go"]):
        print(f"\n  スプレッドシート「{TAB_NAME}」タブに書き込みました。")
    print(f"{'='*55}\n")


if __name__ == "__main__":
    main()
