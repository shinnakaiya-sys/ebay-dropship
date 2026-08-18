"""
出品がeBay検索に実際にヒットするか確認する診断スクリプト

Browse API(item_summary/search)を自分のseller_idでフィルタして叩き、
指定したItemIDが検索結果に出現するかを確認する。
GetMyeBaySelling上は「Active」でも、重複出品ポリシー等により検索から
除外されている（=impressionsが恒久的に0になる）ケースを切り分けるのが目的。

【使い方】
  python ebay_check_search_visibility.py --csv fuseki_test.csv --sample 15
  python ebay_check_search_visibility.py --item-ids 318655238911,318657329274
"""

import argparse
import csv as _csv
import random
import re
import time

import requests

from config import CONFIG
from ebay_lister import _get_ebay_app_token

BROWSE_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"

# eBayタイトルによく入るコンディション語（検索クエリからは除いてノイズを減らす）
_STOPWORDS = {"used", "new", "japanese", "japan", "genuine", "authentic", "f/s", "rare"}


def _build_query(title: str, n_words: int = 4) -> str:
    """タイトルから検索クエリ用に有効そうな単語をn個抜き出す（記号除去・ストップワード除外）"""
    words = re.findall(r"[A-Za-z0-9\-]+", title)
    kept = [w for w in words if w.lower() not in _STOPWORDS]
    return " ".join(kept[:n_words])


def check_visibility(item_id: str, title: str, app_token: str, seller_id: str) -> dict:
    """seller_idフィルタ付きでtitleのキーワード検索を行い、item_idが結果に出現するか確認する"""
    query = _build_query(title)
    resp = requests.get(
        BROWSE_URL,
        headers={"Authorization": f"Bearer {app_token}"},
        params={
            "q":      query,
            "filter": f"sellers:{{{seller_id}}}",
            "limit":  50,
        },
        timeout=15,
    )
    if resp.status_code != 200:
        return {"item_id": item_id, "query": query, "found": None, "total": None,
                "error": f"HTTP {resp.status_code} {resp.text[:150]}"}

    data = resp.json()
    total = data.get("total", 0)
    hits = data.get("itemSummaries", [])
    found = any(h.get("legacyItemId") == item_id or h.get("itemId", "").endswith(f"|{item_id}") for h in hits)
    return {"item_id": item_id, "query": query, "found": found, "total": total, "error": ""}


def main():
    parser = argparse.ArgumentParser(description="出品が検索結果に出現するか確認")
    parser.add_argument("--csv", default="", help="fuseki_test.csv等（item_id, titleカラム必須）")
    parser.add_argument("--sample", type=int, default=15, help="CSVからランダム抽出する件数")
    parser.add_argument("--item-ids", default="", help="カンマ区切りのItemID（--csvの代わりに直接指定）")
    parser.add_argument("--seller-id", default=CONFIG.get("EBAY_SELLER_ID", ""))
    args = parser.parse_args()

    targets: list[dict] = []
    if args.item_ids:
        # タイトルはCSVが無いと分からないため、直接指定時は簡易表示のみ
        targets = [{"item_id": i.strip(), "title": ""} for i in args.item_ids.split(",") if i.strip()]
    elif args.csv:
        with open(args.csv, newline="", encoding="utf-8-sig") as f:
            rows = list(_csv.DictReader(f))
        random.seed(42)
        targets = random.sample(rows, min(args.sample, len(rows)))
    else:
        print("⚠️  --csv か --item-ids のどちらかを指定してください")
        return

    app_token = _get_ebay_app_token(CONFIG)
    if not app_token:
        print("❌ eBay App Token取得失敗")
        return

    print(f"🔍 {len(targets)}件を検索照合中（seller={args.seller_id}）...\n")
    found_count = 0
    for t in targets:
        result = check_visibility(t["item_id"], t.get("title", ""), app_token, args.seller_id)
        if result["error"]:
            mark = "❓"
        elif result["found"]:
            mark = "✅"
            found_count += 1
        else:
            mark = "🚫"
        print(f"  {mark} {result['item_id']}  検索結果{result['total']}件中ヒット={result['found']}"
              f"  query=\"{result['query']}\"  {result['error']}")
        time.sleep(0.3)

    checked = len(targets)
    print(f"\n{'=' * 60}\n検索でヒットした: {found_count}/{checked}件\n{'=' * 60}")
    if checked and found_count < checked * 0.5:
        print("⚠️  半数以上が検索に出現していません。重複出品ポリシー等による検索除外が濃厚です。")


if __name__ == "__main__":
    main()
