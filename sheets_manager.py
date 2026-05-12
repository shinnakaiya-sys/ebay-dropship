"""
Google Sheets で商品・価格・在庫を管理するモジュール

スプレッドシート構成:
  シート1: 商品マスタ     - 商品一覧・設定
  シート2: 価格履歴       - 毎日の価格ログ
  シート3: アラートログ   - 対応履歴

【初期セットアップ手順】
  1. Google Cloud Console でサービスアカウントを作成
  2. Google Sheets API を有効化
  3. credentials.json をダウンロード → プロジェクトフォルダに配置
  4. スプレッドシートをサービスアカウントのメールアドレスと共有
"""

import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime


# Google Sheets アクセス権限
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# シート名定義
SHEET_MASTER   = "商品マスタ"
SHEET_PENDING  = "出品待ちリスト"
SHEET_PRICE    = "価格履歴"
SHEET_ALERT    = "アラートログ"
SHEET_SUMMARY  = "サマリー"

# 商品マスタのカラム定義（A列から順番）
MASTER_COLS = [
    "JANコード",       # A  ← 主キー（EAN）
    "ASIN",           # B  ← Keepaで自動取得
    "eBay商品ID",     # C
    "商品名",          # D
    "仕入れ基準価格",  # E
    "eBay売値(USD)",   # F
    "ステータス",      # G  出品中 / 在庫切れ停止 / 無効
    "最終チェック日",  # H
    "登録日",          # I
    "メモ",            # J
    "下限価格(USD)",   # K  空欄=グローバル設定を使用
]

# 出品待ちリストのカラム定義
PENDING_COLS = [
    "JANコード",       # A  ← 主キー
    "ステータス",      # B  待機中 / 出品完了 / スキップ
    "登録日",          # C
    "メモ",            # D
]


class SheetsManager:
    def __init__(self, sheet_id: str, cred_path: str = "credentials.json"):
        creds = Credentials.from_service_account_file(cred_path, scopes=SCOPES)
        self.gc = gspread.authorize(creds)
        self.sheet = self.gc.open_by_key(sheet_id)
        self._init_sheets()

    # ──────────────────────────────────────────────────────
    # シート初期化（存在しない場合に作成）
    # ──────────────────────────────────────────────────────
    def _init_sheets(self):
        existing = [ws.title for ws in self.sheet.worksheets()]

        if SHEET_MASTER not in existing:
            ws = self.sheet.add_worksheet(SHEET_MASTER, rows=1000, cols=20)
            ws.append_row(MASTER_COLS)
            print(f"  📄 シート作成: {SHEET_MASTER}")
        else:
            # 既存シートのヘッダーをJANコード対応に更新（初回のみ）
            ws = self.sheet.worksheet(SHEET_MASTER)
            headers = ws.row_values(1)
            if headers and headers[0] == "ASIN":
                ws.update("A1:J1", [MASTER_COLS])
                print(f"  🔄 {SHEET_MASTER}: ヘッダーをJANコード対応に更新")

        if SHEET_PRICE not in existing:
            ws = self.sheet.add_worksheet(SHEET_PRICE, rows=10000, cols=10)
            ws.append_row(["日付", "ASIN", "プラットフォーム", "価格", "在庫状態"])
            print(f"  📄 シート作成: {SHEET_PRICE}")

        if SHEET_ALERT not in existing:
            ws = self.sheet.add_worksheet(SHEET_ALERT, rows=5000, cols=10)
            ws.append_row(["日時", "種別", "ASIN", "eBay商品ID", "商品名", "内容"])
            print(f"  📄 シート作成: {SHEET_ALERT}")

        if SHEET_PENDING not in existing:
            ws = self.sheet.add_worksheet(SHEET_PENDING, rows=1000, cols=10)
            ws.append_row(["JANコード", "ステータス", "登録日", "メモ"])
            print(f"  📄 シート作成: {SHEET_PENDING}")

        if SHEET_SUMMARY not in existing:
            ws = self.sheet.add_worksheet(SHEET_SUMMARY, rows=100, cols=10)
            ws.append_row(["実行日時", "管理商品数", "アクション件数", "在庫切れ", "価格更新", "再出品"])
            print(f"  📄 シート作成: {SHEET_SUMMARY}")

    # ──────────────────────────────────────────────────────
    # 商品マスタ: アクティブ商品を取得
    # ──────────────────────────────────────────────────────
    def get_active_products(self) -> list[dict]:
        """ステータスが「出品中」または「在庫切れ停止」の商品を返す"""
        ws = self.sheet.worksheet(SHEET_MASTER)
        records = ws.get_all_records()
        active = [
            r for r in records
            if r.get("ステータス") in ("出品中", "在庫切れ停止")
            and (r.get("ASIN") or r.get("JANコード"))
        ]
        return active

    # ──────────────────────────────────────────────────────
    # 商品マスタ: ステータス更新
    # ──────────────────────────────────────────────────────
    def update_last_checked(self, asin: str):
        """最終チェック日のみ更新（変動なし時に使用）"""
        ws = self.sheet.worksheet(SHEET_MASTER)
        cell = self._find_asin_cell(ws, asin)
        if cell:
            today = datetime.now().strftime("%Y-%m-%d %H:%M")
            ws.update_cell(cell.row, 8, today)

    def update_status(self, asin: str, status: str):
        ws = self.sheet.worksheet(SHEET_MASTER)
        cell = self._find_asin_cell(ws, asin)
        if cell:
            today = datetime.now().strftime("%Y-%m-%d %H:%M")
            ws.update_cell(cell.row, 7, status)   # G列: ステータス
            ws.update_cell(cell.row, 8, today)     # H列: 最終チェック日

    # ──────────────────────────────────────────────────────
    # 商品マスタ: 価格更新
    # ──────────────────────────────────────────────────────
    def update_price(self, asin: str, amazon_price: float, ebay_price: float):
        ws = self.sheet.worksheet(SHEET_MASTER)
        cell = self._find_asin_cell(ws, asin)
        if cell:
            today = datetime.now().strftime("%Y-%m-%d %H:%M")
            ws.update_cell(cell.row, 5, amazon_price)  # E列: 仕入れ基準価格
            ws.update_cell(cell.row, 6, ebay_price)    # F列: eBay売値
            ws.update_cell(cell.row, 8, today)          # H列: 最終チェック日

    # ──────────────────────────────────────────────────────
    # 価格履歴: ログ追記
    # ──────────────────────────────────────────────────────
    def log_price(self, asin: str, platform: str, price: float, in_stock: bool):
        ws = self.sheet.worksheet(SHEET_PRICE)
        today = datetime.now().strftime("%Y-%m-%d")
        ws.append_row([
            today,
            asin,
            platform,
            price,
            "在庫あり" if in_stock else "在庫なし",
        ])

    # ──────────────────────────────────────────────────────
    # アラートログ: 書き込み
    # ──────────────────────────────────────────────────────
    def write_alerts(self, alerts: list[dict]):
        if not alerts:
            return
        ws = self.sheet.worksheet(SHEET_ALERT)
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        rows = [
            [now, a["type"], a["asin"], a["ebay_id"], a["product"], a["message"]]
            for a in alerts
        ]
        ws.append_rows(rows)

    # ──────────────────────────────────────────────────────
    # サマリー: 実行結果を記録
    # ──────────────────────────────────────────────────────
    def write_summary(self, total: int, alerts: list[dict]):
        ws = self.sheet.worksheet(SHEET_SUMMARY)
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        out_of_stock = sum(1 for a in alerts if "在庫切れ" in a["type"])
        price_update  = sum(1 for a in alerts if "価格変動" in a["type"])
        relist        = sum(1 for a in alerts if "在庫復活" in a["type"])
        ws.append_row([now, total, len(alerts), out_of_stock, price_update, relist])
        self.write_alerts(alerts)

    # ──────────────────────────────────────────────────────
    # 出品待ちリスト: 取得
    # ──────────────────────────────────────────────────────
    def get_pending_products(self) -> list[dict]:
        """ステータスが「待機中」の商品を返す（JANコード対応）"""
        ws = self.sheet.worksheet(SHEET_PENDING)
        records = ws.get_all_records()
        return [r for r in records if r.get("ステータス") == "待機中" and r.get("JANコード")]

    # ──────────────────────────────────────────────────────
    # 出品待ちリスト: ステータス更新
    # ──────────────────────────────────────────────────────
    def update_pending_status(self, asin: str, status: str):
        ws = self.sheet.worksheet(SHEET_PENDING)
        cell = self._find_asin_cell(ws, asin)
        if cell:
            ws.update_cell(cell.row, 2, status)  # B列: ステータス

    # ──────────────────────────────────────────────────────
    # 出品待ちリスト: 商品を追加
    # ──────────────────────────────────────────────────────
    def add_pending(self, asin: str, memo: str = ""):
        ws = self.sheet.worksheet(SHEET_PENDING)
        today = datetime.now().strftime("%Y-%m-%d")
        ws.append_row([asin, "待機中", today, memo])
        print(f"  ➕ 出品待ちに追加: {asin}")

    # ──────────────────────────────────────────────────────
    # 新商品を追加
    # ──────────────────────────────────────────────────────
    def add_product(self, asin: str, ebay_id: str, name: str,
                    base_price: float, ebay_price: float, memo: str = "",
                    jan_code: str = ""):
        ws = self.sheet.worksheet(SHEET_MASTER)
        today = datetime.now().strftime("%Y-%m-%d")
        ws.append_row([
            jan_code, asin, ebay_id, name, base_price, ebay_price,
            "出品中", today, today, memo
        ])
        print(f"  ➕ 商品追加: {name[:40]}")

    # ──────────────────────────────────────────────────────
    # ヘルパー
    # ──────────────────────────────────────────────────────
    def _find_asin_cell(self, ws, asin: str):
        """B列（ASIN）またはA列（JANコード）から検索してセルを返す"""
        try:
            # B列（ASIN）で検索
            cell = ws.find(asin, in_column=2)
            if cell:
                return cell
        except Exception:
            pass
        try:
            # A列（JANコード）でも検索（後方互換）
            return ws.find(asin, in_column=1)
        except Exception:
            return None

    def _find_jan_cell(self, ws, jan_code: str):
        """A列（JANコード）からセルを返す"""
        try:
            return ws.find(jan_code, in_column=1)
        except Exception:
            return None

    def update_pending_status_by_jan(self, jan_code: str, status: str):
        """出品待ちリストのJANコード行のステータスを更新"""
        ws = self.sheet.worksheet(SHEET_PENDING)
        cell = self._find_jan_cell(ws, jan_code)
        if cell:
            ws.update_cell(cell.row, 2, status)