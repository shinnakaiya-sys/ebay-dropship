"""
sheets_manager_v2.py
=====================
sheets_manager.py を継承し、scrape_and_adjust_v2.py 用に
Google Sheets API 呼び出し回数を減らすメソッドを追加したもの。

既存の sheets_manager.py / SheetsManager は一切変更しない。

主な改善:
  * ワークシートオブジェクトをキャッシュ（毎回 self.sheet.worksheet() しない）
  * ASIN/JANコード → 行番号のインデックスを1回の get_all_values() で構築
    （旧実装は商品1件ごとに ws.find() = API 1リクエストを消費していた）
  * J・K・N列（競合最安値・出品数・順位）や E・F・H列（価格）の更新を
    それぞれ1回の batch_update にまとめる（旧実装は update_cell を複数回）
"""

from __future__ import annotations

from datetime import datetime

from sheets_manager import (
    SheetsManager,
    SHEET_MASTER,
    MASTER_COLS,
    _JST,
    _col_letter,
)


class SheetsManagerV2(SheetsManager):
    def __init__(self, sheet_id: str, cred_path: str = "credentials.json"):
        self._ws_cache: dict[str, object] = {}
        self._row_index_cache: dict[str, dict[str, int]] = {}
        super().__init__(sheet_id, cred_path)

    # ──────────────────────────────────────────────────────
    # ワークシートキャッシュ
    # ──────────────────────────────────────────────────────
    def _get_worksheet(self, title: str, retries: int = 3, delay: int = 30):
        if title in self._ws_cache:
            return self._ws_cache[title]
        ws = super()._get_worksheet(title, retries=retries, delay=delay)
        self._ws_cache[title] = ws
        return ws

    def invalidate_cache(self, title: str | None = None):
        """行番号が変わる操作（行の追加・削除等）の後に呼ぶ"""
        if title:
            self._row_index_cache.pop(title, None)
        else:
            self._row_index_cache.clear()

    # ──────────────────────────────────────────────────────
    # ASIN/JANコード → 行番号 インデックス
    # ──────────────────────────────────────────────────────
    def build_row_index(self, force: bool = False) -> dict[str, int]:
        """
        商品マスタ全体を1回だけ読み込み、ASIN/JANコード → 行番号（1始まり）
        の辞書を作る。以後の _find_asin_cell 相当の検索をAPI呼び出しなしで行える。
        """
        if not force and SHEET_MASTER in self._row_index_cache:
            return self._row_index_cache[SHEET_MASTER]

        ws = self._get_worksheet(SHEET_MASTER)
        rows = ws.get_all_values()
        index: dict[str, int] = {}
        for i, row in enumerate(rows[1:], start=2):  # 1行目はヘッダー
            jan = row[0].strip() if len(row) > 0 else ""
            asin = row[1].strip() if len(row) > 1 else ""
            # ASIN優先。JANコードは後方互換で、ASINが無い場合のみ登録
            if asin:
                index[asin] = i
            if jan and jan not in index:
                index[jan] = i
        self._row_index_cache[SHEET_MASTER] = index
        return index

    def find_row(self, identifier: str) -> int | None:
        return self.build_row_index().get(str(identifier).strip())

    # ──────────────────────────────────────────────────────
    # 競合最安値・出品数・自分の順位（J・K・N列）を1リクエストで更新
    # ──────────────────────────────────────────────────────
    def apply_rival_result(self, identifier: str, lowest_price: float,
                           count: int, my_rank: int | None) -> bool:
        row = self.find_row(identifier)
        if row is None:
            return False
        ws = self._get_worksheet(SHEET_MASTER)
        j_val = lowest_price if lowest_price > 0 else "競合なし"
        ws.batch_update([
            {"range": f"J{row}", "values": [[j_val]]},
            {"range": f"K{row}", "values": [[count]]},
            {"range": f"N{row}", "values": [[my_rank if my_rank is not None else ""]]},
        ])
        return True

    # ──────────────────────────────────────────────────────
    # 仕入れ基準価格・eBay売値・最終チェック日（E・F・H列）を1リクエストで更新
    # ──────────────────────────────────────────────────────
    def apply_price_update(self, identifier: str, amazon_price: float,
                           ebay_price: float) -> bool:
        row = self.find_row(identifier)
        if row is None:
            return False
        ws = self._get_worksheet(SHEET_MASTER)
        today = datetime.now(_JST).strftime("%Y-%m-%d %H:%M")
        ws.batch_update([
            {"range": f"E{row}", "values": [[amazon_price]]},
            {"range": f"F{row}", "values": [[ebay_price]]},
            {"range": f"H{row}", "values": [[today]]},
        ])
        return True

    # ──────────────────────────────────────────────────────
    # 設定シート: 1回読み込んでCONFIGに適用するヘルパー
    # ──────────────────────────────────────────────────────
    def apply_settings_to(self, config: dict, keys: list[str]) -> dict:
        """get_settings() を1回だけ呼び、指定キーだけ config に上書きする"""
        settings = self.get_settings()
        for key in keys:
            val = settings.get(key)
            if val:
                config[key] = val
        return config
