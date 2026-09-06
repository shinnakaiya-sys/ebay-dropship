"""
暫定措置: 新カテゴリー作成(SetStoreCategories)がブロックされている間、
既存の22カテゴリーへ商品をできるだけ意味のある形で振り分けるためのマッピング。
SetStoreCategoriesの問題が解決したら、本来のcategory_map.py(14カテゴリー構成)へ
run_migration.pyで正式移行する。
"""

# eBay公式トップカテゴリー -> 既存ストアカテゴリー名
TOP1_TO_EXISTING = {
    "Toys & Hobbies": "Toys & Hobbies",
    "Sporting Goods": "Outdoor Sports",
    "Home & Garden": "Home & Garden",
    "Business & Industrial": "Business & Industrial",
    "Jewelry & Watches": "Jewelry & Watches",
    "Cameras & Photo": "Cameras & Photo",
    "Music": "Movies & Music",
    "Movies & TV": "Movies & Music",
    "Consumer Electronics": "Consumer Electronics",
    "Computers/Tablets & Networking": "Electronics",
    "Video Games & Consoles": "Video Games & Consoles",
    "Health & Beauty": "Health & Beauty",
    "Collectibles": "Collectibles",
    "Cell Phones & Accessories": "Electronics",
}

# (トップ, セカンド) -> 既存ストアカテゴリー名。主にSporting Goodsの細分化用
TOP2_OVERRIDE_EXISTING = {
    ("Sporting Goods", "Fishing"): "Fishing",
    ("Sporting Goods", "Camping & Hiking"): "Camping & Hiking",
    ("Sporting Goods", "Golf"): "Golf",
    ("Sporting Goods", "Tennis & Racquet Sports"): "Tennis & Racquet Sports",
    ("Sporting Goods", "Cycling"): "Cycling",
    ("Sporting Goods", "Baseball & Softball"): "Baseball & Softball",
    ("Toys & Hobbies", "Games"): "Indoor Games",
    ("Toys & Hobbies", "Puzzles"): "Indoor Games",
    ("Toys & Hobbies", "Educational"): "Indoor Games",
}

FALLBACK_EXISTING = "Other"


def map_item_existing(ebay_category_name: str) -> str:
    """'親:子:...' 形式のPrimaryCategoryフルパスから既存ストアカテゴリー名を返す"""
    if not ebay_category_name:
        return FALLBACK_EXISTING
    parts = ebay_category_name.split(":")
    top1 = parts[0]
    top2 = parts[1] if len(parts) > 1 else None

    if top2 and (top1, top2) in TOP2_OVERRIDE_EXISTING:
        return TOP2_OVERRIDE_EXISTING[(top1, top2)]
    if top1 in TOP1_TO_EXISTING:
        return TOP1_TO_EXISTING[top1]
    return FALLBACK_EXISTING
