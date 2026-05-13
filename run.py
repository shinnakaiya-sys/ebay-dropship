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
        print(f"   処理可能な商品数: {keepa.tokens_left // MIN_TOKENS_CHECK} 件 → 可能な分だけ処理します")
    if keepa.tokens_left < MIN_TOKENS_CHECK:
        print("   トークンが1件分もないため終了します")
        sheets.write_summary(len(products), [])
        return

    alerts = []  # 要対応アラートを収集

    for i, product in enumerate(products):
        asin        = product["ASIN"]
        ebay_id     = product["eBay商品ID"]
        base_price_raw = product.get("仕入れ基準価格", "")
        if not str(base_price_raw).strip():
            print(f"\n[{i+1}/{len(products)}] {product['商品名'][:40]}...")
            print(f"  ⚠️  仕入れ基準価格が未入力のためスキップ")
            continue
        base_price  = float(base_price_raw)
        # 商品ごとの下限価格（空欄=グローバル設定を使用）
        min_price_raw = product.get("下限価格(USD)", "")
        product_min_price = float(min_price_raw) if str(min_price_raw).strip() else None
        print(f"\n[{i+1}/{len(products)}] {product['商品名'][:40]}...")

        # トークン残量チェック（1商品分なければ中断）
        if keepa.tokens_left < MIN_TOKENS_CHECK:
            print(f"  ⚠️  Keepaトークン不足（残: {keepa.tokens_left}）→ 残り商品をスキップ")
            break

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

        # 計算売値・eBay価格ズレを事前算出
        new_price = calc_sell_price(amazon_price, CONFIG, min_price=product_min_price)
        ebay_price_stale = (
            ebay_price > 0
            and abs(ebay_price - new_price) / new_price > CONFIG["PRICE_CHANGE_THRESHOLD"]
        )

        # ──────────────────────────────────────
        # 3. 競合（日本発送）最安値を取得
        # ──────────────────────────────────────
        rival = ebay.get_jp_lowest_price(
            product.get("JANコード", ""),
            CONFIG.get("EBAY_APP_ID", ""),
            client_secret=CONFIG.get("EBAY_CLIENT_SECRET", ""),
            exclude_item_id=ebay_id,
        )
        sheets.update_rival_price(asin, rival["lowest_price"], rival["count"])
        if rival["lowest_price"] > 0:
            print(f"  🏷️  競合最安値: ${rival['lowest_price']} (出品数:{rival['count']})")
        else:
            print(f"  🏷️  競合なし or 取得不可")
        time.sleep(1)  # APIレートリミット対策

        # ──────────────────────────────────────
        # 4. 判定ロジック
        # ──────────────────────────────────────

        # ケース①: Amazon在庫切れ → eBay出品停止
        if not amazon_in_stock and ebay_active:
            ebay.end_listing(ebay_id)
            sheets.update_status(asin, "在庫切れ（在庫0）")
            alerts.append({
                "type": "⛔ 在庫切れ",
                "asin": asin,
                "ebay_id": ebay_id,
                "message": "Amazon在庫切れ → eBay在庫数0に更新",
                "product": product.get("商品名", "")[:40],
            })
            print(f"  ⛔ 在庫切れ → eBay在庫数0に更新")
            continue

        # ケース②: Amazon在庫復活 → eBay出品再開
        if amazon_in_stock and not ebay_active:
            new_price = calc_sell_price(amazon_price, CONFIG, min_price=product_min_price)
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

        # ケース③: Amazon価格変動 or eBay価格ズレ → eBay価格更新
        if amazon_in_stock and ebay_active and (price_changed or ebay_price_stale):
            ebay.revise_price(ebay_id, new_price)
            sheets.update_price(asin, amazon_price, new_price)
            reason = "Amazon価格変動" if price_changed else f"eBay価格ズレ(${ebay_price}→${new_price})"
            alerts.append({
                "type": "💰 価格変動",
                "asin": asin,
                "ebay_id": ebay_id,
                "message": f"{reason} → eBay更新: ${new_price}",
                "product": product["商品名"][:40],
            })
            print(f"  💰 {reason} → eBay価格更新")
            continue

        sheets.update_price(asin, amazon_price, new_price)
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
def calc_sell_price(amazon_price_jpy, config, weight_kg: float = 1.0, min_price: float = None):
    """
    Amazon円価格 → eBayドル売値を計算（SpeedPAK実送料を使用）

    考慮する要素:
      - 為替レート
      - eBay手数料
      - 国際送料（SpeedPAK Economy, US48）
      - 目標利益率
    """
    from shipping_calculator import get_shipping_jpy
    shipping_jpy = get_shipping_jpy(weight_kg, destination="US48")
    product_usd  = amazon_price_jpy / config["JPY_TO_USD"] * (1 + config["TARIFF_RATE"])
    shipping_usd = shipping_jpy / config["JPY_TO_USD"]
    usd = (product_usd + shipping_usd) / (1 - config["EBAY_FEE_RATE"])
    usd = usd / (1 - config["TARGET_MARGIN"])
    # 商品個別の下限価格（指定なしはグローバル設定を使用）
    floor = min_price if min_price is not None else config.get("MIN_SELL_PRICE_USD", 0)
    if floor and usd < floor:
        print(f"  ⚠️  計算売値 ${usd:.2f} < 下限 ${floor} → 下限価格を適用")
        usd = floor
    return round(usd, 2)


if __name__ == "__main__":
    main()
