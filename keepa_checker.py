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
        import requests
        self._api_key = api_key
        self.api = keepa.Keepa(api_key)
        # /token エンドポイントで実際のトークン数を取得（トークンを消費しない）
        try:
            resp = requests.get(
                "https://api.keepa.com/token",
                params={"key": api_key},
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                print(f"  🔑 Keepa /token レスポンス: {data}")
                self.api.tokens_left = data.get("tokensLeft", self.api.tokens_left)
        except Exception as e:
            print(f"  ⚠️  Keepa /token 取得失敗: {e}")
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
        for attempt in range(3):
            try:
                products = self.api.query(
                    [asin],
                    domain='JP',       # Amazon.co.jp
                    history=True,      # CSVデータ取得に必要
                    offers=20,         # 最小値20
                    stock=True,
                    wait=True,
                    progress_bar=False,
                )
                break
            except Exception as e:
                if attempt < 2 and "timed out" in str(e).lower():
                    print(f"  ⏳  Keepaタイムアウト、リトライ ({attempt+1}/3)...")
                    time.sleep(5)
                    continue
                print(f"  ⚠️  Keepa取得エラー ({asin}): {e}")
                return self._empty_result(asin)
        else:
            return self._empty_result(asin)

        try:
            print(f"    トークン残: {self.api.tokens_left}")

            if not products:
                return self._empty_result(asin)

            p = products[0]
            csv = p.get("csv", [])

            # カート価格（Buy Box）= 実際に購入できる価格 csv[18]
            buy_box_price = self._get_latest_price(csv, index=18)
            # Amazon直販価格 csv[0]
            amazon_price  = self._get_latest_price(csv, index=0)
            # 新品最安値（サードパーティ含む）csv[1]
            new_min_price = self._get_latest_price(csv, index=1)

            # カート価格 → Amazon直販 → 新品最安値 の優先順
            best_price = new_min_price or amazon_price or 0

            # 在庫判定
            in_stock = best_price > 0

            print(f"    Buy Box: {buy_box_price}円 / Amazon直販: {amazon_price}円 / 新品最安値: {new_min_price}円")

            return {
                "asin":          asin,
                "current_price": best_price,
                "in_stock":      in_stock,
                "lowest_fba":    new_min_price or 0,
                "rating":        p.get("avgRating", 0) / 10,
                "review_count":  p.get("reviewCount", 0),
            }

        except Exception as e:
            print(f"  ⚠️  Keepa取得エラー ({asin}): {e}")
            return self._empty_result(asin)

    def _get_latest_price(self, csv: list, index: int) -> float:
        """csvデータから最新価格を取得（Keepaは円そのまま）
        CSV形式: [timestamp, price, timestamp, price, ...] の交互
        価格なし/在庫切れは -1 で表現"""
        try:
            series = csv[index] if len(csv) > index else None
            if not series:
                return 0
            # 奇数インデックス（価格部分のみ）を抽出
            prices = [v for v in series[1::2] if v and v > 0]
            return prices[-1] if prices else 0
        except Exception:
            return 0


    def jan_to_asin(self, jan_code: str) -> str:
        """
        JANコード（EAN）からASINを逆引きする
        KeepaのSearch APIを使用
        """
        import requests
        from config import CONFIG

        try:
            resp = requests.get(
                "https://api.keepa.com/search",
                params={
                    "key":    CONFIG["KEEPA_API_KEY"],
                    "domain": "5",
                    "type":   "product",
                    "term":   jan_code,
                },
                timeout=15,
            )
            data = resp.json()
            products = data.get("products", [])
            if products:
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
            "error": True,
        }