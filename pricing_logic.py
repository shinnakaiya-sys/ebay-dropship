"""
pricing_logic.py
================
scrape_and_adjust_v2.py の「純粋ロジック層」。

外部I/O（Selenium・Sheets・eBay API）に一切依存しないため、
ブラウザやAPIキーなしでユニットテストできる。

既存の scrape_and_adjust.py からの修正点:
  * 送料パース: 先頭 '+' を必須にしない（"$5.00 shipping" を取りこぼさない）
  * セラー判定: 部分一致 → 完全一致（"kaworu" が "kaworu_shop2" に誤爆しない）
  * 価格パース: "$10.00 to $20.00" のレンジ表記は最小値を採用
  * 数値変換: "$1,234.56" 等の記号付き文字列を安全に float 化
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# eBay 検索URLに強制するクエリパラメータ
#   LH_BIN=1            即決のみ
#   LH_ItemCondition    1000 = 新品
#   _sop=15             送料込み安い順
#   LH_PrefLoc=2        日本発送
FORCED_SEARCH_PARAMS = {
    "LH_BIN": "1",
    "LH_ItemCondition": "1000",
    "_sop": "15",
    "LH_PrefLoc": "2",
}

_MONEY_USD = re.compile(r"\$\s*([0-9,]+(?:\.\d+)?)")
_MONEY_JPY = re.compile(r"(?:JPY|¥|円)\s*([0-9,]+(?:\.\d+)?)", re.I)
# セラー表記 "kaworu2021 (1,234) 99.5%" から ID 部分だけを取り出す
_SELLER_TOKEN = re.compile(r"^([A-Za-z0-9._\-*]+)")
# "12 watchers" / "3 sold" などをセラー名と誤認しないためのパターン
_NOT_SELLER = re.compile(r"^\d[\d,]*\s+(watchers?|sold|bids?)\s*$", re.I)


# ==========================================
# データ構造
# ==========================================
@dataclass
class RawItem:
    """検索結果1件ぶんの生テキスト（DOM抽出の結果）"""
    item_id: str
    seller_text: str = ""
    price_text: str = ""
    shipping_text: str = ""


@dataclass
class ScrapeResult:
    """1商品ぶんのスクレイプ結果"""
    lowest_price: float = 0.0          # 競合最安値（送料込み USD）。競合なしは 0.0
    my_rank: int | None = None         # 最安値順で自分が何番目か
    count: int = 0                     # 有効な出品件数（自分を含む）
    competitor_totals: list[float] = field(default_factory=list)

    @property
    def has_rival(self) -> bool:
        return self.lowest_price > 0


@dataclass
class PriceDecision:
    """価格決定の結果"""
    new_price: float
    undercut_price: float              # 競合 - undercut（原価下限を無視した値）
    cost_floor: float
    floor_applied: bool                # 原価下限に張り付いたか


# ==========================================
# 数値・通貨のパース
# ==========================================
def to_float(value, default: float = 0.0) -> float:
    """'$1,234.56' / '1,234' / None / '' を安全に float 化する"""
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return float(value)
    cleaned = re.sub(r"[^\d.\-]", "", str(value))
    if not cleaned or cleaned in ("-", ".", "-."):
        return default
    try:
        return float(cleaned)
    except ValueError:
        return default


def parse_price_usd(text: str, jpy_rate: float) -> float:
    """
    価格テキストを USD に変換する。
    レンジ表記（"$10.00 to $20.00"）は最小値を採用する。
    """
    if not text:
        return 0.0
    usd = [float(m.replace(",", "")) for m in _MONEY_USD.findall(text)]
    if usd:
        return round(min(usd), 2)
    jpy = [float(m.replace(",", "")) for m in _MONEY_JPY.findall(text)]
    if jpy and jpy_rate > 0:
        return round(min(jpy) / jpy_rate, 2)
    return 0.0


def parse_shipping_usd(text: str, jpy_rate: float) -> float:
    """
    送料テキストを USD に変換する。

    旧実装は '+$5.00' 形式（先頭に '+'）しかマッチせず、
    '$5.00 shipping' のような表記で送料を 0 と誤認していた。
    """
    if not text:
        return 0.0
    t = text.strip()
    if not t or "free" in t.lower():
        return 0.0
    m = _MONEY_USD.search(t)
    if m:
        return round(float(m.group(1).replace(",", "")), 2)
    m = _MONEY_JPY.search(t)
    if m and jpy_rate > 0:
        return round(float(m.group(1).replace(",", "")) / jpy_rate, 2)
    return 0.0


# ==========================================
# URL 正規化
# ==========================================
def normalize_search_url(url: str) -> str:
    """検索URLに「新品・即決・日本発送・送料込み安い順」を強制する"""
    from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

    parsed = urlparse(url)
    params = parse_qs(parsed.query, keep_blank_values=True)
    flat = {k: v[0] for k, v in params.items()}
    flat.update(FORCED_SEARCH_PARAMS)
    return urlunparse(parsed._replace(query=urlencode(flat)))


# ==========================================
# 自分の出品判定
# ==========================================
def seller_name(text: str) -> str:
    """'kaworu2021 (1,234) 99.5%' → 'kaworu2021'（小文字化）"""
    if not text:
        return ""
    t = text.strip()
    if _NOT_SELLER.match(t):
        return ""
    m = _SELLER_TOKEN.match(t)
    return m.group(1).lower() if m else ""


def is_my_listing(item: RawItem, my_seller_id: str, my_item_id: str) -> bool:
    """
    商品IDが一致すれば自分の出品（ログイン不要・最優先）。
    次点でセラーIDの「完全一致」で判定する。

    旧実装は `my_seller_id in seller` の部分一致だったため、
    'kaworu' が 'kaworu_shop2'（他人）にマッチして競合から除外され、
    実際より高い最安値を算出してしまう不具合があった。
    """
    my_item_clean = re.sub(r"\D", "", my_item_id or "")
    if my_item_clean and item.item_id == my_item_clean:
        return True
    my_seller = (my_seller_id or "").strip().lower()
    if not my_seller:
        return False
    return seller_name(item.seller_text) == my_seller


# ==========================================
# 検索結果の集計
# ==========================================
def analyze_items(items: list[RawItem], my_seller_id: str, my_item_id: str,
                  jpy_rate: float) -> ScrapeResult:
    """RawItem のリストから競合最安値・自分の順位・出品数を求める"""
    result = ScrapeResult()
    my_total: float | None = None

    for item in items:
        if not item.item_id:
            continue
        result.count += 1

        price = parse_price_usd(item.price_text, jpy_rate)
        shipping = parse_shipping_usd(item.shipping_text, jpy_rate)
        total = round(price + shipping, 2) if price > 0 else None

        if is_my_listing(item, my_seller_id, my_item_id):
            if my_total is None and total is not None:
                my_total = total
            continue

        if total is not None:
            result.competitor_totals.append(total)

    if result.competitor_totals:
        result.lowest_price = round(min(result.competitor_totals), 2)

    if my_total is not None:
        # eBayの「価格の安い順」ソートは実際には厳密な昇順ではないため、
        # DOM上の出現位置ではなく実際の価格比較で順位を算出する
        # （自分より安い競合の件数 + 1）
        result.my_rank = sum(1 for t in result.competitor_totals if t < my_total) + 1

    return result


# ==========================================
# 価格決定
# ==========================================
def decide_new_price(rival_price: float, cost_floor: float,
                     undercut: float = 0.01) -> PriceDecision:
    """競合最安値を undercut ぶん下回る価格を出す。ただし原価下限は割らない。"""
    target = round(rival_price - undercut, 2)
    new_price = max(target, cost_floor)
    return PriceDecision(
        new_price=round(new_price, 2),
        undercut_price=target,
        cost_floor=round(cost_floor, 2),
        floor_applied=new_price > target,
    )


# ==========================================
# 商品行のバリデーション
# ==========================================
def has_rival_price(product: dict) -> bool:
    """J列（競合最安値）が数値の商品だけを価格調整の対象にする"""
    raw = str(product.get("競合最安値(USD)") or "").strip()
    if not raw or raw == "競合なし":
        return False
    try:
        float(re.sub(r"[^\d.\-]", "", raw))
        return True
    except ValueError:
        return False


def product_identifier(product: dict) -> str:
    """ASIN 優先、なければ JANコードを主キーとして使う"""
    return str(product.get("ASIN") or product.get("JANコード") or "").strip()
