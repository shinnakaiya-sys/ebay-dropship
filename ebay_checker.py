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
                "item_id":       item_id,
                "current_price": float(price_str),
                "is_active":     listing_status == "Active",
                "quantity":      quantity,
                "title":         title,
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
    # 出品停止（在庫切れ時）
    # ──────────────────────────────────────────────────────
    def end_listing(self, item_id: str) -> bool:
        """出品を終了する（在庫切れ時）"""
        xml_body = f"""<?xml version="1.0" encoding="utf-8"?>
<EndItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">
  <RequesterCredentials>
    <eBayAuthToken>{self.token}</eBayAuthToken>
  </RequesterCredentials>
  <ItemID>{item_id}</ItemID>
  <EndingReason>NotAvailable</EndingReason>
</EndItemRequest>"""

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

    def _empty_result(self, item_id: str) -> dict:
        return {
            "item_id": item_id,
            "current_price": 0,
            "is_active": False,
            "quantity": 0,
            "title": "",
        }
