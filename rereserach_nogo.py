"""
rereserach_nogo.py
==================
「新品リサーチ」タブで L列が「No-Go」の商品を再リサーチする。

処理内容:
  1. スプレッドシートを読み込み、L列が「No-Go」の行を抽出
  2. A列の JANコードで Keepa から最新仕入れ価格を取得
  3. eBay 最安値（新品・価格+送料最安順）を検索
  4. 利益計算を再実行
  5. 該当行を上書き更新（F=eBay価格, J=仕入れ価格, L=判定, M=重量, N=送料, O=利益, R=更新日, S=再リサーチ済）
     ※ S列に「再リサーチ済」を書き込むことで、次回実行時に同じ行は処理されない

列構成（jan_research.py write_to_sheet と同じ）:
  A=JAN, B=商品名, C=タイトル(eBay), D=カテゴリ, E=コンディション,
  F=販売価格(USD), G=送料, H=返品, I=仕入れ先, J=仕入れ価格(JPY),
  K=月間Sold数, L=判定, M=請求重量(kg), N=送料(JPY), O=利益(JPY),
  P=還付金額(JPY), Q=アイテムスペシフィクス, R=作成日, S=ステータス, T=仕入れ先URL

使い方:
  python3 rereserach_nogo.py
  python3 rereserach_nogo.py --dry-run
  python3 rereserach_nogo.py --limit 5   # 最大N件のみ処理
"""

import os
import sys
import time
from datetime import datetime

import gspread
from oauth2client.service_account import ServiceAccountCredentials
from dotenv import load_dotenv

load_dotenv()

# jan_research.py から共通関数・定数をインポート
from jan_research import (
    get_usd_jpy_rate,
    get_keepa_info,
    get_ebay_active_lowest,
    _ascii_keywords,
    _create_ebay_driver,
    _get_keepa_api_key,
    calc_profit,
    parse_jpy,
    SPREADSHEET_ID,
    TAB_NAME,
    JSON_FILE,
    SCOPE,
)

# 列インデックス（0始まり）
COL_JAN      = 0   # A: JANコード
COL_NAME     = 1   # B: 商品名
COL_EBAY_USD = 5   # F: 販売価格(USD)
COL_PURCHASE = 9   # J: 仕入れ価格(JPY)
COL_SOLD     = 10  # K: 月間Sold数
COL_JUDGE    = 11  # L: 判定
COL_WEIGHT   = 12  # M: 請求重量(kg)
COL_SHIP     = 13  # N: 送料(JPY)
COL_PROFIT   = 14  # O: 利益(JPY)
COL_DATE     = 17  # R: 作成日/更新日
COL_STATUS   = 18  # S: ステータス
COL_SRC_URL  = 19  # T: 仕入れ先URL

RERESEARCH_DONE = "再リサーチ済"


def _safe_get(row: list, idx: int, default: str = "") -> str:
    return row[idx].strip() if idx < len(row) else default


def find_nogo_rows(ws) -> list[tuple[int, list]]:
    """L列が No-Go かつ S列が「再リサーチ済」でない行を返す（1始まり行番号）。"""
    all_rows = ws.get_all_values()
    nogo = []
    for i, row in enumerate(all_rows):
        jan = _safe_get(row, COL_JAN)
        # ヘッダー行（JANコードでない行）はスキップ
        if not (jan.isdigit() and len(jan) >= 10):
            continue
        # 再リサーチ済の行はスキップ
        if _safe_get(row, COL_STATUS) == RERESEARCH_DONE:
            continue
        val = _safe_get(row, COL_JUDGE)
        if "No-Go" in val or "no-go" in val.lower() or "Noーgo" in val:
            nogo.append((i + 1, row))  # gspread は 1始まり
    return nogo


def update_row(ws, row_num: int, ebay_usd: float, amazon_price_str: str,
               amazon_url: str | None, profit: dict, dry_run: bool):
    """指定行の F/J/L/M/N/O/R 列を一括更新する。"""
    today    = datetime.now().strftime("%Y-%m-%d")
    judgment = "✅ GO" if profit["is_go"] else "❌ No-Go"
    ebay_str = f"${ebay_usd:.2f}" if ebay_usd else ""

    batch = [
        {"range": f"F{row_num}", "values": [[ebay_str]]},
        {"range": f"J{row_num}", "values": [[amazon_price_str]]},
        {"range": f"L{row_num}", "values": [[judgment]]},
        {"range": f"M{row_num}", "values": [[profit["weight_kg"]]]},
        {"range": f"N{row_num}", "values": [[profit["shipping_jpy"]]]},
        {"range": f"O{row_num}", "values": [[profit["profit_jpy"]]]},
        {"range": f"R{row_num}", "values": [[today]]},
        {"range": f"S{row_num}", "values": [[RERESEARCH_DONE]]},
    ]
    if amazon_url:
        batch.append({"range": f"T{row_num}", "values": [[amazon_url]]})

    if dry_run:
        print(f"  [DRY-RUN] 行{row_num}: eBay={ebay_str} | {judgment} | "
              f"利益=¥{profit['profit_jpy']:,}")
        return

    ws.batch_update(batch, value_input_option="USER_ENTERED")
    time.sleep(0.5)


def main():
    args    = sys.argv[1:]
    dry_run = "--dry-run" in args
    limit   = None
    for i, arg in enumerate(args):
        if arg == "--limit" and i + 1 < len(args):
            try:
                limit = int(args[i + 1])
            except ValueError:
                pass
            break
        if arg.startswith("--limit="):
            try:
                limit = int(arg.split("=", 1)[1])
            except ValueError:
                pass

    mode = "[DRY-RUN]" if dry_run else "[本番]"
    print(f"\n{'='*55}")
    print(f"  No-Go 再リサーチ {mode}")
    print(f"{'='*55}")

    print("[初期化] 為替レート取得中...")
    rate = get_usd_jpy_rate()
    print(f"  ✅ USD/JPY: ¥{rate:.1f}")

    print("[初期化] スプレッドシート接続中...")
    creds  = ServiceAccountCredentials.from_json_keyfile_name(JSON_FILE, SCOPE)
    client = gspread.authorize(creds)
    ws     = client.open_by_key(SPREADSHEET_ID).worksheet(TAB_NAME)
    print(f"  ✅ 接続成功（タブ: {TAB_NAME}）")

    print("[スキャン] No-Go 行を検索中...")
    nogo_rows = find_nogo_rows(ws)
    print(f"  → {len(nogo_rows)} 件の No-Go 行を発見")

    if not nogo_rows:
        print("  No-Go 行がありません。終了します。")
        return

    if limit:
        nogo_rows = nogo_rows[:limit]
        print(f"  → --limit {limit} 件のみ処理")

    keepa_key = _get_keepa_api_key()
    if keepa_key:
        print("[初期化] Keepa APIキー取得済み")
    else:
        print("[初期化] ⚠️  KEEPA_API_KEY 未設定（シート既存値を使用）")

    print("[初期化] eBay 検索ドライバー起動中...")
    ebay_driver = None
    try:
        ebay_driver = _create_ebay_driver()
        print("  ✅ eBay 検索ドライバー起動完了")
    except Exception as e:
        print(f"  ⚠️  eBay 検索ドライバー起動失敗: {e}")

    summary = {"go": [], "still_nogo": [], "skipped": []}
    try:
        for row_num, row_data in nogo_rows:
            jan = _safe_get(row_data, COL_JAN)
            print(f"\n{'─'*55}")
            print(f"  行 {row_num} / JAN: {jan}")
            print(f"{'─'*55}")

            # Keepa で最新の仕入れ価格・重量を取得
            amazon_title, amazon_price_str, amazon_url, weight_kg = (
                get_keepa_info(jan, keepa_key) if keepa_key
                else (None, None, None, None)
            )

            # Keepa 失敗時はシートの既存値を使用
            if not amazon_price_str:
                amazon_price_str = _safe_get(row_data, COL_PURCHASE)
                if amazon_price_str:
                    print(f"  [Keepa] 価格取得失敗 → シート既存値: {amazon_price_str}")
                else:
                    print("  [Keepa] 価格取得失敗かつシート既存値なし → スキップ")
                    summary["skipped"].append(jan)
                    continue

            if not amazon_title:
                amazon_title = _safe_get(row_data, COL_NAME)

            if weight_kg is None:
                try:
                    w = _safe_get(row_data, COL_WEIGHT)
                    weight_kg = float(w) if w else None
                except ValueError:
                    weight_kg = None

            amazon_jpy = parse_jpy(amazon_price_str)
            print(f"  仕入れ価格: {amazon_price_str}  重量: "
                  f"{weight_kg:.3f} kg" if weight_kg else f"  仕入れ価格: {amazon_price_str}  重量: 不明")

            # eBay 最安値取得（新品・価格+送料最安順）
            print("  [eBay] 最安値検索中（新品）...")
            title_kw = _ascii_keywords(amazon_title or "")
            ebay_usd, ebay_url = get_ebay_active_lowest(jan, ebay_driver, title_kw=title_kw)

            if ebay_usd:
                print(f"  → 最安値: ${ebay_usd:.2f}")
                if ebay_url:
                    print(f"  → URL: {ebay_url[:70]}")
            else:
                print("  → eBay 出品なし（価格 $0 で計算）")

            # 利益計算
            profit = calc_profit(ebay_usd, amazon_jpy, rate, weight_kg)
            judgment = "✅ GO" if profit["is_go"] else "❌ No-Go"
            print(f"  売上: ¥{profit['revenue_jpy']:,} / 送料: ¥{profit['shipping_jpy']:,} "
                  f"/ 利益: ¥{profit['profit_jpy']:,} → {judgment}")

            # シート更新
            update_row(ws, row_num, ebay_usd, amazon_price_str,
                       amazon_url, profit, dry_run)

            if profit["is_go"]:
                summary["go"].append(jan)
            else:
                summary["still_nogo"].append(jan)

            time.sleep(1)

    finally:
        if ebay_driver:
            try:
                ebay_driver.quit()
            except Exception:
                pass
            print("\n  eBay 検索ドライバーを終了しました。")

    print(f"\n{'='*55}")
    print(f"  完了サマリー")
    print(f"{'='*55}")
    print(f"  ✅ GO に変化   : {len(summary['go'])}件  {summary['go']}")
    print(f"  ❌ まだ No-Go  : {len(summary['still_nogo'])}件")
    print(f"  ⏭  スキップ   : {len(summary['skipped'])}件  {summary['skipped']}")
    if not dry_run and summary["go"]:
        print(f"\n  「{TAB_NAME}」タブの該当行を更新しました。")
    print(f"{'='*55}\n")


if __name__ == "__main__":
    main()
