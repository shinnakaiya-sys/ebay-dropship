"""
eBay Trading API で出品状態の確認・価格更新・停止/再開を行うモジュール

APIドキュメント:
  https://developer.ebay.com/api-docs/sell/inventory/resources/methods
"""

import requests
import xml.etree.ElementTree as ET


EBAY_API_URL = "https://api.ebay.com/ws/api.dll"

# サンドボックス（テスト環境）の場合はこちら
# EBAY_API_URL = "https://api.sandbox.ebay.com/ws/api.dll"


class EbayChecker:
    def __init__(self, token: str):
        self.token = token
        self.headers = {
            "X-EBAY-API-SITEID":        "0",          # 0=US, 15=AU, 3=UK
            "X-EBAY-API-COMPATIBILITY-LEVEL": "967",
            "X-EBAY-API-IAF-TOKEN":     token,
            "Content-Type":             "text/xml",
        }

    # ──────────────────────────────────────────────────────
    # 出品状態を取得
    # ──────────────────────────────────────────────────────
    def check(self, item_id: str) -> dict:
        """
        eBay出品IDの現在状態を取得

        Returns:
            {
                "item_id": str,
                "current_price": float,  # USD
                "is_active": bool,
                "quantity": int,
                "title": str,
            }
        """
        xml_body = f"""<?xml version="1.0" encoding="utf-8"?>
<GetItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">
  <RequesterCredentials>
    <eBayAuthToken>{self.token}</eBayAuthToken>
  </RequesterCredentials>
  <ItemID>{item_id}</ItemID>
  <DetailLevel>ReturnAll</DetailLevel>
</GetItemRequest>"""

        try:
            resp = requests.post(
                EBAY_API_URL,
                headers={**self.headers, "X-EBAY-API-CALL-NAME": "GetItem"},
                data=xml_body.encode("utf-8"),
                timeout=15,
            )
            root = ET.fromstring(resp.text)
            ns = {"e": "urn:ebay:apis:eBLBaseComponents"}

            # ステータス確認
            ack = root.findtext("e:Ack", namespaces=ns)
            if ack not in ("Success", "Warning"):
                return self._empty_result(item_id)

            item = root.find("e:Item", ns)
            listing_status = item.findtext("e:SellingStatus/e:ListingStatus", namespaces=ns)
            price_str = item.findtext("e:SellingStatus/e:CurrentPrice", namespaces=ns) or "0"
            quantity = int(item.findtext("e:Quantity", namespaces=ns) or 0)
            title = item.findtext("e:Title", namespaces=ns) or ""

            return {
                "item_id":        item_id,
                "current_price":  float(price_str),
                "is_active":      listing_status == "Active",
                "listing_status": listing_status,
                "quantity":       quantity,
                "title":          title,
            }

        except Exception as e:
            print(f"  ⚠️  eBay取得エラー ({item_id}): {e}")
            return self._empty_result(item_id)

    # ──────────────────────────────────────────────────────
    # 価格更新
    # ──────────────────────────────────────────────────────
    def revise_price(self, item_id: str, new_price_usd: float) -> bool:
        """出品価格を更新する"""
        xml_body = f"""<?xml version="1.0" encoding="utf-8"?>
<ReviseItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">
  <RequesterCredentials>
    <eBayAuthToken>{self.token}</eBayAuthToken>
  </RequesterCredentials>
  <Item>
    <ItemID>{item_id}</ItemID>
    <StartPrice>{new_price_usd}</StartPrice>
  </Item>
</ReviseItemRequest>"""

        return self._call_api("ReviseItem", xml_body, item_id, "価格更新")

    # ──────────────────────────────────────────────────────
    # 在庫数を0に更新（在庫切れ時・出品は残す）
    # ──────────────────────────────────────────────────────
    def end_listing(self, item_id: str) -> bool:
        """在庫数を0に更新する（出品停止ではなく在庫0にする）"""
        return self.update_quantity(item_id, 0)

    def update_quantity(self, item_id: str, quantity: int) -> bool:
        """在庫数を更新する"""
        xml_body = (
            "<?xml version=" + chr(34) + "1.0" + chr(34) + " encoding=" + chr(34) + "utf-8" + chr(34) + "?>"
            "<ReviseItemRequest xmlns=" + chr(34) + "urn:ebay:apis:eBLBaseComponents" + chr(34) + ">"
            "<RequesterCredentials>"
            "<eBayAuthToken>" + self.token + "</eBayAuthToken>"
            "</RequesterCredentials>"
            "<Item>"
            "<ItemID>" + str(item_id) + "</ItemID>"
            "<Quantity>" + str(quantity) + "</Quantity>"
            "</Item>"
            "</ReviseItemRequest>"
        )
        action = f"在庫数{quantity}に更新"
        return self._call_api("ReviseItem", xml_body, item_id, action)

    # ──────────────────────────────────────────────────────
    # 出品停止（完全終了が必要な場合のみ使用）
    # ──────────────────────────────────────────────────────
    def end_listing_permanently(self, item_id: str) -> bool:
        """出品を完全終了する（通常は使わない）"""
        xml_body = (
            "<?xml version=" + chr(34) + "1.0" + chr(34) + " encoding=" + chr(34) + "utf-8" + chr(34) + "?>"
            "<EndItemRequest xmlns=" + chr(34) + "urn:ebay:apis:eBLBaseComponents" + chr(34) + ">"
            "<RequesterCredentials>"
            "<eBayAuthToken>" + self.token + "</eBayAuthToken>"
            "</RequesterCredentials>"
            "<ItemID>" + str(item_id) + "</ItemID>"
            "<EndingReason>NotAvailable</EndingReason>"
            "</EndItemRequest>"
        )
        return self._call_api("EndItem", xml_body, item_id, "出品停止")

    # ──────────────────────────────────────────────────────
    # 出品再開（在庫復活時）
    # ──────────────────────────────────────────────────────
    def relist(self, item_id: str, new_price_usd: float) -> bool:
        """終了した出品を再開する"""
        xml_body = f"""<?xml version="1.0" encoding="utf-8"?>
<RelistItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">
  <RequesterCredentials>
    <eBayAuthToken>{self.token}</eBayAuthToken>
  </RequesterCredentials>
  <Item>
    <ItemID>{item_id}</ItemID>
    <StartPrice>{new_price_usd}</StartPrice>
    <Quantity>1</Quantity>
  </Item>
</RelistItemRequest>"""

        return self._call_api("RelistItem", xml_body, item_id, "再出品")

    # ──────────────────────────────────────────────────────
    # 共通APIコール
    # ──────────────────────────────────────────────────────
    def _call_api(self, call_name: str, xml_body: str, item_id: str, action: str) -> bool:
        try:
            resp = requests.post(
                EBAY_API_URL,
                headers={**self.headers, "X-EBAY-API-CALL-NAME": call_name},
                data=xml_body.encode("utf-8"),
                timeout=15,
            )
            root = ET.fromstring(resp.text)
            ns = {"e": "urn:ebay:apis:eBLBaseComponents"}
            ack = root.findtext("e:Ack", namespaces=ns)

            if ack in ("Success", "Warning"):
                print(f"  ✅ {action}成功: {item_id}")
                return True
            else:
                errors = root.findall(".//e:ShortMessage", ns)
                msg = errors[0].text if errors else "不明なエラー"
                print(f"  ❌ {action}失敗 ({item_id}): {msg}")
                return False

        except Exception as e:
            print(f"  ❌ {action}エラー ({item_id}): {e}")
            return False

    # ──────────────────────────────────────────────────────
    # 日本発送セラーの最安値を取得（Finding API）
    # ──────────────────────────────────────────────────────
    def _get_browse_token(self, app_id: str, client_secret: str) -> str:
        """OAuth Client Credentialsでアプリトークンを取得"""
        import base64
        credentials = base64.b64encode(f"{app_id}:{client_secret}".encode()).decode()
        resp = requests.post(
            "https://api.ebay.com/identity/v1/oauth2/token",
            headers={
                "Authorization": f"Basic {credentials}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data="grant_type=client_credentials&scope=https%3A%2F%2Fapi.ebay.com%2Foauth%2Fapi_scope",
            timeout=10,
        )
        return resp.json().get("access_token", "")

    def get_jp_search_stats(self, jan_code: str, seller_id: str, app_id: str, client_secret: str = "") -> dict:
        """
        JANコード（EAN）で日本発送出品を検索し、競合最安値・出品数・自分の順位を返す。

        Finding API の findItemsByProduct（EAN検索）を使うため、
        出品タイトルにJANコードが含まれていない商品も正確にヒットする。

        Returns:
            {
                "lowest_price": float,    # 競合最安値（送料込み、USD）
                "count": int,             # 日本発送の出品総数
                "my_rank": int | None,    # 自分の順位（見つからない場合はNone）
            }
        """
        if not app_id or not jan_code:
            return {"lowest_price": 0.0, "count": 0, "my_rank": None}

        try:
            items, total_entries = self._find_jp_items_by_ean(jan_code, app_id)
        except Exception as e:
            print(f"  ⚠️  競合価格・順位取得エラー: {e}")
            return {"lowest_price": 0.0, "count": 0, "my_rank": None}

        if not items:
            return {"lowest_price": 0.0, "count": total_entries, "my_rank": None}

        lowest_competitor = float("inf")
        my_rank = None

        for rank, item in enumerate(items, start=1):
            is_mine = (
                seller_id
                and item["seller"].lower() == seller_id.lower()
            )
            if is_mine:
                if my_rank is None:
                    my_rank = rank
            else:
                total_price = item["price"] + item["shipping"]
                if total_price > 0 and total_price < lowest_competitor:
                    lowest_competitor = total_price

        return {
            "lowest_price": round(lowest_competitor, 2) if lowest_competitor != float("inf") else 0.0,
            "count": total_entries,
            "my_rank": my_rank,
        }

    def _find_jp_items_by_ean(self, ean: str, app_id: str) -> tuple[list, int]:
        """
        Finding API の findItemsByProduct（EAN）で日本発送出品を最安値順に取得。
        タイトルにEANが含まれない出品も商品カタログ照合でヒットする。

        Returns: (items_list, total_entries)
            items_list: [{"seller": str, "price": float, "shipping": float}, ...]
        """
        all_items = []
        total_entries = 0
        page = 1

        while True:
            resp = requests.get(
                "https://svcs.ebay.com/services/search/FindingService/v1",
                params={
                    "OPERATION-NAME":                "findItemsByProduct",
                    "SERVICE-VERSION":               "1.0.0",
                    "SECURITY-APPNAME":              app_id,
                    "RESPONSE-DATA-FORMAT":          "JSON",
                    "productId.@type":               "EAN",
                    "productId":                     ean,
                    "itemFilter(0).name":            "LocatedIn",
                    "itemFilter(0).value":           "JP",
                    "itemFilter(1).name":            "ListingType",
                    "itemFilter(1).value(0)":        "FixedPrice",
                    "itemFilter(1).value(1)":        "StoreInventory",
                    "sortOrder":                     "PricePlusShippingLowest",
                    "paginationInput.entriesPerPage": "100",
                    "paginationInput.pageNumber":    str(page),
                },
                timeout=10,
            )
            data = resp.json()
            response = data.get("findItemsByProductResponse", [{}])[0]

            if response.get("ack", [""])[0] not in ("Success", "Warning"):
                error = response.get("errorMessage", [{}])[0].get("error", [{}])[0].get("message", ["不明"])[0]
                print(f"  ⚠️  Finding APIエラー: {error}")
                break

            if page == 1:
                total_entries = int(
                    response.get("paginationOutput", [{}])[0].get("totalEntries", ["0"])[0]
                )

            items = response.get("searchResult", [{}])[0].get("item", [])
            if not items:
                break

            for item in items:
                seller = item.get("sellerInfo", [{}])[0].get("sellerUserName", [""])[0]
                price  = float(
                    item.get("sellingStatus", [{}])[0]
                        .get("currentPrice", [{"__value__": "0"}])[0]
                        .get("__value__", "0")
                )
                shipping_info = item.get("shippingInfo", [{}])[0]
                shipping = float(
                    shipping_info.get("shippingServiceCost", [{"__value__": "0"}])[0]
                                 .get("__value__", "0")
                )
                all_items.append({"seller": seller, "price": price, "shipping": shipping})

            if len(all_items) >= total_entries or len(all_items) >= 200:
                break
            page += 1

        return all_items, total_entries

    # 後方互換のため残す
    def get_jp_lowest_price(self, jan_code: str, app_id: str, client_secret: str = "", exclude_item_id: str = "") -> dict:
        stats = self.get_jp_search_stats(jan_code, "", app_id, client_secret)
        return {"lowest_price": stats["lowest_price"], "count": stats["count"]}

    # ──────────────────────────────────────────────────────
    # 在庫復活（Active+在庫0 → ReviseItem、Ended → RelistItem）
    # ──────────────────────────────────────────────────────
    def restore_listing(self, item_id: str, listing_status: str, new_price_usd: float) -> bool:
        """
        eBay出品を復活させる
        - Active（在庫0で非表示）→ ReviseItem で数量1・価格更新
        - Ended（終了済み）       → RelistItem で再出品
        """
        if listing_status == "Active":
            # 在庫0のまま非表示になっているケース → 数量と価格を更新
            xml_body = (
                "<?xml version=" + chr(34) + "1.0" + chr(34) + " encoding=" + chr(34) + "utf-8" + chr(34) + "?>"
                "<ReviseItemRequest xmlns=" + chr(34) + "urn:ebay:apis:eBLBaseComponents" + chr(34) + ">"
                "<RequesterCredentials>"
                "<eBayAuthToken>" + self.token + "</eBayAuthToken>"
                "</RequesterCredentials>"
                "<Item>"
                "<ItemID>" + str(item_id) + "</ItemID>"
                "<Quantity>1</Quantity>"
                "<StartPrice>" + str(new_price_usd) + "</StartPrice>"
                "</Item>"
                "</ReviseItemRequest>"
            )
            return self._call_api("ReviseItem", xml_body, item_id, "在庫復活（Active→数量1）")
        else:
            # Ended等 → 再出品
            xml_body = f"""<?xml version="1.0" encoding="utf-8"?>
<RelistItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">
  <RequesterCredentials>
    <eBayAuthToken>{self.token}</eBayAuthToken>
  </RequesterCredentials>
  <Item>
    <ItemID>{item_id}</ItemID>
    <StartPrice>{new_price_usd}</StartPrice>
    <Quantity>1</Quantity>
  </Item>
</RelistItemRequest>"""
            return self._call_api("RelistItem", xml_body, item_id, "再出品（Ended→Relist）")

    def get_my_rank_in_search(self, jan_code: str, seller_id: str, app_id: str, client_secret: str) -> int | None:
        """
        JANコードでeBayを最安値順に検索し、自分のセラーIDが何番目に表示されるかを返す。
        見つからない場合は None を返す。

        Returns:
            int: 1始まりの順位（例: 3 → 3番目）
            None: 出品が見つからない
        """
        if not all([jan_code, seller_id, app_id, client_secret]):
            return None

        try:
            token = self._get_browse_token(app_id, client_secret)
            if not token:
                return None

            rank = 0
            offset = 0
            limit = 50  # Browse APIの最大値

            while True:
                resp = requests.get(
                    "https://api.ebay.com/buy/browse/v1/item_summary/search",
                    headers={"Authorization": f"Bearer {token}"},
                    params={
                        "q":      str(jan_code),
                        "filter": "itemLocationCountry:JP",
                        "sort":   "price",
                        "limit":  str(limit),
                        "offset": str(offset),
                    },
                    timeout=10,
                )
                data = resp.json()
                items = data.get("itemSummaries", [])
                if not items:
                    break

                for item in items:
                    rank += 1
                    item_seller = item.get("seller", {}).get("username", "")
                    if item_seller.lower() == seller_id.lower():
                        return rank

                # 全件取得済み or 最大200件でやめる
                total = data.get("total", 0)
                offset += limit
                if offset >= total or offset >= 200:
                    break

            return None

        except Exception as e:
            print(f"  ⚠️  順位取得エラー: {e}")
            return None

    def _empty_result(self, item_id: str) -> dict:
        return {
            "item_id":        item_id,
            "current_price":  0,
            "is_active":      False,
            "listing_status": "",
            "quantity":       0,
            "title":          "",
        }
