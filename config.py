"""
設定ファイル
.env に APIキーを記載してください
"""

import os
from dotenv import load_dotenv

load_dotenv()

CONFIG = {
    # ── API認証情報 ──────────────────────────────
    "KEEPA_API_KEY":      os.getenv("KEEPA_API_KEY"),
    "EBAY_TOKEN":         os.getenv("EBAY_TOKEN"),
    "EBAY_APP_ID":        os.getenv("EBAY_APP_ID"),        # Browse API用 Client ID
    "EBAY_CLIENT_SECRET": os.getenv("EBAY_CLIENT_SECRET"), # Browse API用 Cert ID
    "ANTHROPIC_API_KEY":  os.getenv("ANTHROPIC_API_KEY"),
    "SLACK_WEBHOOK":      os.getenv("SLACK_WEBHOOK"),    # 任意
    "LINE_TOKEN":         os.getenv("LINE_TOKEN"),        # 任意

    # ── Google Sheets ────────────────────────────
    # スプレッドシートのURLの /d/〇〇〇/ の部分
    "SHEET_ID":         os.getenv("SHEET_ID"),
    # サービスアカウントのJSONキーファイルパス
    "GSHEET_CRED_PATH": os.getenv("GSHEET_CRED_PATH", "credentials.json"),

    # ── 価格・通貨設定 ───────────────────────────
    "JPY_TO_USD":       155.0,    # 為替レート（定期的に更新推奨）
    "EBAY_FEE_RATE":    0.17,     # eBay手数料 17%
    "TARIFF_RATE":      0.15,     # 関税 15%
    "TARGET_MARGIN":    0.01,     # 目標利益率 1%

    # ── 販売価格下限 ─────────────────────────────
    # 送料計算ミス時のリスクヘッジ（USD）。0で無効
    "MIN_SELL_PRICE_USD":    20.0,

    # ── チェック閾値 ─────────────────────────────
    # この割合以上価格が変動したらeBayを更新する（5% = 0.05）
    "PRICE_CHANGE_THRESHOLD": 0.01,

    # ── Keepa設定 ────────────────────────────────
    "KEEPA_DOMAIN":     "JP",        # 5 = Amazon.co.jp
}