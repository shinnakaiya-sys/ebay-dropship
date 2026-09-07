"""
test_pricing_logic.py
======================
pricing_logic.py（scrape_and_adjust_v2.py の純粋ロジック層）のユニットテスト。
外部I/O（Selenium・Sheets・eBay API）を一切使わないため、
credentials.json や実ブラウザなしで実行できる。

実行方法:
  venv/bin/python -m pytest test_pricing_logic.py -v
  または
  venv/bin/python test_pricing_logic.py   （pytest未インストールでも動く簡易ランナー）
"""

from pricing_logic import (
    RawItem,
    to_float,
    parse_price_usd,
    parse_shipping_usd,
    normalize_search_url,
    seller_name,
    is_my_listing,
    analyze_items,
    decide_new_price,
    has_rival_price,
    product_identifier,
)

JPY_RATE = 150.0


# ==========================================
# to_float
# ==========================================
def test_to_float_plain_number():
    assert to_float("12.5") == 12.5


def test_to_float_with_currency_symbol_and_commas():
    assert to_float("$1,234.56") == 1234.56


def test_to_float_empty_and_none():
    assert to_float("") == 0.0
    assert to_float(None) == 0.0
    assert to_float("   ") == 0.0


def test_to_float_invalid_returns_default():
    assert to_float("N/A", default=-1) == -1


def test_to_float_passthrough_numeric_types():
    assert to_float(42) == 42.0
    assert to_float(3.14) == 3.14


# ==========================================
# parse_price_usd
# ==========================================
def test_parse_price_usd_simple():
    assert parse_price_usd("$29.99", JPY_RATE) == 29.99


def test_parse_price_usd_with_comma():
    assert parse_price_usd("$1,299.00", JPY_RATE) == 1299.00


def test_parse_price_usd_range_takes_minimum():
    # eBayの「$10.00 to $20.00」表記は最安値側を採用する
    assert parse_price_usd("$10.00 to $20.00", JPY_RATE) == 10.00


def test_parse_price_usd_jpy_fallback():
    assert parse_price_usd("JPY 1,500", JPY_RATE) == round(1500 / JPY_RATE, 2)


def test_parse_price_usd_no_match():
    assert parse_price_usd("Best Offer", JPY_RATE) == 0.0


# ==========================================
# parse_shipping_usd
#   旧実装は '+$5.00' のように先頭に '+' がないと送料を取りこぼしていた。
#   新実装はこの取りこぼしを修正している。
# ==========================================
def test_parse_shipping_usd_with_plus_prefix():
    assert parse_shipping_usd("+$5.00 shipping", JPY_RATE) == 5.00


def test_parse_shipping_usd_without_plus_prefix_regression():
    # これが旧実装のバグ: '+' がないと 0.0 になっていた
    assert parse_shipping_usd("$5.00 shipping", JPY_RATE) == 5.00


def test_parse_shipping_usd_free():
    assert parse_shipping_usd("Free shipping", JPY_RATE) == 0.0
    assert parse_shipping_usd("Free International Shipping", JPY_RATE) == 0.0


def test_parse_shipping_usd_empty():
    assert parse_shipping_usd("", JPY_RATE) == 0.0
    assert parse_shipping_usd(None, JPY_RATE) == 0.0


def test_parse_shipping_usd_jpy():
    assert parse_shipping_usd("+JPY 1,500 shipping", JPY_RATE) == round(1500 / JPY_RATE, 2)


# ==========================================
# normalize_search_url
# ==========================================
def test_normalize_search_url_forces_params():
    url = "https://www.ebay.com/sch/i.html?_nkw=widget"
    out = normalize_search_url(url)
    assert "LH_BIN=1" in out
    assert "LH_ItemCondition=1000" in out
    assert "_sop=15" in out
    assert "LH_PrefLoc=2" in out
    assert "_nkw=widget" in out


def test_normalize_search_url_overrides_existing_params():
    url = "https://www.ebay.com/sch/i.html?_nkw=widget&LH_BIN=0&_sop=12"
    out = normalize_search_url(url)
    assert "LH_BIN=1" in out
    assert "_sop=15" in out
    assert "LH_BIN=0" not in out


# ==========================================
# seller_name / is_my_listing
#   旧実装は部分一致だったため 'kaworu' が 'kaworu_shop2' に誤爆していた。
# ==========================================
def test_seller_name_strips_extra_info():
    assert seller_name("kaworu2021 (1,234) 99.5%") == "kaworu2021"


def test_seller_name_ignores_watchers_text():
    assert seller_name("12 watchers") == ""
    assert seller_name("3 sold") == ""


def test_is_my_listing_by_item_id():
    # eBay商品ID列はプレーンな数字列（例: "123456789012"）で保存されている
    item = RawItem(item_id="123456789", seller_text="someoneelse")
    assert is_my_listing(item, my_seller_id="kaworu2021", my_item_id="123456789") is True


def test_is_my_listing_exact_seller_match():
    item = RawItem(item_id="999999999", seller_text="kaworu2021 (500) 100%")
    assert is_my_listing(item, my_seller_id="kaworu2021", my_item_id="") is True


def test_is_my_listing_rejects_partial_match_regression():
    # 旧実装のバグ回帰テスト: 'kaworu2021' は 'kaworu2021_shop2'（他人）に一致してはいけない
    item = RawItem(item_id="999999999", seller_text="kaworu2021_shop2")
    assert is_my_listing(item, my_seller_id="kaworu2021", my_item_id="") is False


def test_is_my_listing_no_match():
    item = RawItem(item_id="111", seller_text="rival_store")
    assert is_my_listing(item, my_seller_id="kaworu2021", my_item_id="222") is False


# ==========================================
# analyze_items
# ==========================================
def test_analyze_items_finds_lowest_and_rank():
    items = [
        RawItem(item_id="1", seller_text="rival_a", price_text="$30.00", shipping_text="Free shipping"),
        RawItem(item_id="2", seller_text="kaworu2021", price_text="$25.00", shipping_text="Free shipping"),
        RawItem(item_id="3", seller_text="rival_b", price_text="$20.00", shipping_text="$5.00 shipping"),
    ]
    result = analyze_items(items, my_seller_id="kaworu2021", my_item_id="", jpy_rate=JPY_RATE)
    assert result.count == 3
    # rival_b の合計は $25.00 で自分と同額（安くはない）ため、
    # 自分より安いのは rival_a の $30.00 のみ = 0件 → 順位は1位
    assert result.my_rank == 1
    assert result.lowest_price == 25.00  # $20.00 + $5.00 shipping
    assert result.has_rival is True


def test_analyze_items_rank_by_price_not_dom_order_regression():
    """
    eBayの「価格の安い順」ソートは実際には厳密な昇順にならないことがあり、
    自分の出品がDOM上で先頭に出現しても、後方により安い競合が
    紛れ込むケースがある（実データで確認済み）。
    順位はDOM出現順ではなく、実際の価格比較で決まるべき。
    """
    items = [
        RawItem(item_id="1", seller_text="kaworu2021", price_text="$92.15", shipping_text="Free shipping"),
        RawItem(item_id="2", seller_text="rival_a", price_text="$71.32", shipping_text="Free shipping"),
        RawItem(item_id="3", seller_text="rival_b", price_text="$93.60", shipping_text="Free shipping"),
    ]
    result = analyze_items(items, my_seller_id="kaworu2021", my_item_id="", jpy_rate=JPY_RATE)
    # 自分より安いのは rival_a ($71.32) のみ → 2位
    assert result.my_rank == 2


def test_analyze_items_no_rivals():
    items = [RawItem(item_id="1", seller_text="kaworu2021", price_text="$25.00")]
    result = analyze_items(items, my_seller_id="kaworu2021", my_item_id="", jpy_rate=JPY_RATE)
    assert result.lowest_price == 0.0
    assert result.has_rival is False
    assert result.my_rank == 1


def test_analyze_items_skips_items_without_price():
    items = [
        RawItem(item_id="1", seller_text="rival_a", price_text="Best Offer"),
        RawItem(item_id="2", seller_text="rival_b", price_text="$10.00"),
    ]
    result = analyze_items(items, my_seller_id="kaworu2021", my_item_id="", jpy_rate=JPY_RATE)
    assert result.lowest_price == 10.00
    assert result.count == 2  # count はアイテム総数（価格不明も含む）


def test_analyze_items_skips_items_without_item_id():
    items = [RawItem(item_id="", seller_text="x", price_text="$10.00")]
    result = analyze_items(items, my_seller_id="kaworu2021", my_item_id="", jpy_rate=JPY_RATE)
    assert result.count == 0


# ==========================================
# decide_new_price
# ==========================================
def test_decide_new_price_undercuts_by_default_cent():
    d = decide_new_price(rival_price=29.99, cost_floor=20.00)
    assert d.new_price == 29.98
    assert d.floor_applied is False


def test_decide_new_price_applies_cost_floor():
    d = decide_new_price(rival_price=15.00, cost_floor=20.00)
    assert d.new_price == 20.00
    assert d.floor_applied is True
    assert d.undercut_price == 14.99


def test_decide_new_price_custom_undercut():
    d = decide_new_price(rival_price=100.00, cost_floor=0, undercut=0.5)
    assert d.new_price == 99.50


# ==========================================
# has_rival_price / product_identifier
# ==========================================
def test_has_rival_price_true_for_numeric():
    assert has_rival_price({"競合最安値(USD)": "29.99"}) is True


def test_has_rival_price_false_for_no_rival_text():
    assert has_rival_price({"競合最安値(USD)": "競合なし"}) is False


def test_has_rival_price_false_for_empty():
    assert has_rival_price({"競合最安値(USD)": ""}) is False
    assert has_rival_price({}) is False


def test_product_identifier_prefers_asin():
    assert product_identifier({"ASIN": "B000123", "JANコード": "4900000000000"}) == "B000123"


def test_product_identifier_falls_back_to_jan():
    assert product_identifier({"ASIN": "", "JANコード": "4900000000000"}) == "4900000000000"


# ==========================================
# 簡易ランナー（pytest がなくても実行できるように）
# ==========================================
if __name__ == "__main__":
    import sys as _sys

    tests = [(name, obj) for name, obj in list(globals().items())
             if name.startswith("test_") and callable(obj)]
    passed = failed = 0
    for name, fn in tests:
        try:
            fn()
            passed += 1
            print(f"  ✅ {name}")
        except AssertionError as e:
            failed += 1
            print(f"  ❌ {name}: {e}")
        except Exception as e:
            failed += 1
            print(f"  💥 {name}: {type(e).__name__}: {e}")

    print(f"\n{passed} passed, {failed} failed (of {len(tests)})")
    _sys.exit(1 if failed else 0)
