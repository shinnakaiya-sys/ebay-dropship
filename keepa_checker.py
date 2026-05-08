"""
Keepa APIで Amazon.co.jp の価格・在庫を確認するモジュール
"""

import keepa
import time

# トークンが少ないときの閾値
MIN_TOKENS_CHECK = 20   # check()/jan_to_asin()を実行する最低トークン数
MIN_TOKENS_LIST  = 50   # fetch_listing_details()を実行する最低トークン数


class KeepaChecker:
    def __init__(self, api_key):
        self.api = keepa.Keepa(api_key)
        # 初期化直後はtoken情報が古い場合があるため少し待って再取得
        time.sleep(2)
        print(f"✅ Keepa接続（残りトークン: {self.api.tokens_left}）")

    @property
    def tokens_left(self) -> int:
        return self.api.tokens_left

    def _check_tokens(self, required: int, label: str = "") -> bool:
        """トークンが足りない場合に警告してFalseを返す"""
        if self.api.tokens_left < required:
            print(f"  ⚠️  Keepaトークン不足（残: {self.api.tokens_left} / 必要: {required}）{label}")
            return False
        return True

    def check(self, asin: str) -> dict:
        """
        ASINの現在価格と在庫状況を返す

        Returns:
            {
                "asin": str,
                "current_price": float,   # 現在のAmazon価格（円）
                "in_stock": bool,          # 在庫あり/なし
                "lowest_fba": float,       # 最安FBA価格
                "rating": float,
                "review_count": int,
            }
        """
        try:
            products = self.api.query(
                [asin],
                domain='JP',       # Amazon.co.jp
                history=False,     # 履歴不要（高速化・トークン節約）
                offers=5,          # offers数を減らしてトークン節約
                stock=True,
                wait=True,
            )
            print(f"    トークン残: {self.api.tokens_left}")

            if not products:
                return self._empty_result(asin)

            p = products[0]

            # Amazon直販価格（csv[0]）
            current_price = self._get_latest_price(p.get("csv", []), index=0)

            # 新品最安値（csv[1]）- Amazon以外の出品者
            new_price = self._get_latest_price(p.get("csv", []), index=1)

            # 使える価格を選択（Amazon直販 優先）
            best_price = current_price or new_price or 0

            # 在庫判定: Keepaの在庫データ or 価格が存在すれば在庫あり
            in_stock = best_price > 0

            return {
                "asin":         asin,
                "current_price": best_price,
                "in_stock":      in_stock,
                "lowest_fba":    new_price or 0,
                "rating":        p.get("avgRating", 0) / 10,
                "review_count":  p.get("reviewCount", 0),
            }

        except Exception as e:
            print(f"  ⚠️  Keepa取得エラー ({asin}): {e}")
            return self._empty_result(asin)

    def _get_latest_price(self, csv: list, index: int) -> float:
        """csvデータから最新価格を取得（Keepaは円そのまま）"""
        try:
            series = csv[index] if len(csv) > index else None
            if not series:
                return 0
            prices = [x for x in series if x and x > 0]
            return prices[-1] if prices else 0
        except Exception:
            return 0


    def jan_to_asin(self, jan_code: str) -> str:
        """
        JANコード（EAN）からASINを逆引きする
        KeepaのqueryメソッドにEANを直接渡す
        """
        try:
            # KeepaはEAN/JANコードをリストで渡す（wait=Trueでトークン不足時は自動待機）
            products = self.api.query(
                [jan_code],    # リスト形式で渡す
                domain="JP",
                history=False,
                offers=0,
                stock=False,
                wait=True,
            )
            if products and len(products) > 0:
                asin = products[0].get("asin", "")
                if asin:
                    print(f"  🔍 JAN→ASIN変換: {jan_code} → {asin}")
                    return asin
        except Exception as e:
            print(f"  ⚠️  JAN→ASIN変換失敗: {e}")
        return ""

    def check_by_jan(self, jan_code: str) -> dict:
        """
        JANコードから商品情報を取得する
        1. JANコード→ASIN変換
        2. ASINで通常のcheck()を実行
        """
        asin = self.jan_to_asin(jan_code)
        if not asin:
            return {"asin": "", "current_price": 0, "in_stock": False,
                    "lowest_fba": 0, "rating": 0, "review_count": 0, "jan_code": jan_code}
        result = self.check(asin)
        result["jan_code"] = jan_code
        return result

    def _empty_result(self, asin: str) -> dict:
        return {
            "asin": asin,
            "current_price": 0,
            "in_stock": False,
            "lowest_fba": 0,
            "rating": 0,
            "review_count": 0,
        }