"""
refresh_amazon_prices.py
========================
商品マスタの既存出品商品に対して、Keepa APIでAmazon.co.jpの
最新価格を取得しE列（仕入れ基準価格）を更新する。

使い方:
  python3 refresh_amazon_prices.py              # 全件
  python3 refresh_amazon_prices.py --limit 10   # 最大10件
  python3 refresh_amazon_prices.py --dry-run    # 試算のみ（シート更新なし）
"""

import sys
sys.stdout.reconfigure(line_buffering=True)

import argparse
import time
import requests
from config import CONFIG
from sheets_manager import SheetsManager


def _fetch_keepa_price(identifier: str, is_asin: bool, api_key: str) -> tuple[int, str | None]:
    """
    Keepa APIでAmazon.co.jpの現在価格と商品名を取得する。
    history=0, stats=1 でトークン消費を最小化。

    Returns:
        (price_jpy, title) — 取得失敗時は (0, None)
    """
    params = {
        "key":     api_key,
        "domain":  5,   # Amazon Japan
        "stats":   1,
        "history": 0,
    }
    if is_asin:
        params["asin"] = identifier
    else:
        params["code"] = identifier  # JAN/EANコード検索

    try:
        resp = requests.get(
            "https://api.keepa.com/product",
            params=params,
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        products = data.get("products", [])
        if not products:
            return 0, None

        p       = products[0]
        title   = p.get("title") or None
        current = (p.get("stats") or {}).get("current") or []

        # index 0: Amazon直販価格 / index 1: マーケットプレイス新品最安値
        price = 0
        for idx in [0, 1]:
            if len(current) > idx and current[idx] and current[idx] > 0:
                price = current[idx]
                break

        return price, title

    except Exception as e:
        print(f"  [Keepa] エラー: {e}")
        return 0, None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="試算のみ（シート更新なし）")
    parser.add_argument("--limit",   type=int, default=0,  help="処理件数上限（0=全件）")
    args = parser.parse_args()

    api_key = CONFIG.get("KEEPA_API_KEY")
    if not api_key:
        print("❌ KEEPA_API_KEY が未設定です（.env を確認してください）")
        sys.exit(1)

    print("=" * 55)
    mode = "[DRY RUN]" if args.dry_run else "[本番]"
    print(f"  Amazon価格再取得 {mode}")
    print("=" * 55)

    print("[初期化] スプレッドシート接続中...")
    sheets = SheetsManager(CONFIG["SHEET_ID"])
    print("  ✅ 接続成功\n")

    products = sheets.get_active_products()
    targets  = [p for p in products if p.get("ASIN") or p.get("JANコード")]
    # 最終チェック日が古い順に並べ替え（前回スキップされた商品を優先）
    targets.sort(key=lambda p: p.get("最終チェック日", "") or "")

    if args.limit > 0:
        targets = targets[:args.limit]
        print(f"  ※ --limit {args.limit} 件のみ処理")

    print(f"  対象: {len(targets)} 件\n")
    print("-" * 55)

    updated = skip = error = 0

    for i, product in enumerate(targets):
        asin      = str(product.get("ASIN", "")).strip()
        jan       = str(product.get("JANコード", "")).strip()
        name      = product.get("商品名", "")[:40]
        old_price = str(product.get("仕入れ基準価格", "")).strip()
        identifier = asin or jan

        print(f"[{i+1}/{len(targets)}] {name or identifier}")

        if asin:
            price, title = _fetch_keepa_price(asin, is_asin=True,  api_key=api_key)
        else:
            price, title = _fetch_keepa_price(jan,  is_asin=False, api_key=api_key)

        if price <= 0:
            print(f"  ⚠️  価格取得失敗 → スキップ")
            skip += 1
            time.sleep(1)
            continue

        old_price_int = int(old_price) if old_price.isdigit() else 0
        old_str = f"¥{old_price_int:,}" if old_price_int else (old_price or "(未設定)")
        print(f"  仕入れ価格: {old_str} → ¥{price:,}")
        if title and not name:
            print(f"  商品名取得: {title[:60]}")

        if price == old_price_int:
            print("  ℹ️  価格変更なし → タイムスタンプ更新スキップ")
            skip += 1
            time.sleep(1)
            continue

        if not args.dry_run:
            try:
                ok = sheets.update_amazon_price(identifier, price)
                if ok:
                    print("  ✅ 更新完了")
                    updated += 1
                else:
                    print("  ⚠️  シートのセルが見つからない → スキップ")
                    skip += 1
            except Exception as e:
                print(f"  ❌ 更新エラー: {e}")
                error += 1
        else:
            print("  [DRY RUN] 更新対象")
            updated += 1

        time.sleep(1)

    print(f"\n{'='*55}")
    print(f"  完了: 更新 {updated}件  スキップ {skip}件  エラー {error}件")
    if args.dry_run:
        print("  ※ DRY RUN のため実際の更新なし")
    print("=" * 55)


if __name__ == "__main__":
    main()
