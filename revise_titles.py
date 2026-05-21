"""
出品済み商品のタイトルを80文字SEO最適化ロジックで一括更新

【使い方】
  python revise_titles.py              # 商品マスタの全「出品中」商品を更新
  python revise_titles.py --dry-run    # 更新せず生成タイトルだけ確認
  python revise_titles.py --limit 10  # 最大10件だけ処理

【フロー】
  1. 商品マスタから eBay商品ID・ASIN を取得
  2. Keepa で日本語タイトル・ブランド・モデルを再取得
  3. translate_title_for_ebay() で80文字タイトルを生成
  4. eBay ReviseItem API でタイトルを更新
"""

import argparse
import time
import requests
import xml.etree.ElementTree as ET
from datetime import datetime

from config import CONFIG
from sheets_manager import SheetsManager
from ebay_lister import translate_title_for_ebay, fetch_listing_details
from keepa_checker import KeepaChecker


EBAY_API_URL = "https://api.ebay.com/ws/api.dll"


def revise_title(token: str, ebay_item_id: str, new_title: str) -> dict:
    """eBay ReviseItem API でタイトルのみ更新"""
    title_escaped = (new_title
                     .replace("&", "&amp;")
                     .replace("<", "&lt;")
                     .replace(">", "&gt;")
                     .replace('"', "&quot;"))
    xml_body = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<ReviseItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">'
        "<RequesterCredentials>"
        f"<eBayAuthToken>{token}</eBayAuthToken>"
        "</RequesterCredentials>"
        "<Item>"
        f"<ItemID>{ebay_item_id}</ItemID>"
        f"<Title>{title_escaped}</Title>"
        "</Item>"
        "</ReviseItemRequest>"
    )
    try:
        resp = requests.post(
            EBAY_API_URL,
            headers={
                "X-EBAY-API-SITEID":              "0",
                "X-EBAY-API-COMPATIBILITY-LEVEL": "967",
                "X-EBAY-API-IAF-TOKEN":           token,
                "X-EBAY-API-CALL-NAME":           "ReviseItem",
                "Content-Type":                   "text/xml",
            },
            data=xml_body.encode("utf-8"),
            timeout=30,
        )
        root = ET.fromstring(resp.text)
        ns = {"e": "urn:ebay:apis:eBLBaseComponents"}
        ack = root.findtext("e:Ack", namespaces=ns)
        if ack in ("Success", "Warning"):
            return {"success": True, "message": "更新成功"}
        else:
            errors = root.findall(".//e:ShortMessage", ns)
            msg = " / ".join(e.text for e in errors if e.text) or "不明なエラー"
            return {"success": False, "message": msg}
    except Exception as e:
        return {"success": False, "message": str(e)}


def main():
    parser = argparse.ArgumentParser(description="出品済み商品タイトル一括更新")
    parser.add_argument("--dry-run", action="store_true", help="更新せずタイトル生成だけ確認")
    parser.add_argument("--limit",   type=int, default=0, help="最大処理件数（0=全件）")
    args = parser.parse_args()

    print("=" * 60)
    print(f"✏️  タイトル一括更新開始: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    if args.dry_run:
        print("  ⚠️  DRY RUNモード（実際には更新しません）")
    print("=" * 60)

    sheets = SheetsManager(CONFIG["SHEET_ID"])
    keepa  = KeepaChecker(CONFIG["KEEPA_API_KEY"])

    products = sheets.get_active_products()
    # eBay商品IDがある出品中商品のみ対象
    targets = [p for p in products if p.get("eBay商品ID") and p.get("ASIN")]
    if args.limit:
        targets = targets[:args.limit]

    print(f"\n📋 更新対象: {len(targets)} 件\n")

    success_count = 0
    skip_count    = 0
    fail_count    = 0

    for i, product in enumerate(targets):
        asin      = str(product.get("ASIN", "")).strip()
        ebay_id   = str(product.get("eBay商品ID", "")).strip()
        old_name  = str(product.get("商品名", "")).strip()

        print(f"[{i+1}/{len(targets)}] eBay: {ebay_id} / ASIN: {asin}")
        print(f"  📦 現在商品名: {old_name[:60]}")

        # Keepa で最新データ取得（日本語タイトル・ブランド・モデル）
        keepa_data = fetch_listing_details(keepa, asin)
        if not keepa_data:
            print(f"  ⛔ スキップ（Keepaデータ取得失敗）")
            skip_count += 1
            time.sleep(1)
            continue

        jp_title = keepa_data.get("title", "") or old_name
        brand    = keepa_data.get("brand", "")
        model    = keepa_data.get("model", "") or keepa_data.get("mpn", "")

        # 80文字SEO最適化タイトル生成
        new_title = translate_title_for_ebay(jp_title, brand=brand, model=model)

        if not new_title:
            print(f"  ⛔ スキップ（タイトル生成失敗）")
            skip_count += 1
            time.sleep(1)
            continue

        if args.dry_run:
            print(f"  ✅ [DRY RUN] 新タイトル: {new_title}")
            success_count += 1
            time.sleep(0.5)
            continue

        # ReviseItem で更新
        result = revise_title(CONFIG["EBAY_TOKEN"], ebay_id, new_title)
        if result["success"]:
            print(f"  ✅ 更新成功: {new_title}")
            success_count += 1
        else:
            print(f"  ❌ 更新失敗: {result['message']}")
            fail_count += 1

        time.sleep(1)  # API制限対策

    print("\n" + "=" * 60)
    print(f"✅ 完了: 成功 {success_count} 件 / スキップ {skip_count} 件 / 失敗 {fail_count} 件")
    print("=" * 60)


if __name__ == "__main__":
    main()
