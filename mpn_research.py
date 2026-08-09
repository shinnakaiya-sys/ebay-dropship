"""
mpn_research.py
===============
ライバルセラーの販売履歴からMPNを自動収集し、JANコードを特定して
jan_research.py の本体リサーチ（Terapeak・Keepa・eBay最安値・利益計算）に連結する。

=== Phase 1（--seller / --seller-list 指定時）: eBay販売履歴からMPN収集 ===
  1a. Selenium で販売済み商品のItem IDリストを収集
  1b. eBay Browse API で各商品のMPN・GTINを取得

=== Phase 2: MPN → JANコード特定 → jan_research.py に委譲 ===
  ① 既知のJAN（Phase 1でGTINとして収集済み）があればそれを使用
  ② eBay Browse APIでMPNを検索し、日本製JAN(GTIN)を探す
  ③ ①②で見つからなければ、Keepa /search でMPN→ASINを照合し、
     /product のEAN/UPCリストから日本製JANコードを抽出する
  ④ JANが特定できたら jan_research.research_one() を呼び出し、
     Terapeak販売実績確認・Keepa仕入れ価格取得・eBay最安値取得・利益計算・
     スプレッドシート書き込みまでを一括で実行する
  JANが特定できないMPNはスキップする。

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
import time
import base64
import requests
from dotenv import dotenv_values, load_dotenv
load_dotenv()
import gspread
from google.oauth2.service_account import Credentials
import jan_research
try:
    from terapeak_research import create_driver, set_ship_to_us
    TERAPEAK_AVAILABLE = True
except ImportError:
    TERAPEAK_AVAILABLE = False

# ==========================================
# 設定
# ==========================================
BASE_DIR      = os.path.dirname(os.path.abspath(__file__))
CLIENT_ID     = os.getenv("EBAY_CLIENT_ID") or os.getenv("EBAY_APP_ID", "")
CLIENT_SECRET = os.getenv("EBAY_CLIENT_SECRET", "")

# eBayでよく入力される「意味のないMPN」— これらはスキップ
GENERIC_MPN = {
    "DOESNOTAPPLY", "NOTAPPLY", "NA", "NONE", "UNKNOWN", "GENERIC",
    "BLACK", "WHITE", "RED", "BLUE", "SILVER", "GOLD", "JAPAN", "ORIGINAL",
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


def is_japan_jan(code: str) -> bool:
    """13桁・45/49始まりの日本製JANコードか判定"""
    return bool(code) and len(code) == 13 and code.isdigit() and (
        code.startswith("45") or code.startswith("49"))


def search_jan_by_mpn(mpn: str) -> str:
    """eBay Browse APIでMPNをキーワード検索し、日本製JANコード(GTIN)を探す。"""
    token = _get_browse_token()
    if not token:
        return ""
    try:
        r = requests.get(
            "https://api.ebay.com/buy/browse/v1/item_summary/search",
            headers={"Authorization": f"Bearer {token}"},
            params={"q": mpn, "limit": 10, "filter": "itemLocationCountry:JP"},
            timeout=10,
        )
        if not r.ok:
            return ""
        for item in r.json().get("itemSummaries", []):
            gtin = (item.get("gtin") or "").strip()
            if gtin and is_japan_jan(gtin):
                return gtin
    except Exception:
        pass
    return ""


def get_jan_for_mpn(mpn: str, known_jan: str = "") -> tuple[str, str]:
    """
    MPNからJANコードを特定する。優先順位:
      ① Phase 1で収集済みのJAN（GTIN）
      ② eBay Browse APIでMPNを検索し、日本製JAN(GTIN)を探す
      ③ ①②で見つからなければ、Keepa(ASIN経由のEAN/UPCリスト)で探す
    戻り値: (JANコード, ASIN)。ASINはKeepa経由で特定できた場合のみ設定。
    """
    if known_jan and is_japan_jan(known_jan):
        return known_jan, ""

    jan = search_jan_by_mpn(mpn)
    if jan:
        print(f"        JAN: {jan}（eBay Browse APIより特定）")
        return jan, ""

    keepa_key = _get_keepa_api_key()
    if not keepa_key:
        return "", ""

    asin, jan, match_status = find_asin_and_jan_by_mpn(mpn, keepa_key)
    if jan:
        print(f"        JAN: {jan}（Keepa ASIN {asin} のEAN/UPCより特定, {match_status}）")
    return jan, asin


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


def scrape_mpns_from_seller(seller_id_or_url: str, max_items: int = 50) -> list[tuple[str, str]]:
    """
    セラーの販売履歴からMPNを収集して返す。
      Phase 1a: Selenium で Item ID リスト収集
      Phase 1b: Browse API で各アイテムの MPN を取得
    有効な (MPN, JAN) のタプルリスト（重複除去済み。JAN不明時は空文字）を返す。
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

    mpn_jan_pairs = [(mpn, gtin if is_japan_jan(gtin) else "")
                     for mpn, (title, gtin) in mpn_set.items()]
    print(f"\n  → MPN収集完了: {len(item_ids)}件中 {len(mpn_jan_pairs)}件の有効MPN")
    for mpn, (title, gtin) in mpn_set.items():
        gtin_note = f" (GTIN:{gtin})" if gtin else ""
        print(f"    {mpn}{gtin_note}  ← {title[:40]}")

    return mpn_jan_pairs


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
# Keepa: ASIN から商品詳細取得（EAN/UPCリスト含む）
# ==========================================
def _keepa_product_by_asin(asin: str, api_key: str) -> dict:
    """Keepa /product でASIN→商品詳細（生データ）を返す。取得失敗時は空dict。"""
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
        return products[0] if products else {}
    except Exception as e:
        print(f"  [Keepa/product] エラー: {e}")
        return {}


def _extract_jp_jan(product: dict) -> str:
    """Keepa商品データのEAN/UPCリストから日本製JANコードを抽出する。"""
    codes = (product.get("eanList") or []) + (product.get("upcList") or [])
    for code in codes:
        if is_japan_jan(code):
            return code
    return ""


# ==========================================
# MPN → ASIN → JANコード特定
# ==========================================
def find_asin_and_jan_by_mpn(mpn: str, api_key: str) -> tuple[str, str, str]:
    """
    Keepa /search でMPN照合→ASIN取得し、/product のEAN/UPCリストから
    日本製JANコードを抽出する。
    戻り値: (ASIN, JANコード, 照合ステータス)。見つからない場合は空文字。
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
        return "", "", ""

    if not products:
        print(f"  — Keepa: MPN '{mpn}' → 該当なし  (残トークン: {tokens})")
        return "", "", ""

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
        return "", "", ""

    print(f"  🔍 Keepa: MPN '{mpn}' → ASIN {asin} [{match_status}]  (残トークン: {tokens})")
    time.sleep(1)

    full_product = _keepa_product_by_asin(asin, api_key)
    jan = _extract_jp_jan(full_product)
    return asin, jan, match_status


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


# ==========================================
# Phase 2: 1件のMPNをリサーチ
# ==========================================
def research_one(mpn: str, rate: float, ws,
                 account: str = "kozuki", force: bool = False,
                 manual_sold: int = 0, driver=None, ebay_driver=None,
                 dry_run: bool = False, known_jan: str = "") -> dict:
    print(f"\n{'─'*55}")
    print(f"  MPN: {mpn}")
    print(f"{'─'*55}")

    if not is_searchable_mpn(mpn):
        print(f"  → ⚠️  汎用/無効なMPN → スキップ")
        return {"status": "skipped", "reason": "汎用/無効なMPN"}

    # ── JAN / ASIN 特定 ──────────────────────────────────
    print("  [JAN] JANコード特定中...")
    jan, asin = get_jan_for_mpn(mpn, known_jan)
    if not jan:
        print("  → ❌ JANコードが特定できませんでした（eBay/Keepaいずれからも未検出）。スキップ")
        return {"status": "skipped", "reason": "JAN未特定"}

    # ── jan_research.py にリサーチを委譲 ──────────────────
    print(f"  [連携] JAN {jan} を jan_research.py のリサーチに委譲します...")
    result = jan_research.research_one(
        jan, rate, ws, dry_run,
        account=account, force=force, manual_sold=manual_sold,
        driver=driver, ebay_driver=ebay_driver,
    )
    result["mpn"] = mpn
    result["jan"] = jan
    if asin:
        result["asin"] = asin
    return result


# ==========================================
# Phase 2: 全MPNをリサーチ（ドライバー管理含む）
# ==========================================
def run_research(mpn_pairs: list[tuple[str, str]], rate: float, ws, dry_run: bool,
                 account: str, force: bool, manual_sold: int) -> dict:
    """(MPN, JAN)リストに対してリサーチを実行し、サマリー辞書を返す。"""

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
        for mpn, known_jan in mpn_pairs:
            mpn = mpn.strip()
            result = research_one(mpn, rate, ws, account=account,
                                  force=force, manual_sold=manual_sold,
                                  driver=t_driver, ebay_driver=ebay_driver,
                                  dry_run=dry_run, known_jan=known_jan)

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

    print(f"[初期化] スプレッドシート接続中...（連携先: jan_research.py「{jan_research.TAB_NAME}」タブ）")
    creds  = Credentials.from_service_account_file(jan_research.JSON_FILE, scopes=jan_research.SCOPE)
    client = gspread.authorize(creds)
    ws     = client.open_by_key(jan_research.SPREADSHEET_ID).worksheet(jan_research.TAB_NAME)
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
        mpn_pairs = [(mpn, "") for mpn in mpn_codes]
        s = run_research(mpn_pairs, rate, ws, dry_run, account, force, manual_sold)
        _merge(s)

    # ── サマリー ──────────────────────────────────────────
    print(f"\n{'='*55}")
    print(f"  完了サマリー")
    print(f"{'='*55}")
    print(f"  ✅ GO      : {len(total_summary['go'])}件  {total_summary['go']}")
    print(f"  ❌ No-Go   : {len(total_summary['no_go'])}件  {total_summary['no_go']}")
    print(f"  ⏭  スキップ : {len(total_summary['skipped'])}件  {total_summary['skipped']}")
    if not dry_run and (total_summary["go"] or total_summary["no_go"]):
        print(f"\n  スプレッドシート「{jan_research.TAB_NAME}」タブに書き込みました。")
    print(f"{'='*55}\n")


if __name__ == "__main__":
    main()
