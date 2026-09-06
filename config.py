"""
設定ファイル
.env に APIキーを記載してください
"""

import os
import requests
from dotenv import load_dotenv

# 複数アカウント対応: EBAY_ENV_PATH が指定されていれば、そのアカウント専用
# .env を優先して読み込む。未指定時は従来通りカレントディレクトリの .env を読む。
#
# どちらの場合も override=True が必須: override=False（デフォルト）だと、
# シェルに既にEBAY_TOKEN等が環境変数としてexportされていた場合
# （例: 同じターミナルタブで直前にebay_dvz/ebay-kozukiディレクトリの
# .envをsource/exportして作業していた等）、.envの値ではなく
# その残留値が黙って優先されてしまう。実際にこれが原因で、
# kaworu2021向けのはずの出品が別アカウント(dbz_park)のトークンで
# 実行され、別アカウント名義で出品されてしまう事故が発生した
# (2026-09-06、--asin手動実行の2件で発生。通常のバッチ実行は
# 別プロセス起動のため影響なし)。
_account_env_path = os.getenv("EBAY_ENV_PATH")
if _account_env_path:
    load_dotenv(os.path.expanduser(_account_env_path), override=True)
else:
    load_dotenv(override=True)


def _fetch_jpy_rate(fallback: float = 155.0) -> float:
    """Frankfurter API（無料・認証不要）から USD/JPY レートを取得する"""
    try:
        resp = requests.get(
            "https://api.frankfurter.app/latest",
            params={"from": "USD", "to": "JPY"},
            timeout=3,
        )
        rate = resp.json()["rates"]["JPY"]
        print(f"💱 USD/JPY 為替レート取得: {rate}")
        return float(rate)
    except Exception as e:
        print(f"⚠️  為替レート取得失敗（フォールバック {fallback} を使用）: {e}")
        return fallback

CONFIG = {
    # ── API認証情報 ──────────────────────────────
    "KEEPA_API_KEY":      os.getenv("KEEPA_API_KEY"),
    "EBAY_TOKEN":         os.getenv("EBAY_TOKEN"),
    "EBAY_OAUTH_TOKEN":   os.getenv("EBAY_OAUTH_TOKEN"),   # Marketing API用（sell.marketing スコープ必須）
    # EBAY_APP_ID / EBAY_CLIENT_ID はどちらの表記でも受け付ける(アカウントごとに
    # .env内の呼称が異なる場合があるため。kaworu2021のEBAY_APP_ID運用は変更なし)
    "EBAY_APP_ID":        os.getenv("EBAY_APP_ID") or os.getenv("EBAY_CLIENT_ID"),  # Browse API用 Client ID
    "EBAY_CLIENT_SECRET": os.getenv("EBAY_CLIENT_SECRET"), # Browse API用 Cert ID
    "EBAY_REFRESH_TOKEN": os.getenv("EBAY_REFRESH_TOKEN"), # ユーザートークン更新用（sell.analytics.readonly等）
    "EBAY_RUNAME":        os.getenv("EBAY_RUNAME"),        # OAuth認可コードフロー用のRuName
    # EBAY_SELLER_ID未指定時はEBAY_ACCOUNT_NAMEで代替し、それも無ければ従来通りkaworu2021にフォールバック
    "EBAY_SELLER_ID":     os.getenv("EBAY_SELLER_ID") or os.getenv("EBAY_ACCOUNT_NAME", "kaworu2021"),
    "ANTHROPIC_API_KEY":  os.getenv("ANTHROPIC_API_KEY"),
    "SLACK_WEBHOOK":      os.getenv("SLACK_WEBHOOK"),    # 任意
    "LINE_TOKEN":         os.getenv("LINE_TOKEN"),        # 任意

    # ── Google Sheets ────────────────────────────
    # スプレッドシートのURLの /d/〇〇〇/ の部分
    "SHEET_ID":         os.getenv("SHEET_ID") or os.getenv("GOOGLE_SHEET_ID"),
    # サービスアカウントのJSONキーファイルパス
    "GSHEET_CRED_PATH": os.getenv("GSHEET_CRED_PATH", "credentials.json"),

    # ── 価格・通貨設定 ───────────────────────────
    "JPY_TO_USD":       _fetch_jpy_rate(fallback=155.0),  # 起動時に自動取得
    "EBAY_FEE_RATE":    0.15,     # eBay手数料 15%
    "TARIFF_RATE":      0.15,     # 関税 10%
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