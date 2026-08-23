"""
terapeak_jan_daily.py
======================
直近N日の米国eBay SOLD実績（日本セラー・新品・$50以上）をTerapeakからoffsetページングで取得し、
Browse APIでJAN(GTIN)を照合する。日本製JAN(45/49始まり)が付いた新着アイテムのJANだけを、
いつも通り jan_research.py に流し込み、4ステップ判定（Sold実績→仕入れ価格→eBay最安値→利益計算）で
GO/No-Go判定させる（＝利益が出る商品だけがスプレッドシートに「✅ GO」で残る）。

Terapeakのログインは terapeak_research.py と同じ Chrome プロファイル(ebay_session_kaworu)を
使い回すため、他スクリプトが既にログイン済みであれば追加のログイン作業は不要。

使い方:
  python3 terapeak_jan_daily.py                      # 直近3日分をkozukiアカウントでjan_research.pyへ
  python3 terapeak_jan_daily.py --account kaworu
  python3 terapeak_jan_daily.py --days 7 --dry-run
  python3 terapeak_jan_daily.py --all-gtin            # 45/49以外のJANも対象にする
  python3 terapeak_jan_daily.py --force               # jan_research.py側のTerapeak Step1をスキップ
"""

import argparse
import base64
import csv
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

import requests
from dotenv import load_dotenv

load_dotenv()

from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from terapeak_research import create_driver, set_ship_to_us

# ---------------------------------------------------------------- 設定
JST = timezone(timedelta(hours=9))

BASE_DIR   = Path(os.path.dirname(os.path.abspath(__file__)))
STATE_DIR  = BASE_DIR / "terapeak_daily"
STATE_FILE = STATE_DIR / "seen_items.json"      # 取得済みitem ID（日次差分用）
OUT_DIR    = STATE_DIR / "out"                   # CSV監査ログ

# Terapeak検索条件（URLパラメータと1:1）
ROLLING_DAYS   = 3          # 直近3日
MIN_PRICE      = 50         # $50以上
CONDITION_ID   = 1000       # 1000 = New
CATEGORY_ID    = 0          # 全カテゴリ
MARKETPLACE    = "EBAY-US"
SELLER_COUNTRY = "SellerLocation:::JP"
KEYWORDS       = "a -abcd"  # 全件取得の裏技
PAGE_LIMIT     = 50         # 1ページ件数（50が最大）
MAX_PAGES      = 10         # 1回の取得上限（50件×10ページ=500件）。Browse APIクォータ保護のためデフォルトで制限
PAGE_WAIT_SEC  = 2.5        # ページ描画待ち
PAGE_SLEEP     = 1.5        # ページ間スリープ（bot判定回避）

# eBay Browse API（アプリケーショントークン = client credentials）
EBAY_CLIENT_ID     = os.getenv("EBAY_CLIENT_ID") or os.getenv("EBAY_APP_ID", "")
EBAY_CLIENT_SECRET = os.getenv("EBAY_CLIENT_SECRET", "")
JP_JAN_RE = re.compile(r"^(45|49)\d{11}$")

VALID_ACCOUNTS  = {"kozuki", "kaworu", "dbz"}
JAN_BATCH_SIZE  = 30         # jan_research.pyへ流す単位（rival_jan_research.pyと同じ）

# Browse API レート制限対策
BROWSE_SLEEP_BASE   = 0.3    # 通常時のリクエスト間隔（秒）
BROWSE_SLEEP_MAX     = 3.0   # 429発生時に伸ばす間隔の上限（秒）
BROWSE_MAX_RETRIES   = 3     # 1アイテムあたりの429リトライ回数
BROWSE_BACKOFF_BASE  = 30    # 初回バックオフ秒数（Retry-Afterが無い場合）
BROWSE_BACKOFF_CAP   = 300   # バックオフ秒数の上限
BROWSE_ABORT_STREAK  = 5     # 429が連続でこの回数続いたらクォータ切れと判断し打ち切り


# ---------------------------------------------------------------- Terapeak
def build_url(offset: int, start_ms: int, end_ms: int) -> str:
    q = {
        "marketplace": MARKETPLACE,
        "keywords": KEYWORDS,
        "dayRange": "CUSTOM",
        "startDate": start_ms,
        "endDate": end_ms,
        "categoryId": CATEGORY_ID,
        "conditionId": CONDITION_ID,
        "minPrice": MIN_PRICE,
        "sellerCountry": SELLER_COUNTRY,
        "offset": offset,
        "limit": PAGE_LIMIT,
        "sorting": "itemssold",
        "tabName": "SOLD",
        "tz": "Asia/Tokyo",
    }
    return "https://www.ebay.com/sh/research?" + urlencode(q)


# ページ内で走らせる抽出スクリプト（DOM構造は2026-08時点で検証済み）
EXTRACT_JS = r"""
var rows = Array.prototype.slice.call(document.querySelectorAll('tr.research-table-row'));
var cell = function(r, key) {
  var td = r.querySelector('.research-table-row__' + key);
  return td ? td.innerText.trim() : '';
};
var money = function(s) {
  var m = (s || '').match(/\$([\d,]+\.?\d*)/);
  return m ? parseFloat(m[1].replace(/,/g, '')) : null;
};
return rows.map(function(r) {
  var a = r.querySelector('.research-table-row__product-info-name a');
  var href = a ? (a.getAttribute('href') || '') : '';
  var id = href.match(/\/itm\/(\d{11,13})/);
  var ship = cell(r, 'avgShippingCost');
  return {
    item_id: id ? id[1] : null,
    title: a ? a.textContent.trim() : '',
    avg_sold_price: money(cell(r, 'avgSoldPrice')),
    format: /Auction/i.test(cell(r, 'avgSoldPrice')) ? 'Auction' : 'Fixed price',
    avg_shipping: money(ship),
    free_shipping_pct: (ship.match(/(\d+)%\s*Free/) || [null, null])[1],
    total_sold: parseInt((cell(r, 'totalSoldCount') || '0').replace(/[^\d]/g, '') || '0'),
    total_sales: money(cell(r, 'totalSalesValue')),
    date_last_sold: cell(r, 'dateLastSold'),
  };
}).filter(function(x) { return x.item_id; });
"""


def scrape_terapeak(driver, days: int) -> list[dict]:
    """Terapeakをoffsetでページングしてitem行を集める"""
    now = datetime.now(JST)
    end_ms = int(now.timestamp() * 1000)
    start = (now - timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0)
    start_ms = int(start.timestamp() * 1000)
    print(f"[range] {start:%Y-%m-%d %H:%M} 〜 {now:%Y-%m-%d %H:%M} JST")

    rows, seen_in_run = [], set()

    for i in range(MAX_PAGES):
        offset = i * PAGE_LIMIT
        driver.get(build_url(offset, start_ms, end_ms))
        try:
            WebDriverWait(driver, 25).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "tr.research-table-row"))
            )
        except Exception:
            print(f"[page {i+1}] 行なし → 終了 (offset={offset})")
            break
        time.sleep(PAGE_WAIT_SEC)

        batch = driver.execute_script(EXTRACT_JS)
        new = [r for r in batch if r["item_id"] not in seen_in_run]
        for r in new:
            seen_in_run.add(r["item_id"])
        print(f"[page {i+1}] offset={offset} 取得={len(batch)} 新規={len(new)}")

        rows.extend(new)
        if len(batch) < PAGE_LIMIT or not new:
            print("[page] 打ち止め")
            break
        time.sleep(PAGE_SLEEP)

    return rows


# ---------------------------------------------------------------- Browse API
def get_app_token() -> str:
    if not EBAY_CLIENT_ID or not EBAY_CLIENT_SECRET:
        sys.exit("EBAY_CLIENT_ID(または EBAY_APP_ID) / EBAY_CLIENT_SECRET を.envに設定してください")
    auth = base64.b64encode(f"{EBAY_CLIENT_ID}:{EBAY_CLIENT_SECRET}".encode()).decode()
    r = requests.post(
        "https://api.ebay.com/identity/v1/oauth2/token",
        headers={"Authorization": f"Basic {auth}",
                 "Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "client_credentials",
              "scope": "https://api.ebay.com/oauth/api_scope"},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def fetch_gtins(item_ids: list[str], token: str) -> tuple[dict, list[str]]:
    """
    getItem（単体・1件ずつ）でGTIN・MPN・ブランド等を取得。
    Browse APIのバッチ取得（item_ids=パラメータでの複数件同時取得）はこのアプリの
    OAuthスコープでは403 Access Deniedとなることを確認済みのため、単体アイテム
    取得エンドポイント（/item/{item_id}）を1件ずつ呼び出す。

    429時はRetry-Afterヘッダーを尊重しつつ指数バックオフでリトライし、以降の
    リクエスト間隔も自動的に広げる。429が連続で続く場合は本日分のクォータ切れと
    判断し、残りを打ち切ってpendingとして返す（pendingはseen登録しないので
    次回実行時に再照会される＝取りこぼしを防ぐ）。
    """
    out: dict = {}
    pending: list[str] = []
    headers = {"Authorization": f"Bearer {token}",
               "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"}
    total = len(item_ids)
    sleep_interval = BROWSE_SLEEP_BASE
    consecutive_429 = 0

    for i, item_id in enumerate(item_ids):
        url = f"https://api.ebay.com/buy/browse/v1/item/v1|{item_id}|0"
        backoff = BROWSE_BACKOFF_BASE
        r = None
        for attempt in range(BROWSE_MAX_RETRIES + 1):
            try:
                r = requests.get(url, headers=headers, timeout=20)
            except Exception as e:
                print(f"[browse] 通信エラー {item_id}: {e}")
                r = None
                break
            if r.status_code != 429:
                break
            retry_after = r.headers.get("Retry-After", "")
            wait = float(retry_after) if retry_after.replace(".", "", 1).isdigit() else backoff
            wait = min(wait, BROWSE_BACKOFF_CAP)
            print(f"[browse] レート制限 item={item_id} (試行{attempt + 1}/{BROWSE_MAX_RETRIES + 1}) "
                  f"→ {wait:.0f}秒待機")
            time.sleep(wait)
            backoff = min(backoff * 2, BROWSE_BACKOFF_CAP)
            sleep_interval = min(sleep_interval * 1.5, BROWSE_SLEEP_MAX)

        if r is None or r.status_code == 429:
            consecutive_429 += 1
            print(f"[browse] 失敗 {item_id}: 429継続のため今回はスキップ（次回に再照会）")
            pending.append(item_id)
            if consecutive_429 >= BROWSE_ABORT_STREAK:
                remaining = item_ids[i + 1:]
                print(f"[browse] 429が{consecutive_429}件連続 → 本日分クォータ切れと判断し、"
                      f"残り{len(remaining)}件は打ち切り（次回実行時に再照会）")
                pending.extend(remaining)
                break
            continue

        consecutive_429 = 0

        if r.status_code == 404:
            time.sleep(sleep_interval)
            continue

        try:
            r.raise_for_status()
            it = r.json()
        except Exception as e:
            print(f"[browse] 失敗 {item_id}: {e}")
            pending.append(item_id)
            time.sleep(sleep_interval)
            continue

        legacy = it.get("legacyItemId") or item_id
        specs = {s["name"].lower(): (s.get("value") or (s.get("values") or [""])[0])
                 for s in (it.get("localizedAspects") or [])}
        out[legacy] = {
            "gtin": it.get("gtin") or specs.get("jan") or specs.get("ean") or "",
            "mpn": it.get("mpn") or specs.get("mpn") or "",
            "brand": it.get("brand") or specs.get("brand") or "",
            "epid": it.get("epid") or "",
            "category": it.get("categoryPath") or "",
            "seller": (it.get("seller") or {}).get("username", ""),
        }
        if (i + 1) % 50 == 0 or (i + 1) == total:
            print(f"[browse] {i + 1}/{total} 件照会")
        time.sleep(sleep_interval)

    return out, pending


# ---------------------------------------------------------------- jan_research.py 連携
def flush_jans(jan_list: list[str], account: str, dry_run: bool, force: bool):
    """JANコードをバッチでjan_research.pyに渡し、いつもの4ステップ判定をさせる"""
    if not jan_list:
        return
    print(f"\n  → {len(jan_list)}件のJANを jan_research.py へ送信中 (account={account})...")
    cmd = [sys.executable, str(BASE_DIR / "jan_research.py"), "--account", account] + jan_list
    if dry_run:
        cmd.append("--dry-run")
    if force:
        cmd.append("--force")
    subprocess.run(cmd, cwd=str(BASE_DIR))
    print("  → jan_research.py 完了\n")


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", default="kozuki", choices=sorted(VALID_ACCOUNTS),
                     help="jan_research.pyに渡すアカウント名（デフォルト: kozuki）")
    ap.add_argument("--dry-run", action="store_true",
                     help="jan_research.py側のスプレッドシート書き込みをスキップ")
    ap.add_argument("--force", action="store_true",
                     help="jan_research.py側のTerapeak Step1（販売実績再確認）をスキップ")
    ap.add_argument("--all-gtin", action="store_true", help="45/49以外のGTINも対象にする")
    ap.add_argument("--days", type=int, default=ROLLING_DAYS, help="対象日数（デフォルト: 3日）")
    ap.add_argument("--no-dedup", action="store_true", help="既取得item IDも対象にする")
    args = ap.parse_args()

    print(f"\n{'='*55}")
    print("  Terapeak 日次JANリサーチ")
    print(f"  アカウント: {args.account}")
    print(f"  対象日数  : {args.days}日")
    print(f"{'='*55}")

    # ── Terapeak スクレイピング ──────────────────
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    print("[初期化] Terapeakドライバー起動中...")
    driver = create_driver()
    try:
        set_ship_to_us(driver)
        rows = scrape_terapeak(driver, args.days)
    finally:
        driver.quit()

    print(f"\n[terapeak] 合計 {len(rows)} 件")
    if not rows:
        return

    # 既取得item IDを除外（日次差分）
    seen = set(json.loads(STATE_FILE.read_text())) if STATE_FILE.exists() else set()
    targets = rows if args.no_dedup else [r for r in rows if r["item_id"] not in seen]
    print(f"[diff] 新規 {len(targets)} 件 / 既取得 {len(rows) - len(targets)} 件")
    if not targets:
        return

    # ── Browse API で GTIN 照合 ──────────────────
    token = get_app_token()
    info, pending = fetch_gtins([r["item_id"] for r in targets], token)
    pending_set = set(pending)
    if pending_set:
        print(f"[browse] レート制限等により {len(pending_set)} 件は今回未照会 → 次回実行時に再照会されます")

    picked = []
    for r in targets:
        extra = info.get(r["item_id"], {})
        gtin = (extra.get("gtin") or "").strip()
        if not gtin:
            continue
        if not args.all_gtin and not JP_JAN_RE.match(gtin):
            continue
        r.update(extra)
        r["jan"] = gtin
        r["url"] = f"https://www.ebay.com/itm/{r['item_id']}"
        picked.append(r)

    # ── CSV監査ログ ──────────────────────────────
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(JST).strftime("%Y%m%d")
    out = OUT_DIR / f"terapeak_jan_{stamp}.csv"
    cols = ["jan", "title", "avg_sold_price", "avg_shipping", "total_sold",
            "total_sales", "date_last_sold", "brand", "mpn", "epid",
            "category", "seller", "item_id", "url"]
    with out.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(picked)

    seen.update(r["item_id"] for r in rows if r["item_id"] not in pending_set)
    STATE_FILE.write_text(json.dumps(sorted(seen)))

    print(f"\n[done] JAN付き {len(picked)} 件 / 照会 {len(targets)} 件 "
          f"(取得率 {len(picked)/max(len(targets),1):.0%})")
    print(f"[out]  {out}")

    # ── jan_research.py へ流し込み（いつもの4ステップ判定でGO/No-Go）──
    unique_jans = sorted({r["jan"] for r in picked})
    if not unique_jans:
        print("\n[jan_research] 対象JANなし。終了。")
        return

    print(f"\n[jan_research] ユニークJAN {len(unique_jans)}件を "
          f"{JAN_BATCH_SIZE}件ずつ jan_research.py へ送信します...")
    for i in range(0, len(unique_jans), JAN_BATCH_SIZE):
        batch = unique_jans[i:i + JAN_BATCH_SIZE]
        flush_jans(batch, args.account, args.dry_run, args.force)

    print(f"{'='*55}")
    print("  完了。スプレッドシートの「✅ GO」行が利益商品です。")
    print(f"{'='*55}\n")


if __name__ == "__main__":
    main()
