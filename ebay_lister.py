"""
eBay 自動出品モジュール

【フロー】
  1. Google Sheets「出品待ちリスト」から商品を取得
  2. Keepa で最新価格・商品詳細を取得
  3. タイトル・説明文・価格を自動生成
  4. eBay Trading API で出品
  5. 取得したeBay商品IDを「商品マスタ」に登録

【使い方】
  python ebay_lister.py              # 出品待ちリストを一括出品
  python ebay_lister.py --asin BXXXXXXX  # 1件だけ出品（テスト用）
"""

import csv as _csv
import os as _os
import time
import argparse
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from config import CONFIG
from keepa_checker import KeepaChecker
from sheets_manager import SheetsManager


# ──────────────────────────────────────────────────────────────────────────
# eBayカテゴリCSV読み込み（葉カテゴリ検証・AI推薦用）
# ──────────────────────────────────────────────────────────────────────────

def _load_ebay_category_db() -> dict:
    """ebay_categories.csvを読み込んでカテゴリ辞書を構築"""
    csv_path = _os.path.join(_os.path.dirname(__file__), "ebay_categories.csv")
    cat_map = {}
    if not _os.path.exists(csv_path):
        return cat_map
    try:
        with open(csv_path, encoding="utf-8") as f:
            reader = _csv.DictReader(f)
            for row in reader:
                try:
                    cat_id = int(row["CategoryID"])
                    is_leaf = row.get("LeafCategory", "") == "TRUE"
                    path_parts = []
                    for i in range(1, 8):
                        n = row.get(f"L{i}_name", "").replace("&amp;", "&").strip()
                        if n:
                            path_parts.append(n)
                    cat_map[cat_id] = {
                        "name": row["CategoryName"].replace("&amp;", "&"),
                        "is_leaf": is_leaf,
                        "path": " > ".join(path_parts),
                    }
                except (ValueError, KeyError):
                    continue
    except Exception as e:
        print(f"  ⚠️  カテゴリCSV読み込み失敗: {e}")
    return cat_map

EBAY_CATEGORY_DB = _load_ebay_category_db()


def is_leaf_category(cat_id: int) -> bool:
    """カテゴリIDが葉カテゴリかどうかをCSVで確認（CSVにない場合はTrueを返す）"""
    info = EBAY_CATEGORY_DB.get(cat_id)
    if info is None:
        return True  # 不明な場合は通す
    return info["is_leaf"]


# CSVから商品タイトルに関連する葉カテゴリをキーワード検索
_STOP_WORDS = {
    "for", "the", "and", "with", "new", "set", "japanese", "japan", "from",
    "inch", "cm", "mm", "size", "pack", "piece", "lot", "type", "made",
    "high", "quality", "official", "original", "genuine", "product", "item",
    "use", "used", "brand", "model", "color", "black", "white", "blue", "red",
    "green", "silver", "gold", "pink", "clear",
}

def _search_csv_categories(title: str, brand: str = "", n: int = 40) -> list[tuple[str, int]]:
    """
    CSVの全葉カテゴリからタイトル・ブランドのキーワードに合致するものを検索。
    スコア（一致キーワード数）上位n件を返す。
    """
    import re as _re
    query = f"{title} {brand}"
    words = set(_re.findall(r'[a-zA-Z]{3,}', query.lower()))
    keywords = words - _STOP_WORDS
    if not keywords:
        return []

    scored: list[tuple[int, int, str]] = []
    for cat_id, info in EBAY_CATEGORY_DB.items():
        if not info.get("is_leaf"):
            continue
        text = f"{info['name']} {info['path']}".lower()
        score = sum(1 for kw in keywords if kw in text)
        if score > 0:
            scored.append((score, cat_id, f"{info['path']} > {info['name']}"))

    scored.sort(key=lambda x: -x[0])
    return [(label, cat_id) for _, cat_id, label in scored[:n]]


EBAY_API_URL = "https://api.ebay.com/ws/api.dll"
# テスト環境:
# EBAY_API_URL = "https://api.sandbox.ebay.com/ws/api.dll"

# eBay カテゴリID（商品に合わせて変更）
# 検索: https://pages.ebay.com/sellerCenter/catchanges.html
EBAY_CATEGORY_MAP = {
    "electronics":   9355,    # Consumer Electronics
    "toys":          220,     # Toys & Hobbies
    "model_kits":    2592,    # Toys & Hobbies > Models & Kits > Cars, Trucks & Vans
    "sports":        888,     # Sporting Goods
    "home":          11700,   # Home & Garden
    "fashion":       11450,   # Clothing, Shoes & Accessories
    "default":       2592,    # デフォルト: モデルキット（Cars, Trucks & Vans）
}

# 出品期間（日）
LISTING_DURATION = "GTC"  # GTC = Good Till Cancelled（無期限）


class EbayLister:
    def __init__(self, token: str, oauth_token: str = ""):
        self.token = token
        # Marketing API用OAuth2トークン（sell.marketingスコープ必須）
        # Trading APIのIAFトークンとは別物。未設定時はPromoted Listingをスキップ。
        self.oauth_token = oauth_token or CONFIG.get("EBAY_OAUTH_TOKEN", "")
        self.headers = {
            "X-EBAY-API-SITEID":              "0",    # 0=US
            "X-EBAY-API-COMPATIBILITY-LEVEL": "967",
            "X-EBAY-API-IAF-TOKEN":           token,
            "Content-Type":                   "text/xml",
        }
        self._campaign_id = ""  # Default campaignIDキャッシュ

    # ──────────────────────────────────────────────────────
    # Promoted Listings (General) - Default campaign
    # ──────────────────────────────────────────────────────
    def _get_default_campaign_id(self) -> str:
        """Default campaignのIDを取得してキャッシュ"""
        if self._campaign_id:
            return self._campaign_id
        if not self.oauth_token:
            print(
                "  ℹ️  Promoted Listing: EBAY_OAUTH_TOKEN未設定のためスキップ\n"
                "     → eBay Developer Portal で sell.marketing スコープ付きトークンを発行し\n"
                "       .env に EBAY_OAUTH_TOKEN=<token> を追加してください"
            )
            return ""
        try:
            resp = requests.get(
                "https://api.ebay.com/sell/marketing/v1/ad_campaign",
                headers={
                    "Authorization": f"Bearer {self.oauth_token}",
                    "Content-Type":  "application/json",
                },
                timeout=15,
            )
            if resp.status_code == 200:
                campaigns = resp.json().get("campaigns", [])
                for camp in campaigns:
                    if camp.get("campaignName") == "Default campaign":
                        self._campaign_id = camp.get("campaignId", "")
                        print(f"  📢 Default campaign取得: {self._campaign_id}")
                        return self._campaign_id
                # 名前で見つからない場合は最初のRUNNINGキャンペーンを使用
                for camp in campaigns:
                    if camp.get("campaignStatus") == "RUNNING":
                        self._campaign_id = camp.get("campaignId", "")
                        print(f"  ℹ️  'Default campaign'未発見 → '{camp.get('campaignName')}'を使用")
                        return self._campaign_id
                print(f"  ⚠️  RUNNINGキャンペーンが見つかりません（eBay Seller Hub で確認してください）")
            elif resp.status_code == 403:
                print(
                    "  ⚠️  Marketing API 403: EBAY_OAUTH_TOKEN に sell.marketing スコープが不足しています\n"
                    "     → eBay Developer Portal でトークンを再発行してください"
                )
            else:
                print(f"  ⚠️  キャンペーン一覧取得失敗: {resp.status_code}")
        except Exception as e:
            print(f"  ⚠️  キャンペーンID取得エラー: {e}")
        return ""

    def promote_listing(self, item_id: str, bid_pct: float = 2.1) -> bool:
        """出品済み商品をPromoted Listings General（Default campaign）に追加"""
        campaign_id = self._get_default_campaign_id()
        if not campaign_id:
            return False
        try:
            resp = requests.post(
                f"https://api.ebay.com/sell/marketing/v1/ad_campaign/{campaign_id}/bulk_create_ads_by_listing_id",
                headers={
                    "Authorization": f"Bearer {self.oauth_token}",
                    "Content-Type":  "application/json",
                },
                json={
                    "requests": [
                        {
                            "bidPercentage": f"{bid_pct:.2f}",
                            "listingId":     item_id,
                        }
                    ]
                },
                timeout=15,
            )
            if resp.status_code in (200, 201, 207):
                print(f"  📢 Promoted Listing設定完了: {item_id}（{bid_pct}%）")
                return True
            else:
                print(f"  ⚠️  Promoted Listing設定失敗: {resp.status_code} {resp.text[:120]}")
                return False
        except Exception as e:
            print(f"  ⚠️  Promoted Listing設定エラー: {e}")
            return False

    # ──────────────────────────────────────────────────────
    # メイン出品処理
    # ──────────────────────────────────────────────────────
    def list_item(self, product: dict, sku: str = "") -> dict:
        """
        1商品をeBayに出品する

        Args:
            product: {
                "asin": str,
                "title": str,
                "description": str,
                "price_usd": float,
                "category_id": int,
                "image_urls": list[str],
                "condition": str,       # "New" / "Used"
                "item_specifics": dict, # 商品仕様
            }

        Returns:
            {
                "success": bool,
                "item_id": str,   # 出品成功時のeBay商品ID
                "message": str,
            }
        """
        xml_body = self._build_add_item_xml(product, sku=sku)

        for attempt in range(1, 4):  # 最大3回リトライ
            try:
                resp = requests.post(
                    EBAY_API_URL,
                    headers={**self.headers, "X-EBAY-API-CALL-NAME": "AddItem"},
                    data=xml_body.encode("utf-8"),
                    timeout=30,
                )
                result = self._parse_add_item_response(resp.text)

                # 必須Item Specificが不足している場合は "No" で補完してリトライ
                missing = result.get("missing_specifics", [])
                if not result["success"] and missing and attempt < 3:
                    for field in missing:
                        product.setdefault("item_specifics", {})[field] = "No"
                        print(f"  🔧 必須Item Specific補完: {field} = No → リトライ")
                    xml_body = self._build_add_item_xml(product, sku=sku)
                    continue

                # System error（一時的エラー）はリトライ
                if not result["success"] and "system error" in result["message"].lower() and attempt < 3:
                    print(f"  ⚠️  System error → {attempt}回目リトライ（{attempt * 5}秒後）")
                    time.sleep(attempt * 5)
                    continue
                return result
            except Exception as e:
                if attempt < 3:
                    print(f"  ⚠️  リクエストエラー → リトライ ({attempt}): {e}")
                    time.sleep(attempt * 5)
                else:
                    return {"success": False, "item_id": "", "message": str(e)}
        return {"success": False, "item_id": "", "message": "リトライ上限に達しました"}

    # ──────────────────────────────────────────────────────
    # AddItem XML を構築
    # ──────────────────────────────────────────────────────
    def _build_add_item_xml(self, p: dict, sku: str = "") -> str:
        # 商品仕様（Item Specifics）をXMLに変換
        specifics_xml = ""
        for name, value in p.get("item_specifics", {}).items():
            specifics_xml += (
                "<NameValueList>"
                f"<Name>{self._escape_xml(str(name))}</Name>"
                f"<Value>{self._escape_xml(str(value))}</Value>"
                "</NameValueList>"
            )

        # 画像URL（最大12枚・重複なし）
        seen = set()
        raw_images = []
        for url in p.get("image_urls", []):
            if url and url not in seen:
                seen.add(url)
                raw_images.append(url)
        images_to_send = raw_images[:12]
        print(f"  🖼️  取得画像数: {len(p.get('image_urls', []))}枚 → 送信: {len(images_to_send)}枚（重複除去）")
        if not images_to_send:
            print(f"  ⚠️  画像なし（PictureDetailsをスキップ）")
        images_xml = "".join(f"<PictureURL>{url}</PictureURL>" for url in images_to_send)

        title       = self._escape_xml(p["title"][:80])
        price       = p["price_usd"]
        cat_id      = p.get("category_id", EBAY_CATEGORY_MAP["default"])
        cond_id     = self._condition_id(p.get("condition", "New"))
        desc        = p["description"].replace("]]>", "]]]]><![CDATA[>")
        upc         = p.get("upc", "Does not apply")
        stock_count = max(1, int(p.get("stock_count", 1)))

        # ボリューム割引（在庫2個以上の場合）
        volume_xml = ""
        if stock_count >= 2:
            p2 = round(price * 0.95, 2)
            p3 = round(price * 0.92, 2)
            p4 = round(price * 0.90, 2)
            volume_xml = (
                "<QuantityDiscount>"
                "<DiscountName>ALL_INCLUSIVE</DiscountName>"
                "<QuantityDiscountPrices>"
                f"<QuantityDiscountPrice><MinQuantity>2</MinQuantity><Price currencyID=\"USD\">{p2}</Price></QuantityDiscountPrice>"
                f"<QuantityDiscountPrice><MinQuantity>3</MinQuantity><Price currencyID=\"USD\">{p3}</Price></QuantityDiscountPrice>"
                f"<QuantityDiscountPrice><MinQuantity>4</MinQuantity><Price currencyID=\"USD\">{p4}</Price></QuantityDiscountPrice>"
                "</QuantityDiscountPrices>"
                "</QuantityDiscount>"
            )
            print(f"  🏷️  ボリューム割引設定: 2個→{p2}$ / 3個→{p3}$ / 4個以上→{p4}$")

        xml = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<AddItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">'
            "<RequesterCredentials>"
            f"<eBayAuthToken>{self.token}</eBayAuthToken>"
            "</RequesterCredentials>"
            "<Item>"
            f"<Title>{title}</Title>"
            f"<Description><![CDATA[{desc}]]></Description>"
            "<PrimaryCategory>"
            f"<CategoryID>{cat_id}</CategoryID>"
            "</PrimaryCategory>"
            f"<StartPrice>{price}</StartPrice>"
            f"<ConditionID>{cond_id}</ConditionID>"
            "<Country>JP</Country>"
            "<Currency>USD</Currency>"
            + (f"<SKU>{sku}</SKU>" if sku else "") +
            "<Location>Tokyo, Japan</Location>"
            "<DispatchTimeMax>7</DispatchTimeMax>"
            "<ListingDuration>GTC</ListingDuration>"
            "<ListingType>FixedPriceItem</ListingType>"
            f"<Quantity>{stock_count}</Quantity>"
            + volume_xml +
            "<ShipToLocations>Worldwide</ShipToLocations>"
            "<ProductListingDetails>"
            f"<UPC>{upc}</UPC>"
            "</ProductListingDetails>"
        )

        if images_xml:
            xml += f"<PictureDetails>{images_xml}</PictureDetails>"

        if specifics_xml:
            xml += f"<ItemSpecifics>{specifics_xml}</ItemSpecifics>"

        xml += (
            "<SellerProfiles>"
            "<SellerShippingProfile>"
            "<ShippingProfileID>392355301023</ShippingProfileID>"
            "</SellerShippingProfile>"
            "<SellerReturnProfile>"
            "<ReturnProfileID>260040355023</ReturnProfileID>"
            "</SellerReturnProfile>"
            "<SellerPaymentProfile>"
            "<PaymentProfileID>319160588023</PaymentProfileID>"
            "</SellerPaymentProfile>"
            "</SellerProfiles>"
            "</Item>"
            "</AddItemRequest>"
        )
        return xml

    # ──────────────────────────────────────────────────────
    # AddItem レスポンスを解析
    # ──────────────────────────────────────────────────────
    def _parse_add_item_response(self, xml_text: str) -> dict:
        try:
            root = ET.fromstring(xml_text)
            ns = {"e": "urn:ebay:apis:eBLBaseComponents"}
            ack = root.findtext("e:Ack", namespaces=ns)

            if ack in ("Success", "Warning"):
                item_id = root.findtext("e:ItemID", namespaces=ns) or ""
                fees = root.findtext("e:Fees/e:Fee/e:Fee", namespaces=ns) or "0"
                return {
                    "success": True,
                    "item_id": item_id,
                    "message": f"出品成功（手数料: ${fees}）",
                }
            else:
                # 詳細エラーを全て表示（Warning系は除外して実害エラーのみ抽出）
                errors_short = root.findall(".//e:ShortMessage", ns)
                errors_long  = root.findall(".//e:LongMessage", ns)
                error_codes  = root.findall(".//e:ErrorCode", ns)
                msgs = []
                missing_specifics: list[str] = []
                import re as _re2
                for s, l, c in zip(errors_short, errors_long, error_codes):
                    # 文字化け除去（非標準スペース U+00C2 A0 等）
                    short = (s.text or "").replace("\u00c2\u00a0", " ").replace("\u00a0", " ").strip()
                    long_  = (l.text or "").replace("\u00c2\u00a0", " ").replace("\u00a0", " ").strip()
                    code   = (c.text or "").strip()
                    msgs.append(f"[{code}] {short}")
                    print(f"    eBayエラー {code}: {long_}")
                    # 必須Item Specificの不足を検出して自動補完リトライ用に返す
                    if code == "21919303":
                        _m = _re2.search(r"The item specific (.+?) is missing", long_)
                        if _m:
                            missing_specifics.append(_m.group(1).strip())
                msg = " / ".join(msgs) if msgs else "不明なエラー"
                return {"success": False, "item_id": "", "message": msg,
                        "missing_specifics": missing_specifics}

        except Exception as e:
            return {"success": False, "item_id": "", "message": f"レスポンス解析エラー: {e}"}

    # ──────────────────────────────────────────────────────
    # ヘルパー
    # ──────────────────────────────────────────────────────
    def _condition_id(self, condition: str) -> int:
        return 1000 if condition == "New" else 3000  # 1000=New, 3000=Used

    def _escape_xml(self, text: str) -> str:
        return (text
                .replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
                .replace('"', "&quot;"))


# ──────────────────────────────────────────────────────────
# 商品データ生成（Keepaデータ → eBay出品データ）
# ──────────────────────────────────────────────────────────


# ──────────────────────────────────────────────────────────
# eBayタイトルから最適カテゴリIDを自動取得
# ──────────────────────────────────────────────────────────

def get_best_category(token: str, title: str, jan_code: str = "", config: dict = None,
                      brand: str = "", product_type: str = "") -> int:
    """
    タイトル優先でeBayの最適葉カテゴリIDを自動取得

    優先順位:
    1. タイトル → GetSuggestedCategories API（LeafCategoryのみ・信頼度40%以上・CSVで検証）
    2. Anthropic AI → category_hints.md の葉カテゴリ候補から選択
    3. デフォルトカテゴリ
    """
    base_headers = {
        "X-EBAY-API-SITEID":              "0",
        "X-EBAY-API-COMPATIBILITY-LEVEL": "967",
        "X-EBAY-API-IAF-TOKEN":           token,
        "Content-Type":                   "text/xml",
    }

    def _call_suggested(query: str) -> int:
        """GetSuggestedCategoriesで葉カテゴリのみ取得（0=失敗）"""
        query_safe = query.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        xml_body = (
            "<?xml version=" + chr(34) + "1.0" + chr(34) + " encoding=" + chr(34) + "utf-8" + chr(34) + "?>"
            "<GetSuggestedCategoriesRequest xmlns=" + chr(34) + "urn:ebay:apis:eBLBaseComponents" + chr(34) + ">"
            "<RequesterCredentials>"
            "<eBayAuthToken>" + token + "</eBayAuthToken>"
            "</RequesterCredentials>"
            "<Query>" + query_safe + "</Query>"
            "</GetSuggestedCategoriesRequest>"
        )
        resp = requests.post(
            "https://api.ebay.com/ws/api.dll",
            headers={**base_headers, "X-EBAY-API-CALL-NAME": "GetSuggestedCategories"},
            data=xml_body.encode("utf-8"),
            timeout=15,
        )
        if resp.status_code != 200:
            print(f"  ⚠️  GetSuggestedCategories HTTPエラー: {resp.status_code}")
            return 0
        root = ET.fromstring(resp.text)
        ns = {"e": "urn:ebay:apis:eBLBaseComponents"}
        ack = root.findtext("e:Ack", namespaces=ns)
        if ack not in ("Success", "Warning"):
            err = root.findtext(".//e:ShortMessage", namespaces=ns) or "不明"
            print(f"  ⚠️  GetSuggestedCategories APIエラー: {ack} / {err}")
            return 0

        suggestions = root.findall(".//e:SuggestedCategory", ns)
        if not suggestions:
            print(f"  ⚠️  GetSuggestedCategories: 候補なし（クエリ: {query[:40]}）")
            return 0

        # 葉カテゴリのみ対象（APIとCSV両方でLeaf確認）
        PCT_THRESHOLD = 40  # これ未満の信頼度はAIに委ねる
        best_id, best_pct = None, -1
        for s in suggestions:
            cat_id_str = s.findtext("e:Category/e:CategoryID", namespaces=ns)
            pct        = int(s.findtext("e:PercentItemFound", namespaces=ns) or "0")
            leaf       = s.findtext("e:Category/e:LeafCategory", namespaces=ns)
            if cat_id_str and leaf == "true":
                cat_id_int = int(cat_id_str)
                if is_leaf_category(cat_id_int) and pct > best_pct:
                    best_pct = pct
                    best_id  = cat_id_int

        if not best_id:
            # 候補はあるが全て非葉カテゴリ → 最初の候補IDをデバッグ表示
            first = suggestions[0]
            fid  = first.findtext("e:Category/e:CategoryID", namespaces=ns)
            fname = first.findtext("e:Category/e:CategoryName", namespaces=ns)
            fleaf = first.findtext("e:Category/e:LeafCategory", namespaces=ns)
            print(f"  ⚠️  葉カテゴリ候補なし（例: {fid} {fname} leaf={fleaf}）→ AI判定へ")
            return 0

        if best_pct < PCT_THRESHOLD:
            info = EBAY_CATEGORY_DB.get(best_id, {})
            print(f"  ⚠️  API信頼度が低い（{best_pct}% < {PCT_THRESHOLD}%）: {best_id} {info.get('path', '')} → AI判定へ")
            return 0

        # 非葉カテゴリは使わない（出品エラーの原因）
        return best_id

    # Step 1: タイトルでGetSuggestedCategories
    query = title[:80].strip() or "Japan import goods"
    try:
        cat_id = _call_suggested(query)
        if cat_id:
            info = EBAY_CATEGORY_DB.get(cat_id, {})
            path = info.get("path", "")
            print(f"  🏷️  タイトルカテゴリ: {cat_id}（{path}）")
            return cat_id
    except Exception as e:
        print(f"  ⚠️  タイトルカテゴリ取得失敗: {e}")

    # Step 2: CSV全体をキーワード検索 → 候補をAIに渡して最終判定
    _config = config or {}
    if _config.get("ANTHROPIC_API_KEY"):
        try:
            # CSVからタイトル・ブランドで関連葉カテゴリを検索
            csv_candidates = _search_csv_categories(title, brand=brand, n=40)
            print(f"  🔍  CSV候補: {len(csv_candidates)}件")

            if not csv_candidates:
                print(f"  ⚠️  CSV候補なし → デフォルトへ")
            else:
                category_hints = "\n".join(
                    f"- {label}: {cid}" for label, cid in csv_candidates
                )
                valid_ids = {cid for _, cid in csv_candidates}
                extra = ""
                if brand:
                    extra += f"Brand: {brand}\n"
                if product_type:
                    extra += f"Product type hint: {product_type}\n"
                prompt = (
                    f"You are an eBay category expert. Select the MOST SPECIFIC leaf category for this product.\n"
                    f"IMPORTANT: You MUST choose from the list below. Reply with ONLY the numeric ID.\n\n"
                    f"Product title: {title}\n"
                    f"{extra}\n"
                    f"Candidate leaf categories (from eBay category database):\n{category_hints}\n\n"
                    f"Think carefully about what the product physically IS. "
                    f"Reply with ONLY the numeric category ID from the list above."
                )
                resp = requests.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={
                        "Content-Type": "application/json",
                        "x-api-key": _config.get("ANTHROPIC_API_KEY", ""),
                        "anthropic-version": "2023-06-01",
                    },
                    json={"model": "claude-haiku-4-5-20251001", "max_tokens": 50,
                          "messages": [{"role": "user", "content": prompt}]},
                    timeout=15,
                )
                data = resp.json()
                if data.get("content"):
                    import re as _re
                    _raw = data["content"][0]["text"].strip()
                    _m = _re.search(r'\b(\d{4,6})\b', _raw)
                    if not _m:
                        print(f"  ⚠️  AI返却値から数値を抽出できません: {_raw[:60]}")
                        raise ValueError("no numeric category id found")
                    ai_cat_id = int(_m.group(1))
                    if ai_cat_id in valid_ids and is_leaf_category(ai_cat_id):
                        info = EBAY_CATEGORY_DB.get(ai_cat_id, {})
                        path = info.get("path", "")
                        print(f"  🏷️  AIカテゴリ（CSV検索）: {ai_cat_id}（{path}）")
                        return ai_cat_id
                    else:
                        print(f"  ⚠️  AI返却カテゴリ {ai_cat_id} は候補外 → スキップ")
        except Exception as e:
            print(f"  ⚠️  AIカテゴリ取得失敗: {e}")

    default_id = EBAY_CATEGORY_MAP["default"]
    print(f"  🏷️  デフォルトカテゴリ: {default_id}")
    return default_id


# ──────────────────────────────────────────────────────────
# Anthropic APIで日本語タイトル→英語eBayタイトルに変換
# ──────────────────────────────────────────────────────────

def translate_title_for_ebay(japanese_title: str, brand: str = "", model: str = "") -> str:
    """
    日本語タイトルをeBay Cassiniアルゴリズム最適化済み英語タイトルに変換（80文字フル活用）

    2026年eBay SEOロジック（Cassiniアルゴリズム対応）:
    1. Brand + Model を先頭に配置（最重要キーワードを前半に）
    2. 80文字をフル活用（未使用スペースは機会損失）
    3. フィラーワード禁止（WOW/Amazing/Look等）
    4. キーワード重複なし
    5. 買い手が検索する自然な言葉を使用
    6. 具体的スペック・互換性・カテゴリ属性を含む
    """
    def _call_title_api(prompt_text: str) -> str:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "Content-Type": "application/json",
                "x-api-key": CONFIG.get("ANTHROPIC_API_KEY", ""),
                "anthropic-version": "2023-06-01",
            },
            json={
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 150,
                "messages": [{"role": "user", "content": prompt_text}]
            },
            timeout=15,
        )
        data = resp.json()
        if data.get("content"):
            return data["content"][0]["text"].strip().strip('"').strip("'")[:80]
        return ""

    prompt = f"""You are an eBay SEO title expert. Convert this Japanese product to an English eBay title.

CRITICAL REQUIREMENT: Your title MUST be 78-80 characters long. Count every character carefully.
Every unused character is a lost ranking opportunity in eBay Cassini search.

RULES:
1. Brand + Model number first (highest search weight)
2. Fill remaining space with: dimensions, capacity, material, color, quantity, compatibility, product type synonyms buyers actually search
3. "Japanese" is OK only if it genuinely describes the product style/origin (e.g. "Japanese Hand Saw", "Japanese Pruning Shears") — never as padding
4. NEVER use "New", "Japan" as filler — eBay has a Condition field; these waste characters
5. NO: WOW, Amazing, Look, L@@K, !!!, keyword repetition
6. Title Case

Product info:
- Japanese title: {japanese_title}
- Brand: {brand or "extract from title"}
- Model: {model or "extract from title"}

TARGET: exactly 80 characters (hard max). Aim for 78-80.
EXAMPLE of good 80-char title: "Pilot G2 07 Retractable Gel Ink Fine Point 0.7mm Black Ballpoint Pen 12 Pack Set"

Reply with ONLY the title. No quotes. No explanation."""

    try:
        title = _call_title_api(prompt)
        if not title:
            return japanese_title[:80]

        # 75文字未満なら拡張リトライ（1回）
        if len(title) < 75:
            expand_prompt = f"""This eBay title is only {len(title)} characters. Expand it to 78-80 characters by adding relevant keywords, specs, or synonyms. Do NOT exceed 80 characters.

Current title ({len(title)} chars): {title}

Product: {japanese_title}
Brand: {brand or "N/A"} | Model: {model or "N/A"}

Add keywords like: material, dimensions, color, compatibility, quantity, product type synonyms. Do NOT add "New" or "Japan" as filler.
Reply with ONLY the expanded title. No quotes."""
            expanded = _call_title_api(expand_prompt)
            if expanded and len(expanded) > len(title):
                title = expanded

        print(f"  🌐 SEO最適化タイトル: {title} ({len(title)}文字)")
        return title

    except Exception as e:
        print(f"  ⚠️  タイトル翻訳失敗: {e}")

    return japanese_title[:80]

def build_listing_data(asin: str, keepa_data: dict, config: dict) -> dict:
    """
    KeepaデータからeBay出品に必要なデータを生成する

    - タイトル: Amazonタイトルをそのまま使用（80文字制限）
    - 説明文: テンプレートを自動生成
    - 価格: 仕入れ値から利益率・手数料込みで計算
    """
    amazon_price  = keepa_data["current_price"]
    sell_price    = calc_sell_price(
        amazon_price, config,
        weight_kg  = keepa_data.get("weight_kg", 1.0),
        length_cm  = keepa_data.get("length_cm", 0),
        width_cm   = keepa_data.get("width_cm",  0),
        height_cm  = keepa_data.get("height_cm", 0),
    )
    jp_title      = keepa_data.get("title", "")
    brand         = keepa_data.get("brand", "")
    manufacturer  = keepa_data.get("manufacturer", "")
    model         = keepa_data.get("model", "") or keepa_data.get("partNumber", "")
    title         = translate_title_for_ebay(jp_title, brand=brand, model=model)

    description = build_description(keepa_data, amazon_price)

    upc = keepa_data.get("upc", "Does not apply")
    _mpn_raw = keepa_data.get("mpn", "") or ""
    # "-" や空文字・記号のみ・リスト文字列など無効値はeBayが拒否するため "Does Not Apply" に正規化
    _mpn_invalid = ("", "-", ".", "N/A", "n/a", "[]", "[[]]", "Does Not Apply")
    mpn = _mpn_raw if _mpn_raw and str(_mpn_raw).strip() not in _mpn_invalid else "Does Not Apply"

    # カテゴリを先に決定（Item Specifics生成に活用するため）
    jan_code    = keepa_data.get("upc", "") or ""
    category_id = get_best_category(config["EBAY_TOKEN"], title, jan_code=jan_code, config=config,
                                     brand=brand or "", product_type=keepa_data.get("product_group", ""))
    cat_path    = EBAY_CATEGORY_DB.get(category_id, {}).get("path", "")

    item_specifics = {}
    item_specifics["Brand"] = brand or "Does Not Apply"
    item_specifics["MPN"]   = mpn
    if manufacturer:
        item_specifics["Manufacturer"] = manufacturer
    item_specifics["Country/Region of Manufacture"] = "Japan"

    # Anthropic APIでItem Specificsを自動生成（カテゴリ情報を含む）
    try:
        import requests as _req, json as _json
        _ccg_hint = ""
        if category_id in {261044, 261068, 183454, 2536}:
            _ccg_hint = ' Include "Set" (the card game set/expansion name), "Game" (e.g. Weiss Schwarz), "Language".'
        if category_id in {172513, 64352, 15200, 182084, 31388, 48749}:
            _ccg_hint = ' Include "Compatible Brand" (brand this accessory is compatible with), "Type", "Model".'
        if category_id in {71307, 122668, 175702, 42017, 631, 20068, 233}:
            _ccg_hint = ' Include "Battery Included" (Yes/No), "Voltage", "Power Source", "Type".'
        _headphone_cats = {14985, 112529, 184904, 33963, 293, 61882}
        _is_headphone = category_id in _headphone_cats or any(w in cat_path for w in ("Headphone", "Earphone", "Earbud"))
        if _is_headphone:
            _ccg_hint = ' REQUIRED: include "Connectivity" (value: "Wired" or "Wireless"). Also include "Driver Unit", "Impedance", "Type", "Color".'
        if category_id in {262304, 262305, 262306, 262307, 262308, 262309, 262310, 262311, 262312, 262313, 262314, 262315, 262316, 45} or "Railroads & Trains" in cat_path:
            _ccg_hint = ' Include "Gauge" (N/HO/Z/G/O/OO/TT), "Scale", "Brand", "Type".'
        _prompt = (
            f"You are an eBay listing expert. Generate Item Specifics for this product.\n"
            f"eBay Category: {cat_path}\n"
            f"Product: {title}\nBrand: {brand or 'N/A'}\nModel: {mpn or 'N/A'}\n"
            f"Generate ONLY a JSON object with relevant eBay Item Specifics for this category.\n"
            f"Include fields the category may require (Type, Color, Material, Size, Features, Theme, "
            f"Age Group, Item Width, Item Length, Item Height, Unit Type, Units per Lot, etc.){_ccg_hint}\n"
            f"Max 12 fields. No markdown. Example: {{\"Type\": \"Figure\", \"Color\": \"Multi-Color\"}}"
        )
        _resp = _req.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "Content-Type": "application/json",
                "x-api-key": config.get("ANTHROPIC_API_KEY", ""),
                "anthropic-version": "2023-06-01",
            },
            json={"model": "claude-haiku-4-5-20251001", "max_tokens": 400,
                  "messages": [{"role": "user", "content": _prompt}]},
            timeout=15,
        )
        _data = _resp.json()
        if _data.get("content"):
            _text = _data["content"][0]["text"].strip().replace("```json", "").replace("```", "").strip()
            _ai = _json.loads(_text)
            for k, v in _ai.items():
                if k not in item_specifics and v and str(v).lower() not in ("n/a", "unknown", ""):
                    item_specifics[k] = str(v)[:65]
            print(f"  🏷️  Item Specifics: {len(item_specifics)}項目生成")
    except Exception as _e:
        print(f"  ⚠️  Item Specifics生成失敗: {_e}")

    # カテゴリ固有の必須フィールド補完（寸法データから自動セット）
    _w = keepa_data.get("width_cm", 0) or 0
    _l = keepa_data.get("length_cm", 0) or 0
    _h = keepa_data.get("height_cm", 0) or 0

    # 寸法が必須なカテゴリ群（Kitchen/Home/Storage等）
    _dim_required_cats = {179206, 20655, 20654, 20656, 26677, 72}
    if category_id in _dim_required_cats:
        if "Item Width" not in item_specifics and _w:
            item_specifics["Item Width"] = f"{_w} cm"
        if "Item Length" not in item_specifics and _l:
            item_specifics["Item Length"] = f"{_l} cm"
        if "Item Height" not in item_specifics and _h:
            item_specifics["Item Height"] = f"{_h} cm"

    # カメラ・パーツ系カテゴリ: "Compatible Brand" が必須
    _compat_brand_cats = {172513, 64352, 15200, 182084, 31388, 48749}
    if category_id in _compat_brand_cats and "Compatible Brand" not in item_specifics:
        item_specifics["Compatible Brand"] = brand or "Does Not Apply"

    # 電動工具カテゴリ: "Battery Included" が必須
    _power_tool_cats = {71307, 122668, 175702, 42017, 631, 20068, 233}
    if category_id in _power_tool_cats and "Battery Included" not in item_specifics:
        # タイトルにバッテリー・充電器の記述があればYes、なければNo
        _title_lower = title.lower()
        _has_battery = any(w in _title_lower for w in ("battery", "charger", "cordless", "コードレス"))
        item_specifics["Battery Included"] = "Yes" if _has_battery else "No"

    # 鉄道模型カテゴリ: "Gauge" が必須（カテゴリパスで判定）
    _railroad_cats = {262304, 262305, 262306, 262307, 262308, 262309, 262310, 262311, 262312, 262313, 262314, 262315, 262316, 45}
    _is_railroad = category_id in _railroad_cats or "Model Railroad" in cat_path or "Railroads & Trains" in cat_path
    if _is_railroad and "Gauge" not in item_specifics:
        import re as _re
        _gauge_map = {
            r"\bN\s*(?:scale|gauge|ゲージ)\b": "N",
            r"\bHO\s*(?:scale|gauge|ゲージ)\b": "HO",
            r"\bZ\s*(?:scale|gauge|ゲージ)\b": "Z",
            r"\bG\s*(?:scale|gauge|ゲージ)\b": "G",
            r"\bO\s*(?:scale|gauge|ゲージ)\b": "O",
            r"\bOO\s*(?:scale|gauge)\b": "OO",
            r"\bTT\s*(?:scale|gauge)\b": "TT",
        }
        _gauge_val = None
        _search_text = f"{title} {keepa_data.get('title', '')}"
        for pattern, gauge in _gauge_map.items():
            if _re.search(pattern, _search_text, _re.IGNORECASE):
                _gauge_val = gauge
                break
        item_specifics["Gauge"] = _gauge_val or "N"  # 不明時はNスケールをデフォルト

    # ヘッドホンカテゴリ: "Connectivity" が必須
    _headphone_cats = {14985, 112529, 184904, 33963, 293, 61882}
    _is_headphone = category_id in _headphone_cats or any(w in cat_path for w in ("Headphone", "Earphone", "Earbud"))
    if _is_headphone and "Connectivity" not in item_specifics:
        _title_lower = title.lower()
        _wireless_kw = ("wireless", "bluetooth", "bt ", "true wireless", "tws", "nfc", "wifi", "wi-fi")
        _is_wireless = any(w in _title_lower for w in _wireless_kw)
        item_specifics["Connectivity"] = "Wireless" if _is_wireless else "Wired"

    # CCGカテゴリ: "Set" / "Franchise" / "Game" フィールドが必須
    _ccg_cats = {261044, 261068, 183454, 2536}
    if category_id in _ccg_cats:
        import re as _re
        _remove_words = r"\b(Bushiroad|Weiss Schwarz|Weiß Schwarz|Booster|Box|Pack|Sealed|New|TCG|CCG|Card Game|Trading Card)\b"
        if "Set" not in item_specifics:
            _set_candidate = _re.sub(_remove_words, "", title, flags=_re.IGNORECASE).strip(" -|/")
            _set_candidate = _re.sub(r"\s+", " ", _set_candidate).strip()
            item_specifics["Set"] = (_set_candidate or brand or "Does Not Apply")[:65]
        if "Franchise" not in item_specifics:
            # ブランドをFranchise（IP名）として使用
            item_specifics["Franchise"] = (brand or "Does Not Apply")[:65]
        if "Game" not in item_specifics:
            item_specifics["Game"] = "Weiss Schwarz"

    # おもちゃ・ゲームカテゴリ全般: "Franchise"が必須なケースに対応
    _toy_cats = {220, 246, 19071, 3034, 4082, 19169}
    if category_id in _toy_cats or "Toys & Hobbies" in cat_path:
        if "Franchise" not in item_specifics:
            item_specifics["Franchise"] = (brand or "Does Not Apply")[:65]

    return {
        "asin":           asin,
        "title":          title,
        "description":    description,
        "price_usd":      sell_price,
        "category_id":    category_id,
        "image_urls":     keepa_data.get("image_urls", []),
        "condition":      "New",
        "item_specifics": item_specifics,
        "upc":            upc,
        "stock_count":    keepa_data.get("stock_count", 1),
    }


def build_description(keepa_data: dict, amazon_price_jpy: float) -> str:
    """
    eBay Cassini SEO最適化済み商品説明文を生成

    2026年SEOロジック:
    - 説明文にもキーワードを自然に含める（Cassiniは説明文も解析）
    - モバイルフレンドリー（短い段落・箇条書き）
    - 買い手の信頼を高める情報を含む
    - 検索意図に合致した自然な文章
    """
    jp_title  = keepa_data.get("title", "")
    jp_feats  = keepa_data.get("features", []) or []
    brand     = keepa_data.get("brand", "") or "See description"
    model     = keepa_data.get("model", "") or keepa_data.get("partNumber", "") or ""
    rating    = keepa_data.get("rating", 0)
    reviews   = keepa_data.get("review_count", 0)

    prompt = f"""You are an eBay listing copywriter expert. Create an SEO-optimized HTML product description for eBay.

2026 eBay Cassini SEO Rules:
- Naturally weave keywords buyers search for into the description
- Mobile-friendly: short paragraphs, bullet points
- Build buyer trust: condition, authenticity, shipping info
- Include brand, model, key specs naturally in text
- NO keyword stuffing
- Professional but approachable tone

Product Info:
- Japanese Title: {jp_title}
- Brand: {brand}
- Model: {model}
- Amazon JP Rating: {rating}/5 ({reviews} reviews)
- Features (Japanese): {chr(10).join(jp_feats[:5]) if jp_feats else "N/A"}

Create HTML description with these sections:
1. Brief compelling intro (1-2 sentences with keywords)
2. Key Features (translated bullet points)
3. Product Specs table (Brand, Model, Condition, Origin)
4. Why Buy From Japan section
5. Shipping & Returns info

Use inline CSS for styling. Keep total under 800 words. Reply with ONLY the HTML."""

    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "Content-Type": "application/json",
                "x-api-key": CONFIG.get("ANTHROPIC_API_KEY", ""),
                "anthropic-version": "2023-06-01",
            },
            json={
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 1500,
                "messages": [{"role": "user", "content": prompt}]
            },
            timeout=30,
        )
        data = resp.json()
        if resp.status_code != 200 or data.get("type") == "error":
            err = data.get("error", data)
            raise RuntimeError(f"説明文生成失敗: HTTP {resp.status_code} / {err}")
        if data.get("content"):
            html = data["content"][0]["text"].strip()
            # コードブロックを除去
            if html.startswith("```"):
                html = html.split("\n", 1)[1].rsplit("```", 1)[0]
            print(f"  📝 SEO説明文生成完了（{len(html)}文字）")
            return html
    except RuntimeError:
        raise
    except Exception as e:
        raise RuntimeError(f"説明文生成失敗: {e}") from e

    raise RuntimeError("説明文生成失敗: APIレスポンスが空")


def calc_sell_price(amazon_price_jpy: float, config: dict, weight_kg: float = 1.0,
                    length_cm: float = 0, width_cm: float = 0, height_cm: float = 0) -> float:
    """Amazon円価格 → eBayドル売値（SpeedPAK実送料を使用）"""
    from shipping_calculator import get_shipping_jpy
    shipping_jpy = get_shipping_jpy(weight_kg, destination="US48",
                                    length_cm=length_cm, width_cm=width_cm, height_cm=height_cm)
    # 関税は売上に対して適用（eBay手数料と同様に売値から差し引く）
    product_usd  = amazon_price_jpy / config["JPY_TO_USD"]
    shipping_usd = shipping_jpy / config["JPY_TO_USD"]
    usd = (product_usd + shipping_usd) / (1 - config["EBAY_FEE_RATE"] - config["TARIFF_RATE"])
    usd = usd / (1 - config["TARGET_MARGIN"])
    print(f"  📦 送料: ¥{shipping_jpy:,}（{weight_kg}kg → US48）")
    floor = config.get("MIN_SELL_PRICE_USD", 0)
    if floor and usd < floor:
        print(f"  ⚠️  計算売値 ${usd:.2f} < 下限 ${floor} → 下限価格を適用")
        usd = floor
    return round(usd, 2)


# ──────────────────────────────────────────────────────────
# KeepaCheckerの拡張取得（出品用に詳細情報も取得）
# ──────────────────────────────────────────────────────────

def fetch_listing_details(keepa_api, asin: str) -> dict:
    """出品に必要な詳細情報をKeepa REST APIで直接取得"""
    import requests
    from config import CONFIG

    try:
        resp = requests.get(
            "https://api.keepa.com/product",
            params={
                "key":     CONFIG["KEEPA_API_KEY"],
                "domain":  "5",
                "asin":    asin,
                "history": "1",
                "offers":  "20",
                "stock":   "1",
            },
            timeout=30,
        )
        data = resp.json()
        products = data.get("products", [])
        if not products:
            print(f"  ⚠️  Keepa: 商品が見つかりません ({asin})")
            return {}

        p = products[0]

        # 画像URL生成（images 配列 → imagesCSV カンマ区切り → 両方を試みる）
        image_urls = []
        images = p.get("images") or []
        if not images:
            images_csv = p.get("imagesCSV", "") or ""
            if images_csv:
                images = [s.strip() for s in images_csv.split(",") if s.strip()]
        for img in images[:12]:
            if isinstance(img, dict):
                img_id = img.get("m") or img.get("l") or ""
            else:
                img_id = str(img)
            if img_id:
                img_id_encoded = img_id.replace("+", "%2B")
                image_urls.append(
                    f"https://images-na.ssl-images-amazon.com/images/I/{img_id_encoded}"
                )
        print(f"  🖼️  Keepa画像数: {len(images)}枚 → URL生成: {len(image_urls)}枚")

        # 商品特徴
        features = p.get("features", []) or []

        # 現在価格: 新品最安値(csv[1])優先 → Amazon直販(csv[0]) → Buy Box(csv[18])
        csv = p.get("csv", []) or []
        price = 0
        for idx in [1, 0, 18, 3]:  # 新品最安値優先、Buy Boxも参照
            if idx >= len(csv):
                continue
            series = csv[idx]
            if not series:
                continue
            prices = [v for v in series[1::2] if v and v > 0]
            if prices:
                price = prices[-1]
                break
        # availabilityAmazon: -1=出品なし, 0=在庫あり, 1=一時的品切, 2=在庫なし, 4=予約商品
        avail = p.get("availabilityAmazon", -1)
        in_stock = price > 0 or avail == 0  # 価格が取れなくてもavail=0なら在庫あり
        raw_stock = p.get("stock")  # Keepaが返す在庫数（整数 or None）
        if isinstance(raw_stock, (int, float)) and raw_stock >= 2:
            stock_count = int(min(raw_stock, 10))  # 最大10個でキャップ
        elif avail == 0 and in_stock:
            stock_count = 1  # 在庫あり確認だが数量不明 → 安全に1
        else:
            stock_count = 1
        print(f"  💴 取得価格: ¥{price:,.0f} / 在庫: {'あり' if in_stock else 'なし'} / 数量: {stock_count}")
        print(f"    トークン残: {data.get('tokensLeft', 'N/A')}")

        upc_list = p.get("upcList") or []
        ean_list = p.get("eanList") or []
        part_num = p.get("partNumber") or ""
        model    = p.get("model") or ""

        upc = ean_list[0] if ean_list else (upc_list[0] if upc_list else "Does not apply")
        raw_mpn = str(part_num or model or "").strip()
        # 数字のみ（JAN/EAN/UPCコード）・リスト文字列・記号のみなど無効値はMPNとして除外
        _mpn_invalid = ("", "-", ".", "N/A", "n/a", "[]", "[[]]")
        mpn = raw_mpn if raw_mpn and raw_mpn not in _mpn_invalid and not raw_mpn.isdigit() else "Does Not Apply"

        # 重量・寸法（Keepa: 重量はg、寸法はmm）
        pkg_weight_g  = p.get("packageWeight") or 0   # グラム
        pkg_length_mm = p.get("packageLength") or 0
        pkg_width_mm  = p.get("packageWidth")  or 0
        pkg_height_mm = p.get("packageHeight") or 0

        weight_kg  = (pkg_weight_g  / 1000) if pkg_weight_g  else 1.0  # 不明時は1kg
        length_cm  = (pkg_length_mm / 10)   if pkg_length_mm else 0
        width_cm   = (pkg_width_mm  / 10)   if pkg_width_mm  else 0
        height_cm  = (pkg_height_mm / 10)   if pkg_height_mm else 0

        if pkg_weight_g:
            print(f"  ⚖️  重量: {weight_kg:.3f}kg / 寸法: {length_cm}×{width_cm}×{height_cm}cm")
        else:
            print(f"  ⚖️  重量不明 → デフォルト1.0kgを使用")

        return {
            "title":         p.get("title", ""),
            "brand":         p.get("brand", "") or "Does Not Apply",
            "manufacturer":  p.get("manufacturer", ""),
            "features":      features,
            "image_urls":    image_urls,
            "current_price": price,
            "in_stock":      in_stock,
            "stock_count":   stock_count,
            "rating":        (p.get("avgRating") or 0) / 10,
            "review_count":  p.get("reviewCount", 0),
            "upc":           upc,
            "mpn":           mpn,
            "weight_kg":     weight_kg,
            "length_cm":     length_cm,
            "width_cm":      width_cm,
            "height_cm":     height_cm,
        }

    except Exception as e:
        print(f"  ⚠️  Keepa詳細取得エラー ({asin}): {e}")
        return {}


# ──────────────────────────────────────────────────────────
# メイン処理
# ──────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="eBay自動出品ツール")
    parser.add_argument("--asin", type=str, help="1件だけ出品するASIN（テスト用）")
    parser.add_argument("--dry-run", action="store_true", help="出品せずデータ確認のみ")
    args = parser.parse_args()

    print("=" * 60)
    print(f"🛒 eBay自動出品開始: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    if args.dry_run:
        print("  ⚠️  DRY RUNモード（実際には出品しません）")
    print("=" * 60)

    sheets = SheetsManager(CONFIG["SHEET_ID"])

    # 設定シートから利益率などを上書き
    _settings = sheets.get_settings()
    _OVERRIDABLE = ["TARGET_MARGIN", "EBAY_FEE_RATE", "TARIFF_RATE", "MIN_SELL_PRICE_USD", "PRICE_CHANGE_THRESHOLD"]
    for _key in _OVERRIDABLE:
        if _key in _settings:
            CONFIG[_key] = _settings[_key]
    print(f"  📊 利益率: {CONFIG['TARGET_MARGIN']*100:.1f}%（設定シートより）")

    keepa  = KeepaChecker(CONFIG["KEEPA_API_KEY"])
    lister = EbayLister(CONFIG["EBAY_TOKEN"])

    # 出品対象を取得
    if args.asin:
        # 1件指定モード（テスト用）
        pending = [{"ASIN": args.asin, "メモ": "手動指定"}]
    else:
        # 出品待ちリストから取得
        pending = sheets.get_pending_products()

    print(f"\n📋 出品待ち: {len(pending)} 件\n")

    success_count = 0
    fail_count    = 0

    for i, item in enumerate(pending):
        jan_code = str(item.get("JANコード", "") or item.get("ASIN", "")).strip()
        print(f"[{i+1}/{len(pending)}] JANコード: {jan_code}")

        # JANコード→ASIN変換してKeepaで詳細取得
        if jan_code.isdigit():
            # JANコード（数字のみ）の場合はASINに変換
            asin = keepa.jan_to_asin(jan_code)
            if not asin:
                print(f"  ⛔ スキップ（ASIN変換失敗）")
                sheets.update_pending_status(jan_code, "スキップ（ASIN変換失敗）")
                fail_count += 1
                continue
            keepa_data = fetch_listing_details(keepa, asin)
        else:
            # ASINの場合はそのまま取得（後方互換）
            asin = jan_code
            keepa_data = fetch_listing_details(keepa, asin)

        if not keepa_data or not keepa_data.get("in_stock"):
            print(f"  ⛔ スキップ（在庫なし or 取得失敗）")
            sheets.update_pending_status(jan_code, "スキップ（在庫なし）")
            fail_count += 1
            continue

        print(f"  📦 {keepa_data['title'][:50]}")
        print(f"  💴 Amazon価格: ¥{keepa_data['current_price']:,.0f}")

        # 出品データ生成
        listing = build_listing_data(asin, keepa_data, CONFIG)
        print(f"  💵 eBay売値: ${listing['price_usd']}")

        if args.dry_run:
            print(f"  ✅ [DRY RUN] 出品データ確認OK")
            continue

        # eBayに出品（JANコードをSKUとして設定）
        sku = jan_code if jan_code.isdigit() else ""
        result = lister.list_item(listing, sku=sku)

        if result["success"]:
            ebay_id = result["item_id"]
            print(f"  ✅ 出品成功！ eBay商品ID: {ebay_id}")

            # Promoted Listings General: Default campaign 2.1%
            lister.promote_listing(ebay_id, bid_pct=2.1)

            # 商品マスタに登録
            sheets.add_product(
                asin       = asin,
                ebay_id    = ebay_id,
                name       = keepa_data["title"],
                base_price = keepa_data["current_price"],
                ebay_price = listing["price_usd"],
                memo       = item.get("メモ", ""),
                jan_code   = jan_code if jan_code.isdigit() else keepa_data.get("upc", ""),
            )
            # 出品待ちリストのステータスを更新
            sheets.update_pending_status(jan_code, "出品完了")
            success_count += 1
        else:
            print(f"  ❌ 出品失敗: {result['message']}")
            sheets.update_pending_status(jan_code, f"失敗: {result['message'][:30]}")
            fail_count += 1

        time.sleep(1)  # API制限対策

    print("\n" + "=" * 60)
    print(f"✅ 完了: 成功 {success_count} 件 / 失敗 {fail_count} 件")
    print("=" * 60)


if __name__ == "__main__":
    main()