"""
無在庫ドロップシッピング 毎日チェックシステム
Amazon.co.jp（Keepa）× eBay × Google Sheets

【ファイル構成】
  main.py               ← このファイル（メイン実行）
  keepa_checker.py      ← Keepa価格・在庫チェック
  ebay_checker.py       ← eBay価格・在庫チェック
  sheets_manager.py     ← Google Sheets管理
  notifier.py           ← Slack/LINE通知
  config.py             ← 設定

【セットアップ】
  pip install keepa requests gspread google-auth pandas python-dotenv

【実行】
  python main.py           # 手動実行
  # または GitHub Actions / cron で毎日自動実行
"""

import time
from datetime import datetime
from config import CONFIG
from keepa_checker import KeepaChecker
from ebay_checker import EbayChecker
from sheets_manager import SheetsManager
from notifier import Notifier


def main():
    print("=" * 60)
    print(f"🚀 毎日チェック開始: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 60)

    # 各モジュール初期化
    sheets = SheetsManager(CONFIG["SHEET_ID"])
    keepa = KeepaChecker(CONFIG["KEEPA_API_KEY"])
    ebay = EbayChecker(CONFIG["EBAY_TOKEN"])
    notifier = Notifier(CONFIG)

    # Keepaトークンチェック（商品数 × 最低必要トークン）
    from keepa_checker import MIN_TOKENS_CHECK
    products = sheets.get_active_products()
    print(f"\n📋 管理商品数: {len(products)} 件")

    required = len(products) * MIN_TOKENS_CHECK
    if keepa.tokens_left < required:
        print(f"⚠️  Keepaトークン不足（残: {keepa.tokens_left} / 推奨: {required}）")
        print("   トークン回復後に再実行してください（1トークン/秒で回復）")
        sheets.write_summary(len(products), [])
        return

    alerts = []  # 要対応アラートを収集

    for i, product in enumerate(products):
        asin        = product["ASIN"]
        ebay_id     = product["eBay商品ID"]
        base_price  = float(product["仕入れ基準価格"])
        print(f"\n[{i+1}/{len(products)}] {product['商品名'][:40]}...")

        # ──────────────────────────────────────
        # 1. Keepa: Amazon価格・在庫チェック
        # ──────────────────────────────────────
        keepa_data = keepa.check(asin)

        amazon_price    = keepa_data["current_price"]
        amazon_in_stock = keepa_data["in_stock"]
        price_changed   = abs(amazon_price - base_price) / base_price > CONFIG["PRICE_CHANGE_THRESHOLD"]

        # Sheetsに記録
        sheets.log_price(asin, "amazon", amazon_price, amazon_in_stock)

        # ──────────────────────────────────────
        # 2. eBay: 現在の出品状態チェック
        # ──────────────────────────────────────
        ebay_data      = ebay.check(ebay_id)
        ebay_price     = ebay_data["current_price"]
        ebay_active    = ebay_data["is_active"]

        # Sheetsに記録
        sheets.log_price(asin, "ebay", ebay_price, ebay_active)

        # ──────────────────────────────────────
        # 3. 判定ロジック
        # ──────────────────────────────────────

        # ケース①: Amazon在庫切れ → eBay出品停止
        if not amazon_in_stock and ebay_active:
            ebay.end_listing(ebay_id)
            sheets.update_status(asin, "在庫切れ停止")
            alerts.append({
                "type": "⛔ 在庫切れ",
                "asin": asin,
                "ebay_id": ebay_id,
                "message": "Amazon在庫切れ → eBay出品停止",
                "product": product["商品名"][:40],
            })
            print(f"  ⛔ 在庫切れ → eBay出品停止")
            continue

        # ケース②: Amazon在庫復活 → eBay出品再開
        if amazon_in_stock and not ebay_active:
            new_price = calc_sell_price(amazon_price, CONFIG)
            ebay.relist(ebay_id, new_price)
            sheets.update_status(asin, "出品中")
            alerts.append({
                "type": "✅ 在庫復活",
                "asin": asin,
                "ebay_id": ebay_id,
                "message": f"在庫復活 → eBay再出品 ¥{new_price}",
                "product": product["商品名"][:40],
            })
            print(f"  ✅ 在庫復活 → eBay再出品")
            continue

        # ケース③: Amazon価格変動 → eBay価格更新
        if amazon_in_stock and ebay_active and price_changed:
            new_price = calc_sell_price(amazon_price, CONFIG)
            ebay.revise_price(ebay_id, new_price)
            sheets.update_price(asin, amazon_price, new_price)
            alerts.append({
                "type": "💰 価格変動",
                "asin": asin,
                "ebay_id": ebay_id,
                "message": f"Amazon: ¥{amazon_price} → eBay更新: ${new_price}",
                "product": product["商品名"][:40],
            })
            print(f"  💰 価格変動 → eBay価格更新 (¥{amazon_price})")
            continue

        print(f"  ✔  変動なし（Amazon: ¥{amazon_price} / 在庫: {'あり' if amazon_in_stock else 'なし'}）")
        time.sleep(0.5)  # API制限対策

    # ──────────────────────────────────────
    # 4. アラート通知 & サマリー記録
    # ──────────────────────────────────────
    if alerts:
        notifier.send(alerts)

    sheets.write_summary(len(products), alerts)

    print("\n" + "=" * 60)
    print(f"✅ 完了: {len(alerts)} 件のアクション実行")
    print("=" * 60)


# ──────────────────────────────────────────────────────────
# 売値計算（eBayドル建て）
# ──────────────────────────────────────────────────────────
def calc_sell_price(amazon_price_jpy, config):
    """
    Amazon円価格 → eBayドル売値を計算

    考慮する要素:
      - 為替レート
      - eBay手数料
      - 国際送料
      - 目標利益率
    """
    usd = amazon_price_jpy / config["JPY_TO_USD"]
    usd += config["SHIPPING_USD"]                     # 送料加算
    usd = usd / (1 - config["EBAY_FEE_RATE"])        # eBay手数料を乗せる
    usd = usd / (1 - config["TARGET_MARGIN"])         # 目標利益率を乗せる
    return round(usd, 2)


if __name__ == "__main__":
    main()
