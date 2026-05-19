"""
損益分岐点価格を商品マスタのK列（下限価格）に一括入力するスクリプト

損益分岐点 = 利益率0%での計算売値（仕入れ原価 + 送料 + eBay手数料 + 関税を回収するだけの価格）

【実行】
  python set_breakeven_prices.py

【動作】
  - K列が空欄の商品にのみ書き込む（手動設定済みは上書きしない）
  - --overwrite オプションで全商品を上書き可能
"""

import sys
import time
import argparse
from config import CONFIG
from sheets_manager import SheetsManager, SHEET_MASTER, MASTER_COLS
from shipping_calculator import get_shipping_jpy


def calc_breakeven(amazon_price_jpy: float, config: dict, weight_kg: float = 1.0) -> float:
    """
    損益分岐点価格（USD）を計算する。
    利益率0%・下限価格制約なしで calc_sell_price と同じ計算式。
    """
    shipping_jpy = get_shipping_jpy(weight_kg, destination="US48")
    product_usd  = amazon_price_jpy / config["JPY_TO_USD"] * (1 + config["TARIFF_RATE"])
    shipping_usd = shipping_jpy / config["JPY_TO_USD"]
    usd = (product_usd + shipping_usd) / (1 - config["EBAY_FEE_RATE"])
    return round(usd, 2)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--overwrite", action="store_true",
                        help="K列に既存の値があっても上書きする")
    args = parser.parse_args()

    print("=" * 60)
    print("損益分岐点価格 → K列（下限価格）一括入力")
    print("=" * 60)

    sheets = SheetsManager(CONFIG["SHEET_ID"])

    # 設定シートから手数料率などを上書き
    settings = sheets.get_settings()
    for key in ["EBAY_FEE_RATE", "TARIFF_RATE"]:
        if key in settings:
            CONFIG[key] = settings[key]
    print(f"  手数料: {CONFIG['EBAY_FEE_RATE']*100:.0f}%  関税: {CONFIG['TARIFF_RATE']*100:.0f}%  為替: {CONFIG['JPY_TO_USD']:.1f}")

    ws = sheets.sheet.worksheet(SHEET_MASTER)
    records = ws.get_all_records(expected_headers=MASTER_COLS)

    k_col = MASTER_COLS.index("下限価格(USD)") + 1  # 1始まり列番号

    updated = 0
    skipped = 0
    no_price = 0
    batch = []  # バッチ書き込み用 [(row, value), ...]

    for i, row in enumerate(records, start=2):  # 1行目はヘッダー
        name       = row.get("商品名", "")[:35]
        base_raw   = row.get("仕入れ基準価格", "")
        current_k  = row.get("下限価格(USD)", "")

        # 仕入れ基準価格が未入力はスキップ
        if not str(base_raw).strip():
            no_price += 1
            continue

        # --overwrite なし かつ K列に値があればスキップ
        if not args.overwrite and str(current_k).strip():
            skipped += 1
            continue

        base_price = float(base_raw)
        breakeven  = calc_breakeven(base_price, CONFIG)
        batch.append((i, breakeven, name, base_price))
        updated += 1

    # バッチ書き込み（1回のAPIコールでまとめて更新）
    if batch:
        col_letter = chr(ord("A") + k_col - 1)
        cell_updates = [
            {
                "range": f"{col_letter}{row_i}",
                "values": [[val]],
            }
            for row_i, val, _, _ in batch
        ]
        ws.batch_update(cell_updates)
        for row_i, val, name, base in batch:
            print(f"  行{row_i:3d} ¥{base:,.0f} → ${val:.2f}  {name}")

    print()
    print(f"✅ 完了: {updated}件 更新 / {skipped}件 スキップ（既存値あり）/ {no_price}件 仕入れ価格なし")
    if skipped > 0:
        print(f"   ※ --overwrite オプションで既存値も上書きできます")


if __name__ == "__main__":
    main()
