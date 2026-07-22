"""
test_wait_speed.py
==================
WebDriverWait vs time.sleep(4) の速度・精度比較テスト。
シート・eBay API は一切触らない。

使い方:
  python3 test_wait_speed.py <eBay検索URL>
  python3 test_wait_speed.py  # URLなし → デフォルトURL で実行

結果:
  - 両方式でかかった秒数
  - 取得できた competitor 件数（同じなら OK）
"""

import sys
import time
import re
import os

# ─── ドライバー起動（scrape_and_adjust.py と同じ設定） ─────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

def _create_driver():
    import shutil
    from selenium import webdriver
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.chrome.options import Options

    options = Options()
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


# ─── URL にフィルタパラメータを付与 ────────────────────────────────────────
def _build_url(url: str) -> str:
    from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
    parsed = urlparse(url)
    params = parse_qs(parsed.query, keep_blank_values=True)
    params["LH_BIN"]           = ["1"]
    params["LH_ItemCondition"] = ["1000"]
    params["_sop"]             = ["15"]
    params["LH_PrefLoc"]       = ["2"]
    new_query = urlencode({k: v[0] for k, v in params.items()})
    return urlunparse(parsed._replace(query=new_query))


# ─── 方式 A: 現行の固定 sleep ──────────────────────────────────────────────
def method_fixed_sleep(driver, url: str, sleep_sec: float = 4.0) -> dict:
    from selenium.webdriver.common.by import By

    driver.get(url)
    t0 = time.time()
    time.sleep(sleep_sec)
    elapsed = time.time() - t0

    items = driver.find_elements(By.CSS_SELECTOR, "ul.srp-results li")
    count = sum(1 for it in items if it.find_elements(By.CSS_SELECTOR, "a[href*='/itm/']"))
    return {"method": f"fixed sleep({sleep_sec}s)", "elapsed": elapsed, "item_count": count}


# ─── 方式 B: WebDriverWait（提案方式） ────────────────────────────────────
def method_wait(driver, url: str, timeout: float = 10.0) -> dict:
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC

    driver.get(url)
    t0 = time.time()
    try:
        WebDriverWait(driver, timeout).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "ul.srp-results li"))
        )
    except Exception:
        pass  # タイムアウト → 空リストとして後続処理が処理
    elapsed = time.time() - t0

    items = driver.find_elements(By.CSS_SELECTOR, "ul.srp-results li")
    count = sum(1 for it in items if it.find_elements(By.CSS_SELECTOR, "a[href*='/itm/']"))
    return {"method": f"WebDriverWait(timeout={timeout}s)", "elapsed": elapsed, "item_count": count}


# ─── メイン ────────────────────────────────────────────────────────────────
def main():
    # デフォルト: eBay の汎用検索（何でも出る）
    default_url = "https://www.ebay.com/sch/i.html?_nkw=sony+headphones"
    url = sys.argv[1] if len(sys.argv) > 1 else default_url
    url = _build_url(url)

    print("=" * 55)
    print("  WebDriverWait vs fixed sleep  比較テスト")
    print("=" * 55)
    print(f"  URL: {url[:70]}...")
    print()

    results = []
    driver = _create_driver()
    try:
        # A: fixed sleep 4秒
        print("[テスト 1/3] 固定 sleep 4秒 ...")
        r = method_fixed_sleep(driver, url, sleep_sec=4.0)
        results.append(r)
        print(f"  → {r['elapsed']:.2f}秒  取得件数: {r['item_count']}件")
        time.sleep(2)

        # B: WebDriverWait
        print("[テスト 2/3] WebDriverWait (最大10秒) ...")
        r = method_wait(driver, url, timeout=10.0)
        results.append(r)
        print(f"  → {r['elapsed']:.2f}秒  取得件数: {r['item_count']}件")
        time.sleep(2)

        # C: fixed sleep 2秒（短縮版の比較用）
        print("[テスト 3/3] 固定 sleep 2秒 ...")
        r = method_fixed_sleep(driver, url, sleep_sec=2.0)
        results.append(r)
        print(f"  → {r['elapsed']:.2f}秒  取得件数: {r['item_count']}件")

    finally:
        driver.quit()

    # ─── 結果サマリー ───────────────────────────────────────────────────
    print()
    print("=" * 55)
    print("  結果サマリー")
    print("=" * 55)
    ref_count = results[0]["item_count"]
    for r in results:
        match = "✅ 一致" if r["item_count"] == ref_count else f"❌ 不一致（基準={ref_count}）"
        print(f"  {r['method']:<35}  {r['elapsed']:.2f}秒  件数:{r['item_count']} {match}")

    print()
    wait_result = results[1]
    sleep4_result = results[0]
    saved = sleep4_result["elapsed"] - wait_result["elapsed"]
    print(f"  WebDriverWait は固定sleep(4秒)より {saved:+.2f}秒 {'速い' if saved > 0 else '遅い'}")
    print()
    print("  ※ 件数が全テストで一致していれば WebDriverWait への切り替えは安全です")
    print("=" * 55)


if __name__ == "__main__":
    main()
