"""
price_adjuster.py
=================
競合最安値(J列)より $0.01 安く価格設定する。原価格は絶対に下回らない。

  自分が最安値でない場合: 競合最安値 - $0.01
  自分が最安値の場合    : 2番目のライバル(= 競合最安値) - $0.01

どちらのケースも J列 - $0.01 で統一。

使い方:
  python3 price_adjuster.py              # 全件実行
  python3 price_adjuster.py --dry-run    # 試算のみ
  python3 price_adjuster.py --limit 10   # 最大10件
"""

import sys
sys.stdout.reconfigure(line_buffering=True)

import time
import argparse
from config import CONFIG
from sheets_manager import SheetsManager
from ebay_checker import EbayChecker
from run import calc_sell_price


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="試算のみ（更新しない）")
    parser.add_argument("--limit",   type=int, default=0, help="処理件数上限（0=全件）")
    args = parser.parse_args()

    print("=" * 55)
    print(f"  {'[DRY RUN] ' if args.dry_run else ''}競合価格連動 価格調整")
    print("=" * 55)

    sheets = SheetsManager(CONFIG["SHEET_ID"])
    for key in ["TARGET_MARGIN", "EBAY_FEE_RATE", "TARIFF_RATE",
                "MIN_SELL_PRICE_USD", "PRICE_CHANGE_THRESHOLD"]:
        val = sheets.get_settings().get(key)
        if val:
            CONFIG[key] = val

    ebay    = EbayChecker(CONFIG["EBAY_TOKEN"])
    products = sheets.get_active_products()

    # J列（競合最安値）が入っている商品のみ対象
    targets = [p for p in products if str(p.get("競合最安値(USD)") or "").strip()]
    if args.limit > 0:
        targets = targets[:args.limit]

    print(f"  対象: {len(targets)}件（J列あり）\n")

    updated = skip = error = 0

    for i, product in enumerate(targets):
        identifier    = product.get("ASIN") or product.get("JANコード", "")
        ebay_id       = str(product.get("eBay商品ID", "")).strip()
        name          = product.get("商品名", "")[:40]
        current_price = float(product.get("eBay売値(USD)") or 0)
        rival_price   = float(product.get("競合最安値(USD)") or 0)

        print(f"[{i+1}/{len(targets)}] {name}")

        base_raw = product.get("仕入れ基準価格", "")
        if not str(base_raw).strip():
            print("  ⚠️  仕入れ基準価格未入力 → スキップ")
            skip += 1
            continue

        try:
            # 原価格（絶対下限）
            product_config = CONFIG.copy()
            margin_raw = product.get("利益率", "")
            if str(margin_raw).strip():
                try:
                    product_config["TARGET_MARGIN"] = float(str(margin_raw).strip())
                except ValueError:
                    pass
            min_raw = product.get("下限価格(USD)", "")
            product_min = float(min_raw) if str(min_raw).strip() else None
            cost_floor  = calc_sell_price(float(base_raw), product_config, min_price=product_min)

            # 目標価格 = 競合最安値 - $0.01（原価格下限を適用）
            target    = round(rival_price - 0.01, 2)
            new_price = max(target, cost_floor)

            if new_price > target:
                print(f"  競合${rival_price} - $0.01 = ${target} → 原価下限${cost_floor}を適用")
            else:
                print(f"  競合${rival_price} - $0.01 = ${new_price}")
            print(f"  現在: ${current_price}  →  新価格: ${new_price}")

            # 変動が閾値以下ならスキップ
            if current_price > 0:
                diff = abs(new_price - current_price) / current_price
                if diff <= CONFIG["PRICE_CHANGE_THRESHOLD"]:
                    print(f"  → 変動率 {diff:.1%} ≤ 閾値 → スキップ")
                    skip += 1
                    continue

            if not args.dry_run:
                if ebay_id:
                    ebay.revise_price(ebay_id, new_price)
                sheets.update_price(identifier, float(base_raw), new_price)
                print("  ✅ 更新完了")
            else:
                print("  [DRY RUN] 更新対象")

            updated += 1

        except Exception as e:
            print(f"  ❌ エラー: {e}")
            error += 1

        time.sleep(0.5)

    print(f"\n{'=' * 55}")
    print(f"  更新: {updated}件  スキップ: {skip}件  エラー: {error}件")
    if args.dry_run:
        print("  ※ DRY RUN のため実際の更新なし")
    print(f"{'=' * 55}")


if __name__ == "__main__":
    main()
