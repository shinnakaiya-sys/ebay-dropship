"""
jan_research.py
===============
JANコードから4ステップでリサーチする。

Step 1: eBay Sold Items 確認（過去30日）
        → 3個未満なら ❌ 販売実績不足 としてスキップ
Step 2: Amazon.co.jp 仕入れ価格取得
Step 3: eBay Active Listings 最安値・URL取得（新品のみ）
Step 4: 利益計算 → GO/No-Go判定

利益計算式:
  利益(JPY) = eBay最安値(USD) × 為替レート
              - 仕入れ価格(JPY)
              - eBay手数料(販売額の15%)
              - 関税(仕入れ価格の15%)
              - 国際送料(デフォルト: 3,000円)

使い方:
  python3 jan_research.py --account kozuki 4901777321991
  python3 jan_research.py --account kaworu 4901777321991 4902370548501
  python3 jan_research.py --account dbz --dry-run 4901777321991

アカウント指定（--account）省略時は kozuki を使用
"""

import os
import sys
import re
import time
import base64
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timedelta, timezone
from dotenv import dotenv_values, load_dotenv
load_dotenv()
import gspread
from google.oauth2.service_account import Credentials
try:
    from terapeak_research import create_driver, do_research, extract_rows, set_ship_to_us
    TERAPEAK_AVAILABLE = True
except ImportError:
    TERAPEAK_AVAILABLE = False

# ==========================================
# 設定
# ==========================================
CLIENT_ID      = os.getenv("EBAY_CLIENT_ID") or os.getenv("EBAY_APP_ID", "")
CLIENT_SECRET  = os.getenv("EBAY_CLIENT_SECRET", "")
SPREADSHEET_ID = "1GEGnGQtb5Fb76W9Nyd5gGM-igQAe1-U9-W2nmhVjaB8"
TAB_NAME       = "新品リサーチ"
JSON_FILE      = "credentials.json"
SCOPE          = ["https://spreadsheets.google.com/feeds",
                  "https://www.googleapis.com/auth/drive"]

SOLD_THRESHOLD = 1     # Step 1: 最低販売数（これ未満はスキップ）
EBAY_FEE_RATE  = 0.15   # eBay手数料 17%
CUSTOMS_RATE   = 0.15   # 関税（仕入れ価格の15%）
MIN_PROFIT_JPY = 10    # GOと判定する最低利益ライン（円）

# ==========================================
# SpeedPAK Economy Japan 送料表（USA本土48州）
# 出典: Orange Connex RATE GUIDE of eBay SpeedPAK Economy-JP（2026年7月30日改定）
# ==========================================
_SPEEDPAK_US48 = [
    (0.1, 1157), (0.2, 1289), (0.3, 1491), (0.4, 1677), (0.5, 1943),
    (0.6, 2096), (0.7, 2189), (0.8, 2549), (0.9, 2660), (1.0, 2848),
    (1.1, 2958), (1.2, 3065), (1.3, 3174), (1.4, 3493), (1.5, 3599),
    (1.6, 3711), (1.7, 3816), (1.8, 3928), (1.9, 4768), (2.0, 4947),
    (2.5, 5264), (3.0, 5973), (3.5, 6562), (4.0, 7266), (4.5, 8615),
    (5.0, 11065), (5.5, 11789), (6.0, 12576), (6.5, 13354), (7.0, 14344),
    (7.5, 15144), (8.0, 15932), (8.5, 16563), (9.0, 17119), (9.5, 18019),
    (10.0, 18522), (10.5, 19122), (11.0, 19677), (11.5, 20338), (12.0, 20936),
    (12.5, 21585), (13.0, 22131), (13.5, 22685), (14.0, 23454), (14.5, 23766),
    (15.0, 24509), (15.5, 25139), (16.0, 26547), (16.5, 27138), (17.0, 27817),
    (17.5, 28478), (18.0, 29144), (18.5, 29687), (19.0, 30372), (19.5, 31062),
    (20.0, 32016), (20.5, 32683), (21.0, 33410), (21.5, 34089), (22.0, 34762),
    (22.5, 35463), (23.0, 36325), (23.5, 36860), (24.0, 37421), (24.5, 38077),
    (25.0, 38625),
]

US_CUSTOMS_CLEARANCE_FEE   = 245    # 米国輸入通関手数料（円/件）
US_CUSTOMS_PROCESSING_RATE = 0.021  # 米国関税処理手数料（関税額の2.1%）
DEFAULT_WEIGHT_KG          = 0.5    # 重量不明時のデフォルト（kg）


def get_speedpak_rate_us48(weight_kg: float) -> int:
    """請求重量(kg)からSpeedPAK Economy USA本土48州の基本送料(JPY)を返す。"""
    import math
    weight_kg = math.ceil(weight_kg * 1000) / 1000  # グラム単位で切り上げ
    for limit, price in _SPEEDPAK_US48:
        if weight_kg <= limit:
            return price
    return _SPEEDPAK_US48[-1][1]  # 25kg超は最大料金

AMAZON_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ja-JP,ja;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


def _get_keepa_api_key() -> str | None:
    """各.envファイルからKEEPA_API_KEYを取得"""
    for env_file in [".env.kozuki", ".env.kaworu", ".env.dbz"]:
        env = dotenv_values(env_file)
        key = env.get("KEEPA_API_KEY", "")
        if key:
            return key
    return os.getenv("KEEPA_API_KEY")


# ==========================================
# eBay OAuth トークン取得
# ==========================================
def get_ebay_token() -> str | None:
    url = "https://api.ebay.com/identity/v1/oauth2/token"
    auth_str = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Authorization": f"Basic {auth_str}",
    }
    data = {
        "grant_type": "client_credentials",
        "scope": "https://api.ebay.com/oauth/api_scope",
    }
    try:
        resp = requests.post(url, headers=headers, data=data, timeout=10)
        return resp.json().get("access_token")
    except Exception as e:
        print(f"  [トークン] 取得失敗: {e}")
        return None


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
    except:
        return 155.0  # API失敗時のフォールバック


# ==========================================
# Step 1: Terapeak で過去30日の販売数確認
# ==========================================
def get_terapeak_sold_count(jan: str, _unused: str, driver) -> tuple[int, str, float]:
    """
    Terapeak（Seller Hub Research）で過去30日の販売数・代表タイトル・
    販売数加重平均価格(USD)を返す。
    JANコードで検索し、複数行ヒットした場合は総販売数を合計して返す。
    """
    print(f"           Teapeakキーワード: {jan}")

    try:
        do_research(driver, keywords=jan, days=30, min_price=0, category_id="")
        rows = extract_rows(driver)
        if not rows:
            return 0, "", 0.0
        total = 0
        first_title = rows[0].get("タイトル", "")
        weighted_sum = 0.0
        total_sold_for_avg = 0
        for row in rows:
            sold_str = row.get("総販売数", "0")
            sold_n = int(re.sub(r"[^\d]", "", sold_str) or 0)
            total += sold_n
            price_str = row.get("平均販売価格(USD)", "")
            m = re.search(r'[\d.]+', price_str)
            if m and sold_n > 0:
                weighted_sum += float(m.group()) * sold_n
                total_sold_for_avg += sold_n
        avg_usd = weighted_sum / total_sold_for_avg if total_sold_for_avg > 0 else 0.0
        return total, first_title, avg_usd
    except Exception as e:
        print(f"  [Terapeak] 検索エラー: {e}")
        return 0, "", 0.0


# ==========================================
# Step 2: Amazon.co.jp 仕入れ価格取得
# ==========================================
def get_amazon_info(jan: str) -> tuple[str | None, str | None, str | None]:
    """(商品名, 価格文字列, URL) を返す。取得失敗時は (None, None, None)。"""
    search_url = f"https://www.amazon.co.jp/s?k={jan}"
    try:
        resp = requests.get(search_url, headers=AMAZON_HEADERS, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        print(f"  [Amazon] リクエスト失敗: {e}")
        return None, None, None

    soup    = BeautifulSoup(resp.text, "html.parser")
    product = soup.find("div", {"data-component-type": "s-search-result"})
    if not product:
        return None, None, None

    # タイトル
    title = ""
    for sel in [
        {"class": "a-text-normal"},
        {"class": "a-size-medium a-color-base a-text-normal"},
        {"class": "a-size-base-plus a-color-base a-text-normal"},
    ]:
        tag = product.find("span", sel)
        if tag and tag.text.strip():
            title = tag.text.strip()
            break

    # 価格
    price_whole = product.find("span", class_="a-price-whole")
    price_str   = ""
    if price_whole:
        digits = re.sub(r"[^\d]", "", price_whole.text)
        if digits:
            price_str = f"¥{int(digits):,}"

    # 商品URL（ASIN）
    link        = product.find("a", class_="a-link-normal s-no-outline")
    product_url = ""
    if link:
        href       = link.get("href", "")
        asin_match = re.search(r"/dp/([A-Z0-9]{10})", href)
        if asin_match:
            product_url = f"https://www.amazon.co.jp/dp/{asin_match.group(1)}"
        elif href:
            product_url = "https://www.amazon.co.jp" + href.split("?")[0]

    # 検索結果に価格がない場合は商品ページから再取得
    if not price_str and product_url:
        price_str = _get_price_from_product_page(product_url)

    return title or None, price_str or None, product_url or None


def _get_price_from_product_page(url: str) -> str:
    try:
        time.sleep(1)
        resp        = requests.get(url, headers=AMAZON_HEADERS, timeout=15)
        soup        = BeautifulSoup(resp.text, "html.parser")
        price_whole = soup.find("span", class_="a-price-whole")
        if price_whole:
            digits = re.sub(r"[^\d]", "", price_whole.text)
            if digits:
                return f"¥{int(digits):,}"
    except:
        pass
    return ""


def parse_jpy(price_str: str) -> int:
    """'¥6,645' → 6645"""
    if not price_str:
        return 0
    return int(re.sub(r"[^\d]", "", price_str) or 0)


# ==========================================
# Step 2: Keepa で仕入れ情報取得
# ==========================================
def get_keepa_info(jan: str, api_key: str) -> tuple[str | None, str | None, str | None, float | None]:
    """
    Keepa API でJANコードから商品情報を取得。
    戻り値: (商品名, 価格文字列, Amazon URL, 請求重量kg)
    domain=5 = Amazon Japan
    価格はKeepaが×100で格納しているため÷100してYen換算。
    重量はKeepaが100g単位で格納 → ÷10 でkg換算。
    寸法はKeepaがmm単位で格納 → ÷10 でcm換算。
    """
    import math
    try:
        resp = requests.get(
            "https://api.keepa.com/product",
            params={
                "key":     api_key,
                "domain":  5,       # Amazon Japan
                "code":    jan,
                "stats":   1,
                "history": 0,       # 価格履歴不要（トークン節約）
            },
            timeout=15,
        )
        resp.raise_for_status()
        products = resp.json().get("products", [])
        if not products:
            return None, None, None, None

        product = products[0]
        title   = product.get("title") or None
        asin    = product.get("asin", "")
        url     = f"https://www.amazon.co.jp/dp/{asin}" if asin else None

        # 現在価格: stats.current[0]=Amazon直販, [1]=マーケットプレイス新品
        current   = (product.get("stats") or {}).get("current") or []
        price_raw = -1
        for idx in [0, 1]:
            if len(current) > idx and current[idx] and current[idx] > 0:
                price_raw = current[idx]
                break

        price_str = None
        if price_raw > 0:
            price_str = f"¥{price_raw:,}"

        # 重量・寸法から請求重量を計算
        # Keepa: packageWeight は100g単位, 寸法はmm単位
        weight_kg = None
        pkg_weight = product.get("packageWeight")   # 100g単位
        pkg_length = product.get("packageLength")   # mm
        pkg_width  = product.get("packageWidth")    # mm
        pkg_height = product.get("packageHeight")   # mm

        actual_kg = None
        if pkg_weight and pkg_weight > 0:
            actual_kg = pkg_weight / 1000.0  # g → kg

        vol_kg = None
        if pkg_length and pkg_width and pkg_height and pkg_length > 0:
            l_cm = pkg_length / 10
            w_cm = pkg_width  / 10
            h_cm = pkg_height / 10
            vol_kg = (l_cm * w_cm * h_cm) / 8000

        if actual_kg is not None and vol_kg is not None:
            weight_kg = max(actual_kg, vol_kg)
        elif actual_kg is not None:
            weight_kg = actual_kg
        elif vol_kg is not None:
            weight_kg = vol_kg

        # グラム単位で切り上げ
        if weight_kg is not None:
            weight_kg = math.ceil(weight_kg * 1000) / 1000

        return title, price_str, url, weight_kg

    except Exception as e:
        print(f"  [Keepa] エラー: {e}")
        return None, None, None, None


# ==========================================
# Step 3: eBay Active Listings 最安値取得
# ==========================================
def _ascii_keywords(title: str) -> str:
    """日本語タイトルからASCII英数字トークンを抽出してeBay検索用文字列を返す。"""
    tokens = re.findall(r"[A-Za-z0-9][A-Za-z0-9+\-\.]*", title)
    tokens = [t for t in tokens if len(t) >= 2]
    return " ".join(tokens[:8])


_EBAY_BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _create_ebay_driver():
    """
    eBay検索用ドライバー。
    通常は ebay_session プロファイル（非ヘッドレス）を使用。
    環境変数 HEADLESS=1 でヘッドレスモードに切り替え可能。
    """
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
        profile_path = os.path.join(_EBAY_BASE_DIR, "ebay_session")
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
    """eBay検索ページから新品最安値(USD)とURLをSeleniumでスクレイピング。"""
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


def get_ebay_active_lowest(jan: str, ebay_driver, title_kw: str = "") -> tuple[float, str]:
    """
    新品かつ最安値のeBay出品価格(USD)と出品URLをSeleniumで取得。
    検索順: ① JAN番号キーワード → ② 商品名英数字キーワード
    ebay_driver が None の場合は (0.0, "") を返す。
    """
    if ebay_driver is None:
        return 0.0, ""

    # ① JAN番号で検索
    if jan:
        price, url = _ebay_search_lowest(ebay_driver, jan)
        if price:
            return price, url

    # ② 商品名英数字部分で検索
    if title_kw:
        price, url = _ebay_search_lowest(ebay_driver, title_kw)
        if price:
            print(f"           (商品名キーワード検索: '{title_kw}')")
            return price, url

    return 0.0, ""


# ==========================================
# Step 4: 利益計算
# ==========================================
def calc_profit(ebay_usd: float, amazon_jpy: int, rate: float,
                weight_kg: float | None = None) -> dict:
    """
    利益計算式（SpeedPAK Economy Japan USA本土48州）:
      利益 = eBay販売価格×レート
             − 仕入れ価格
             − eBay手数料（17%）
             − 関税（仕入れの15%）
             − SpeedPAK基本送料（重量別）
             − 米国輸入通関手数料（¥245/件）
             − 米国関税処理手数料（関税額×2.1%）
    """
    if weight_kg is None or weight_kg <= 0:
        weight_kg = DEFAULT_WEIGHT_KG

    revenue_jpy      = ebay_usd * rate
    ebay_fee_jpy     = revenue_jpy * EBAY_FEE_RATE
    customs_jpy      = amazon_jpy * CUSTOMS_RATE
    base_shipping    = get_speedpak_rate_us48(weight_kg)
    us_processing    = round(customs_jpy * US_CUSTOMS_PROCESSING_RATE)
    total_shipping   = base_shipping + US_CUSTOMS_CLEARANCE_FEE + us_processing
    profit_jpy       = revenue_jpy - amazon_jpy - ebay_fee_jpy - customs_jpy - total_shipping

    return {
        "revenue_jpy":    round(revenue_jpy),
        "ebay_fee_jpy":   round(ebay_fee_jpy),
        "customs_jpy":    round(customs_jpy),
        "weight_kg":      weight_kg,
        "base_shipping":  base_shipping,
        "us_clearance":   US_CUSTOMS_CLEARANCE_FEE,
        "us_processing":  us_processing,
        "shipping_jpy":   total_shipping,
        "profit_jpy":     round(profit_jpy),
        "is_go":          profit_jpy >= MIN_PROFIT_JPY,
    }


# ==========================================
# スプレッドシート書き込み
# ==========================================
def write_to_sheet(ws, jan: str, result: dict, dry_run: bool):
    """
    「新品出品下書き」タブに1行追記する。
    列構成（A〜T）:
      A=JAN, B=商品名, C=タイトル(eBay), D=カテゴリ, E=コンディション,
      F=販売価格(USD), G=送料, H=返品, I=仕入れ先, J=仕入れ価格(JPY),
      K=月間Sold数, L=判定, M=請求重量(kg), N=送料(JPY), O=利益(JPY),
      P=還付金額(JPY), Q=アイテムスペシフィクス, R=作成日, S=ステータス, T=仕入れ先URL
    """
    today       = datetime.now().strftime("%Y-%m-%d")
    judgment    = "✅ GO" if result["is_go"] else "❌ No-Go"
    ebay_usd    = f"${result['ebay_usd']:.2f}" if result["ebay_usd"] else ""
    profit      = result.get("profit") or {}
    amazon_jpy  = parse_jpy(result.get("amazon_price_str", ""))
    refund_jpy  = round(amazon_jpy * 0.1)

    row_data = [
        jan,                                    # A: JAN
        result.get("amazon_title", ""),         # B: 商品名
        "",                                     # C: タイトル(eBay)
        "",                                     # D: カテゴリ
        "New",                                  # E: コンディション
        ebay_usd,                               # F: 販売価格(USD)
        "Free Shipping",                        # G: 送料
        "30 Days Returns",                      # H: 返品
        "Amazon.co.jp",                         # I: 仕入れ先
        result.get("amazon_price_str", ""),     # J: 仕入れ価格(JPY)
        result.get("sold_count", ""),           # K: 月間Sold数(推定)
        judgment,                               # L: 判定
        profit.get("weight_kg", ""),            # M: 請求重量(kg)
        profit.get("shipping_jpy", ""),         # N: 送料(JPY)
        profit.get("profit_jpy", ""),           # O: 利益(JPY)
        refund_jpy if amazon_jpy else "",       # P: 還付金額（仕入れ価格の10%）
        "",                                     # Q: アイテムスペシフィクス
        today,                                  # R: 作成日
        "リサーチ完了",                           # S: ステータス
        result.get("amazon_url", ""),           # T: 仕入れ先URL
    ]

    if dry_run:
        print(f"  [DRY-RUN] 書き込み予定: JAN={jan} | 判定={judgment} | "
              f"利益=¥{result['profit']['profit_jpy']:,}")
        return

    # append_row は空文字列の列でズレが発生するため、明示的な行番号で書き込む
    next_row = len(ws.get_all_values()) + 1
    # シートの行数上限を超える場合は行を追加してから書き込む
    sheet_row_count = ws.row_count
    if next_row > sheet_row_count:
        ws.add_rows(100)
    ws.update(f'A{next_row}', [row_data], value_input_option='USER_ENTERED')
    time.sleep(0.5)


# ==========================================
# 1件のJANコードを処理
# ==========================================
def research_one(jan: str, rate: float, ws, dry_run: bool,
                 account: str = "kozuki", force: bool = False,
                 manual_sold: int = 0, driver=None, ebay_driver=None) -> dict:
    print(f"\n{'─'*55}")
    print(f"  JAN: {jan}")
    print(f"{'─'*55}")

    ebay_title = ""

    # ── Keepa: 商品名・価格・URL・重量取得（Step1/2で共用）────
    keepa_key = _get_keepa_api_key()
    print("  [Keepa] 商品情報取得中...")
    amazon_title, amazon_price_str, amazon_url, weight_kg = (
        get_keepa_info(jan, keepa_key) if keepa_key else (None, None, None, None)
    )
    if amazon_title:
        print(f"          商品名: {amazon_title[:60]}")
    if amazon_price_str:
        print(f"          価格 : {amazon_price_str}")
    if weight_kg:
        print(f"          請求重量: {weight_kg:.3f} kg")
    else:
        print(f"          請求重量: 不明 → デフォルト {DEFAULT_WEIGHT_KG} kg を使用")
    if not keepa_key:
        print("  ⚠️  KEEPA_API_KEY未設定")

    # ── Step 1: Terapeak で販売実績確認 ───────────
    terapeak_avg_usd = 0.0
    if force:
        sold_count = manual_sold if manual_sold > 0 else SOLD_THRESHOLD
        print(f"  [Step 1] ⏭  スキップ（--force 指定）販売数: {sold_count}個として処理")
    else:
        print("  [Step 1] Terapeak販売実績確認（過去30日・JANコード検索）...")
        sold_count, ebay_title, terapeak_avg_usd = get_terapeak_sold_count(jan, "", driver)
        print(f"           販売数: {sold_count}個")
        if terapeak_avg_usd:
            print(f"           Terapeak平均販売価格: ${terapeak_avg_usd:.2f}")

        if sold_count < SOLD_THRESHOLD:
            print(f"  → ❌ 販売実績不足（{sold_count}個 < {SOLD_THRESHOLD}個）スキップ")
            return {"status": "skipped", "reason": "販売実績不足", "sold_count": sold_count}

    print(f"  → ✅ 販売実績OK（{sold_count}個）")

    # ── Step 2: 仕入れ価格確認（Keepaで取得済み）──────
    print("  [Step 2] 仕入れ価格確認（Keepa）...")
    if not amazon_price_str:
        print("  → ❌ Keepa価格取得失敗。スキップ")
        return {"status": "skipped", "reason": "Keepa価格取得失敗"}

    amazon_jpy = parse_jpy(amazon_price_str)
    print(f"           商品名: {(amazon_title or '(不明)')[:50]}")
    print(f"           価格: {amazon_price_str}")

    # ── Step 3: eBay最安値（Active）────────────────
    print("  [Step 3] eBay最安値（Active Listings・新品）取得...")
    ebay_usd, ebay_url = get_ebay_active_lowest(
        jan, ebay_driver, title_kw=_ascii_keywords(amazon_title or ""))

    price_source = "eBay Active"
    if ebay_usd:
        print(f"           最安値: ${ebay_usd:.2f}")
        print(f"           URL: {ebay_url[:70] if ebay_url else '(なし)'}")
    elif terapeak_avg_usd:
        ebay_usd = terapeak_avg_usd
        ebay_url = ""
        price_source = "Terapeak販売履歴(平均)"
        print(f"           → eBay出品なし。Terapeak平均販売価格で利益計算: ${ebay_usd:.2f}")
    else:
        print("           → eBay出品なし・Terapeak価格も取得不可（利益計算は価格0で実行）")

    # ── Step 4: 利益計算 ─────────────────────────
    print(f"  [Step 4] 利益計算... (レート: ¥{rate:.1f}/USD, 価格ソース: {price_source})")
    profit = calc_profit(ebay_usd, amazon_jpy, rate, weight_kg)

    used_weight = profit['weight_kg']
    print(f"           売上   : ¥{profit['revenue_jpy']:>8,}")
    print(f"           仕入れ : ¥{amazon_jpy:>8,}  (−)")
    print(f"           eBay手数料: ¥{profit['ebay_fee_jpy']:>6,}  (−)")
    print(f"           関税   : ¥{profit['customs_jpy']:>8,}  (−)")
    print(f"           送料   : ¥{profit['shipping_jpy']:>8,}  (−)  [{used_weight:.3f}kg: 基本¥{profit['base_shipping']:,} + 通関¥{profit['us_clearance']} + 関税処理¥{profit['us_processing']}]")
    print(f"           {'─'*30}")
    judgment = "✅ GO" if profit["is_go"] else "❌ No-Go"
    print(f"           利益   : ¥{profit['profit_jpy']:>8,}  → {judgment}")

    # 送料5,000円以上は除外
    SHIPPING_LIMIT_JPY = 5000
    if profit["shipping_jpy"] >= SHIPPING_LIMIT_JPY:
        print(f"  → ❌ 送料上限超過（¥{profit['shipping_jpy']:,} ≥ ¥{SHIPPING_LIMIT_JPY:,}）スキップ")
        return {"status": "skipped", "reason": f"送料上限超過（¥{profit['shipping_jpy']:,}）"}

    product_name = amazon_title or ebay_title or jan

    result = {
        "status":           "processed",
        "sold_count":       sold_count,
        "amazon_title":     product_name,
        "amazon_price_str": amazon_price_str,
        "amazon_url":       amazon_url or "",
        "ebay_usd":         ebay_usd,
        "ebay_url":         ebay_url,
        "profit":           profit,
        "is_go":            profit["is_go"],
    }

    write_to_sheet(ws, jan, result, dry_run)
    return result


# ==========================================
# メイン
# ==========================================
def main():
    args    = sys.argv[1:]
    dry_run = "--dry-run" in args
    force   = "--force"   in args

    # --account kozuki/kaworu/dbz を解析
    account = "kozuki"
    for i, arg in enumerate(args):
        if arg == "--account" and i + 1 < len(args):
            account = args[i + 1]
            break
        if arg.startswith("--account="):
            account = arg.split("=", 1)[1]
            break

    # --sold-count N を解析（--force と併用して手動販売数を指定）
    manual_sold = 0
    for i, arg in enumerate(args):
        if arg == "--sold-count" and i + 1 < len(args):
            try:
                manual_sold = int(args[i + 1])
            except ValueError:
                pass
            break
        if arg.startswith("--sold-count="):
            try:
                manual_sold = int(arg.split("=", 1)[1])
            except ValueError:
                pass
            break

    valid_accounts = {"kozuki", "kaworu", "dbz"}
    if account not in valid_accounts:
        print(f"❌ 無効なアカウント名: {account}（kozuki / kaworu / dbz のいずれかを指定）")
        sys.exit(1)

    # JANコードのみ取り出す
    skip_next = False
    jan_codes = []
    for arg in args:
        if skip_next:
            skip_next = False
            continue
        if arg in ("--account", "--sold-count"):
            skip_next = True
            continue
        if arg.startswith("--"):
            continue
        jan_codes.append(arg)

    if not jan_codes:
        print("使い方: python3 jan_research.py --account <アカウント名> <JANコード>...")
        print("例:     python3 jan_research.py --account kozuki 4901777321991")
        print("        python3 jan_research.py --account kaworu 4901777321991 --dry-run")
        sys.exit(1)

    mode = "[DRY-RUN]" if dry_run else "[本番]"
    print(f"\n{'='*55}")
    print(f"  JANコードリサーチ {mode}")
    print(f"  アカウント: {account}")
    if force:
        print(f"  Step1スキップ: ON（--force）")
    print(f"  対象: {len(jan_codes)}件")
    print(f"{'='*55}")

    # 初期化
    print("[初期化] 為替レート取得中...")
    rate = get_usd_jpy_rate()
    print(f"  ✅ USD/JPY: ¥{rate:.1f}")

    print("[初期化] スプレッドシート接続中...")
    creds  = Credentials.from_service_account_file(JSON_FILE, scopes=SCOPE)
    client = gspread.authorize(creds)
    ws     = client.open_by_key(SPREADSHEET_ID).worksheet(TAB_NAME)
    print("  ✅ 接続成功")

    # Terapeak ドライバー起動
    t_driver = None
    if not force and not TERAPEAK_AVAILABLE:
        print("[初期化] Terapeak未インストール → --force モードで続行（Terapeak スキップ）")
        force = True
    elif not force:
        print("[初期化] Teapeakドライバー起動中...")
        try:
            t_driver = create_driver()
            set_ship_to_us(t_driver)
            print("  ✅ Teapeakドライバー起動完了")
        except Exception as e:
            print(f"  ⚠️  Teapeakドライバー起動失敗: {e}")
            print("  ⚠️  --force オプションを使用するか、Chromeプロファイルを確認してください")
            sys.exit(1)

    # eBay検索用ドライバー起動（ebay_sessionプロファイル・非ヘッドレス）
    ebay_driver = None
    print("[初期化] eBay検索ドライバー起動中...")
    try:
        ebay_driver = _create_ebay_driver()
        print("  ✅ eBay検索ドライバー起動完了")
    except Exception as e:
        print(f"  ⚠️  eBay検索ドライバー起動失敗: {e}")
        print("     Step 3をスキップします（eBay価格は¥0で計算されます）")

    # 全JANコードを処理
    summary = {"go": [], "no_go": [], "skipped": []}
    try:
        for jan in jan_codes:
            jan = jan.strip()
            if not jan.isdigit() or len(jan) < 10:
                print(f"\n  ⚠️  無効なJANコード: {jan}（スキップ）")
                continue

            result = research_one(jan, rate, ws, dry_run, account=account,
                                  force=force, manual_sold=manual_sold, driver=t_driver,
                                  ebay_driver=ebay_driver)

            if result["status"] == "skipped":
                summary["skipped"].append(jan)
            elif result.get("is_go"):
                summary["go"].append(jan)
            else:
                summary["no_go"].append(jan)

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

    # サマリー表示
    print(f"\n{'='*55}")
    print(f"  完了サマリー")
    print(f"{'='*55}")
    print(f"  ✅ GO      : {len(summary['go'])}件  {summary['go']}")
    print(f"  ❌ No-Go   : {len(summary['no_go'])}件  {summary['no_go']}")
    print(f"  ⏭  スキップ : {len(summary['skipped'])}件  {summary['skipped']}")
    if not dry_run and (summary["go"] or summary["no_go"]):
        print(f"\n  スプレッドシート「{TAB_NAME}」タブに書き込みました。")
    print(f"{'='*55}\n")


if __name__ == "__main__":
    main()
