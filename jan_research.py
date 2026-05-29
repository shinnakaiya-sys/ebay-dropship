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
from dotenv import dotenv_values
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
        return 150.0  # API失敗時のフォールバック


# ==========================================
# Step 1: Terapeak で過去30日の販売数確認
# ==========================================
def get_terapeak_sold_count(jan: str, _unused: str, driver) -> tuple[int, str]:
    """
    Terapeak（Seller Hub Research）で過去30日の販売数と代表タイトルを返す。
    JANコードで検索し、複数行ヒットした場合は総販売数を合計して返す。
    """
    print(f"           Teapeakキーワード: {jan}")

    try:
        do_research(driver, keywords=jan, days=30, min_price=0, category_id="")
        rows = extract_rows(driver)
        if not rows:
            return 0, ""
        total = 0
        first_title = rows[0].get("タイトル", "")
        for row in rows:
            sold_str = row.get("総販売数", "0")
            total += int(re.sub(r"[^\d]", "", sold_str) or 0)
        return total, first_title
    except Exception as e:
        print(f"  [Terapeak] 検索エラー: {e}")
        return 0, ""


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
def get_ebay_active_lowest(jan: str, token: str) -> tuple[float, str]:
    """
    新品かつ最安値のeBay出品価格(USD)と出品URLを返す。
    GTINで見つからない場合はキーワード検索にフォールバック。
    """
    url     = "https://api.ebay.com/buy/browse/v1/item_summary/search"
    headers = {"Authorization": f"Bearer {token}"}

    # GTINで検索（新品・最安値順）
    params = {
        "gtin":   jan,
        "filter": "conditions:{NEW}",
        "sort":   "price",
        "limit":  "5",
    }
    try:
        resp  = requests.get(url, headers=headers, params=params, timeout=10)
        items = resp.json().get("itemSummaries", [])

        # GTINで見つからない場合はキーワード検索
        if not items:
            params2 = {
                "q":      jan,
                "filter": "conditions:{NEW}",
                "sort":   "price",
                "limit":  "5",
            }
            resp  = requests.get(url, headers=headers, params=params2, timeout=10)
            items = resp.json().get("itemSummaries", [])

        if items:
            item      = items[0]
            price_val = float(item.get("price", {}).get("value", 0))
            item_url  = item.get("itemWebUrl", "")
            return price_val, item_url
    except Exception as e:
        print(f"  [eBay Browse API] エラー: {e}")

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
def research_one(jan: str, token: str, rate: float, ws, dry_run: bool,
                 account: str = "kozuki", force: bool = False,
                 manual_sold: int = 0, driver=None) -> dict:
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
    if force:
        sold_count = manual_sold if manual_sold > 0 else SOLD_THRESHOLD
        print(f"  [Step 1] ⏭  スキップ（--force 指定）販売数: {sold_count}個として処理")
    else:
        print("  [Step 1] Terapeak販売実績確認（過去30日・JANコード検索）...")
        sold_count, ebay_title = get_terapeak_sold_count(jan, "", driver)
        print(f"           販売数: {sold_count}個")

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
    ebay_usd, ebay_url = get_ebay_active_lowest(jan, token)

    if ebay_usd:
        print(f"           最安値: ${ebay_usd:.2f}")
        print(f"           URL: {ebay_url[:70] if ebay_url else '(なし)'}")
    else:
        print("           → eBay出品なし（利益計算は仮価格0で実行）")

    # ── Step 4: 利益計算 ─────────────────────────
    print(f"  [Step 4] 利益計算... (レート: ¥{rate:.1f}/USD)")
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
    print("\n[初期化] eBayトークン取得中...")
    token = get_ebay_token()
    if not token:
        print("  ❌ eBayトークン取得失敗。終了します。")
        sys.exit(1)
    print("  ✅ トークン取得成功")

    print("[初期化] 為替レート取得中...")
    rate = get_usd_jpy_rate()
    print(f"  ✅ USD/JPY: ¥{rate:.1f}")

    print("[初期化] スプレッドシート接続中...")
    creds  = ServiceAccountCredentials.from_json_keyfile_name(JSON_FILE, SCOPE)
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

    # 全JANコードを処理
    summary = {"go": [], "no_go": [], "skipped": []}
    try:
        for jan in jan_codes:
            jan = jan.strip()
            if not jan.isdigit() or len(jan) < 10:
                print(f"\n  ⚠️  無効なJANコード: {jan}（スキップ）")
                continue

            result = research_one(jan, token, rate, ws, dry_run, account=account,
                                  force=force, manual_sold=manual_sold, driver=t_driver)

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
