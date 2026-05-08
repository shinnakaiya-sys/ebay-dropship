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

import time
import argparse
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from config import CONFIG
from keepa_checker import KeepaChecker
from sheets_manager import SheetsManager


EBAY_API_URL = "https://api.ebay.com/ws/api.dll"
# テスト環境:
# EBAY_API_URL = "https://api.sandbox.ebay.com/ws/api.dll"

# eBay カテゴリID（商品に合わせて変更）
# 検索: https://pages.ebay.com/sellerCenter/catchanges.html
EBAY_CATEGORY_MAP = {
    "electronics":   9355,    # Consumer Electronics
    "toys":          220,     # Toys & Hobbies
    "sports":        888,     # Sporting Goods
    "home":          11700,   # Home & Garden
    "fashion":       11450,   # Clothing, Shoes & Accessories
    "default":       14943,   # Cameras & Photo > Other
}

# 出品期間（日）
LISTING_DURATION = "GTC"  # GTC = Good Till Cancelled（無期限）


class EbayLister:
    def __init__(self, token: str):
        self.token = token
        self.headers = {
            "X-EBAY-API-SITEID":              "0",    # 0=US
            "X-EBAY-API-COMPATIBILITY-LEVEL": "967",
            "X-EBAY-API-IAF-TOKEN":           token,
            "Content-Type":                   "text/xml",
        }

    # ──────────────────────────────────────────────────────
    # メイン出品処理
    # ──────────────────────────────────────────────────────
    def list_item(self, product: dict) -> dict:
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
        xml_body = self._build_add_item_xml(product)

        try:
            resp = requests.post(
                EBAY_API_URL,
                headers={**self.headers, "X-EBAY-API-CALL-NAME": "AddItem"},
                data=xml_body.encode("utf-8"),
                timeout=30,
            )
            return self._parse_add_item_response(resp.text)

        except Exception as e:
            return {"success": False, "item_id": "", "message": str(e)}

    # ──────────────────────────────────────────────────────
    # AddItem XML を構築
    # ──────────────────────────────────────────────────────
    def _build_add_item_xml(self, p: dict) -> str:
        # 商品仕様（Item Specifics）をXMLに変換
        specifics_xml = ""
        for name, value in p.get("item_specifics", {}).items():
            specifics_xml += (
                "<NameValueList>"
                f"<Name>{self._escape_xml(str(name))}</Name>"
                f"<Value>{self._escape_xml(str(value))}</Value>"
                "</NameValueList>"
            )

        # 画像URL（最大12枚）
        images_xml = ""
        for url in p.get("image_urls", [])[:12]:
            if url:
                images_xml += f"<PictureURL>{url}</PictureURL>"

        title    = self._escape_xml(p["title"][:80])
        price    = p["price_usd"]
        cat_id   = p.get("category_id", EBAY_CATEGORY_MAP["default"])
        cond_id  = self._condition_id(p.get("condition", "New"))
        desc     = p["description"].replace("]]>", "]]]]><![CDATA[>")
        ship_usd = CONFIG["SHIPPING_USD"]

        upc  = p.get("upc", "Does not apply")

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
            "<Location>Tokyo, Japan</Location>"
            "<DispatchTimeMax>7</DispatchTimeMax>"
            "<ListingDuration>GTC</ListingDuration>"
            "<ListingType>FixedPriceItem</ListingType>"
            "<Quantity>1</Quantity>"
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
            "<ShippingProfileID>362218680023</ShippingProfileID>"
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
                # 詳細エラーを全て表示
                errors_short = root.findall(".//e:ShortMessage", ns)
                errors_long  = root.findall(".//e:LongMessage", ns)
                msgs = []
                for s, l in zip(errors_short, errors_long):
                    msgs.append(f"{s.text} | {l.text}")
                msg = " / ".join(msgs) if msgs else "不明なエラー"
                return {"success": False, "item_id": "", "message": msg}

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

def get_best_category(token: str, title: str, jan_code: str = "") -> int:
    """
    JANコード（EAN）優先でeBayの最適カテゴリIDを自動取得

    優先順位:
    1. JANコード → FindProducts API（最高精度・商品DB直接マッチ）
    2. タイトル  → GetSuggestedCategories API（フォールバック）
    """
    import xml.etree.ElementTree as ET

    base_headers = {
        "X-EBAY-API-SITEID":              "0",
        "X-EBAY-API-COMPATIBILITY-LEVEL": "967",
        "X-EBAY-API-IAF-TOKEN":           token,
        "Content-Type":                   "text/xml",
    }

    # Step 1: JANコード（EAN）をGetSuggestedCategoriesのクエリとして使用
    # JANコードは数字のみのため特殊文字エスケープ不要で最も安全
    if jan_code and jan_code not in ("Does not apply", ""):
        xml_body = (
            "<?xml version=" + chr(34) + "1.0" + chr(34) + " encoding=" + chr(34) + "utf-8" + chr(34) + "?>"
            "<GetSuggestedCategoriesRequest xmlns=" + chr(34) + "urn:ebay:apis:eBLBaseComponents" + chr(34) + ">"
            "<RequesterCredentials>"
            "<eBayAuthToken>" + token + "</eBayAuthToken>"
            "</RequesterCredentials>"
            "<Query>" + jan_code + "</Query>"
            "</GetSuggestedCategoriesRequest>"
        )
        try:
            resp = requests.post(
                "https://api.ebay.com/ws/api.dll",
                headers={**base_headers, "X-EBAY-API-CALL-NAME": "GetSuggestedCategories"},
                data=xml_body.encode("utf-8"),
                timeout=15,
            )
            root = ET.fromstring(resp.text)
            ns = {"e": "urn:ebay:apis:eBLBaseComponents"}
            ack = root.findtext("e:Ack", namespaces=ns)
            if ack in ("Success", "Warning"):
                suggestions = root.findall(".//e:SuggestedCategory", ns)
                best_id  = None
                best_pct = 0
                for s in suggestions:
                    cat_id = s.findtext("e:Category/e:CategoryID", namespaces=ns)
                    pct    = s.findtext("e:PercentItemFound", namespaces=ns) or "0"
                    leaf   = s.findtext("e:Category/e:LeafCategory", namespaces=ns)
                    if cat_id and leaf == "true" and int(pct) > best_pct:
                        best_pct = int(pct)
                        best_id  = int(cat_id)
                if best_id:
                    print(f"  🏷️  JANコードカテゴリ: {best_id}（EAN: {jan_code} / {best_pct}%マッチ）")
                    return best_id
        except Exception as e:
            print(f"  ⚠️  JANコードカテゴリ取得失敗: {e}")

    # Step 2: タイトルでGetSuggestedCategories（フォールバック）
    query = title.encode("ascii", errors="ignore").decode()[:60].strip()
    if not query or len(query) < 3:
        query = "Japan import goods"
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
    try:
        resp = requests.post(
            "https://api.ebay.com/ws/api.dll",
            headers={**base_headers, "X-EBAY-API-CALL-NAME": "GetSuggestedCategories"},
            data=xml_body.encode("utf-8"),
            timeout=15,
        )
        root = ET.fromstring(resp.text)
        ns = {"e": "urn:ebay:apis:eBLBaseComponents"}
        suggestions = root.findall(".//e:SuggestedCategory", ns)
        best_id  = None
        best_pct = 0
        for s in suggestions:
            cat_id = s.findtext("e:Category/e:CategoryID", namespaces=ns)
            pct    = s.findtext("e:PercentItemFound", namespaces=ns) or "0"
            leaf   = s.findtext("e:Category/e:LeafCategory", namespaces=ns)
            if cat_id and leaf == "true" and int(pct) > best_pct:
                best_pct = int(pct)
                best_id  = int(cat_id)
        if best_id:
            print(f"  🏷️  タイトルカテゴリ: {best_id}（{best_pct}%マッチ）")
            return best_id
    except Exception as e:
        print(f"  ⚠️  カテゴリ自動取得失敗: {e}")

    print(f"  🏷️  デフォルトカテゴリ: {EBAY_CATEGORY_MAP['default']}")
    return EBAY_CATEGORY_MAP["default"]


# ──────────────────────────────────────────────────────────
# Anthropic APIで日本語タイトル→英語eBayタイトルに変換
# ──────────────────────────────────────────────────────────

def translate_title_for_ebay(japanese_title: str, brand: str = "", model: str = "") -> str:
    """
    日本語タイトルをeBay Cassiniアルゴリズム最適化済み英語タイトルに変換

    2026年eBay SEOロジック（Cassiniアルゴリズム対応）:
    1. Brand + Model を先頭に配置（最重要キーワードを前半に）
    2. 80文字をフル活用（未使用スペースは機会損失）
    3. フィラーワード禁止（WOW/Amazing/Look等）
    4. キーワード重複なし
    5. 買い手が検索する自然な言葉を使用
    6. 具体的スペック・互換性・カテゴリ属性を含む
    """
    prompt = f"""You are an eBay SEO expert. Convert this Japanese product title to an optimized English eBay listing title.

STRICT RULES (2026 Cassini Algorithm):
1. Start with Brand name and Model number (most important keywords first)
2. Use EXACTLY 80 characters or as close as possible (every character = ranking opportunity)
3. Include: Brand + Model + Key Feature + Condition/Type + Specs
4. NO filler words: WOW, Amazing, Look, Nice, Great, L@@K, !!!
5. NO keyword stuffing or repetition
6. Use natural buyer search language (how buyers actually search)
7. Include compatibility info if applicable (e.g. "for iPhone 15")
8. Add "New" at end if space allows
9. Use Title Case

Product info:
- Japanese title: {japanese_title}
- Brand: {brand or "extract from title"}
- Model: {model or "extract from title"}

Reply with ONLY the optimized English title. Nothing else. No quotes."""

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
                "max_tokens": 120,
                "messages": [{"role": "user", "content": prompt}]
            },
            timeout=15,
        )
        data = resp.json()
        if data.get("content"):
            translated = data["content"][0]["text"].strip().strip('"').strip("'")[:80]
            print(f"  🌐 SEO最適化タイトル: {translated} ({len(translated)}文字)")
            return translated
    except Exception as e:
        print(f"  ⚠️  タイトル翻訳失敗: {e}")

    import re
    fallback = re.sub(r'[^\x00-\x7F]+', ' ', japanese_title).strip()[:80]
    return fallback or japanese_title[:80]

def build_listing_data(asin: str, keepa_data: dict, config: dict) -> dict:
    """
    KeepaデータからeBay出品に必要なデータを生成する

    - タイトル: Amazonタイトルをそのまま使用（80文字制限）
    - 説明文: テンプレートを自動生成
    - 価格: 仕入れ値から利益率・手数料込みで計算
    """
    amazon_price  = keepa_data["current_price"]
    sell_price    = calc_sell_price(amazon_price, config)
    jp_title      = keepa_data.get("title", "")
    brand         = keepa_data.get("brand", "")
    manufacturer  = keepa_data.get("manufacturer", "")
    model         = keepa_data.get("model", "") or keepa_data.get("partNumber", "")
    title         = translate_title_for_ebay(jp_title, brand=brand, model=model)

    description = build_description(keepa_data, amazon_price)

    upc = keepa_data.get("upc", "Does not apply")
    mpn = keepa_data.get("mpn", "Does Not Apply")

    item_specifics = {}
    item_specifics["Brand"]    = brand or "Does Not Apply"
    item_specifics["MPN"]      = mpn
    if manufacturer:
        item_specifics["Manufacturer"] = manufacturer
    item_specifics["Country/Region of Manufacture"] = "Japan"

    # タイトルからeBayの最適カテゴリを自動取得
    # JANコード（EAN）を優先してカテゴリを自動取得
    jan_code    = keepa_data.get("upc", "") or ""
    category_id = get_best_category(config["EBAY_TOKEN"], title, jan_code=jan_code)

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
- Price in JPY: ¥{amazon_price_jpy:,.0f}

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
        if data.get("content"):
            html = data["content"][0]["text"].strip()
            # コードブロックを除去
            if html.startswith("```"):
                html = html.split("\n", 1)[1].rsplit("```", 1)[0]
            print(f"  📝 SEO説明文生成完了（{len(html)}文字）")
            return html
    except Exception as e:
        print(f"  ⚠️  説明文生成失敗: {e}")

    # フォールバック
    return f"""
<div style="font-family:Arial,sans-serif;max-width:800px;line-height:1.6;">
  <h3>About This Item</h3>
  <table border="1" cellpadding="8" style="border-collapse:collapse;width:100%;">
    <tr><td><b>Brand</b></td><td>{brand}</td></tr>
    <tr><td><b>Model</b></td><td>{model or "See title"}</td></tr>
    <tr><td><b>Condition</b></td><td>Brand New</td></tr>
    <tr><td><b>Origin</b></td><td>Japan</td></tr>
    <tr><td><b>Rating</b></td><td>{rating}/5 ({reviews} reviews on Amazon Japan)</td></tr>
  </table>
  <h3>Shipping from Japan</h3>
  <p>✅ Ships directly from Japan | ✅ Tracking provided | ✅ 7-14 business days</p>
  <p><small>Import duties may apply in your country.</small></p>
</div>
"""


def calc_sell_price(amazon_price_jpy: float, config: dict) -> float:
    """Amazon円価格 → eBayドル売値"""
    usd  = amazon_price_jpy / config["JPY_TO_USD"]
    usd += config["SHIPPING_USD"]
    usd  = usd / (1 - config["EBAY_FEE_RATE"])
    usd  = usd / (1 - config["TARGET_MARGIN"])
    return round(usd, 2)


# ──────────────────────────────────────────────────────────
# KeepaCheckerの拡張取得（出品用に詳細情報も取得）
# ──────────────────────────────────────────────────────────

def fetch_listing_details(keepa_api, asin: str) -> dict:
    """出品に必要な詳細情報をKeepaから取得"""
    try:
        products = keepa_api.api.query(
            [asin],
            domain='JP',
            history=False,  # 履歴不要（トークン節約）
            offers=20,      # 最小値20
            stock=True,
            wait=True,
        )
        print(f"    トークン残: {keepa_api.tokens_left}")
        if not products:
            return {}

        p = products[0]

        # 画像URL生成
        # Keepa Pythonライブラリは images キーに辞書リストを返す
        # {'l': '大画像ID.jpg', 'm': '中画像ID.jpg', ...}
        image_urls = []
        images = p.get("images") or []
        for img in images[:12]:
            if isinstance(img, dict):
                # 中画像(m)を優先、なければ大画像(l)
                img_id = img.get("m") or img.get("l") or ""
            else:
                img_id = str(img)
            if img_id and "+" not in img_id:  # +を含むURLは無効になる場合がある
                image_urls.append(
                    f"https://images-na.ssl-images-amazon.com/images/I/{img_id}"
                )
            elif img_id:
                # +を%2Bにエンコード
                img_id_encoded = img_id.replace("+", "%2B")
                image_urls.append(
                    f"https://images-na.ssl-images-amazon.com/images/I/{img_id_encoded}"
                )

        # 商品特徴
        features = p.get("features", []) or []

        # 現在価格（複数ソースを順番に試す）
        # csv[0]=Amazon直販, csv[1]=新品出品者, csv[3]=新品最安値
        # Keepaの価格単位: 円そのまま（/100不要）
        csv = p.get("csv", []) or []
        price = 0
        for idx in [0, 1, 3]:
            if idx >= len(csv):
                continue
            series = csv[idx]
            if series:
                valid = [x for x in series if x and x > 0]
                if valid:
                    price = valid[-1]  # 円そのまま
                    break

        in_stock = price > 0
        print(f"  💴 取得価格: ¥{price:,.0f} / 在庫: {'あり' if in_stock else 'なし'}")

        upc_list = p.get("upcList") or []
        ean_list = p.get("eanList") or []
        part_num = p.get("partNumber") or ""
        model    = p.get("model") or ""

        # JANコード: EANを優先（日本商品はEANがJANコードに相当）
        upc = ean_list[0] if ean_list else (upc_list[0] if upc_list else "Does not apply")
        mpn = part_num or model or "Does Not Apply"

        return {
            "title":         p.get("title", ""),
            "brand":         p.get("brand", "") or "Does Not Apply",
            "manufacturer":  p.get("manufacturer", ""),
            "features":      features,
            "image_urls":    image_urls,
            "current_price": price,
            "in_stock":      in_stock,
            "rating":        (p.get("avgRating") or 0) / 10,
            "review_count":  p.get("reviewCount", 0),
            "upc":           upc,
            "mpn":           mpn,
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

        # eBayに出品
        result = lister.list_item(listing)

        if result["success"]:
            ebay_id = result["item_id"]
            print(f"  ✅ 出品成功！ eBay商品ID: {ebay_id}")

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