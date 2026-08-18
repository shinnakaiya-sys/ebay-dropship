"""
eBay ユーザートークン(refresh_token)再発行スクリプト（初回/スコープ追加時のみ実行）

デフォルトで sell.analytics.readonly（トラフィックレポート用）と
sell.marketing（Promoted Listings用）の両方を含むrefresh_tokenを発行する。
既存の EBAY_REFRESH_TOKEN を上書きするので、実行後は .env を手動で更新すること。

【使い方】
  1. 認可URLを表示:
       python ebay_oauth_authorize.py
     表示されたURLをブラウザで開き、eBayアカウントでログイン・同意する。
     同意後にリダイレクトされたURLの ?code=xxxx... の値をコピーする
     （URLエンコードされたままでよい）。

  2. codeをaccess_token/refresh_tokenに交換:
       python ebay_oauth_authorize.py --code "<コピーしたcode>"
     出力された refresh_token を .env の EBAY_REFRESH_TOKEN に貼り付ける。

【スコープを追加/変更したい場合】
  SCOPES 変数を編集する（スペース区切りで複数指定可）。
"""

import argparse
import base64
import urllib.parse

import requests

from config import CONFIG
from ebay_oauth import SCOPE_ANALYTICS, SCOPE_MARKETING

SCOPES = [
    SCOPE_ANALYTICS,
    SCOPE_MARKETING,  # Promoted Listings（bulk_create_ads_by_listing_id）用
]


def build_authorize_url() -> str:
    params = {
        "client_id":     CONFIG["EBAY_APP_ID"],
        "redirect_uri":  CONFIG["EBAY_RUNAME"],
        "response_type": "code",
        "scope":         " ".join(SCOPES),
    }
    return "https://auth.ebay.com/oauth2/authorize?" + urllib.parse.urlencode(params, quote_via=urllib.parse.quote)


def exchange_code(code: str) -> dict:
    creds = base64.b64encode(f"{CONFIG['EBAY_APP_ID']}:{CONFIG['EBAY_CLIENT_SECRET']}".encode()).decode()
    resp = requests.post(
        "https://api.ebay.com/identity/v1/oauth2/token",
        headers={
            "Authorization": f"Basic {creds}",
            "Content-Type":  "application/x-www-form-urlencoded",
        },
        data={
            "grant_type":   "authorization_code",
            "code":         urllib.parse.unquote(code),
            "redirect_uri": CONFIG["EBAY_RUNAME"],
        },
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--code", default="", help="認可後にリダイレクトURLから取得したcode")
    args = parser.parse_args()

    if not args.code:
        print("以下のURLをブラウザで開いてeBayアカウントで同意してください:\n")
        print(build_authorize_url())
        print("\n同意後、リダイレクト先URLの ?code=... の値を")
        print('  python ebay_oauth_authorize.py --code "<code>"')
        print("で渡してください。")
        return

    data = exchange_code(args.code)
    print("\n✅ 取得成功。以下を .env の該当行に貼り付けてください:\n")
    print(f"EBAY_REFRESH_TOKEN={data.get('refresh_token', '')}")
    print(f"\n(access_tokenの有効期限: {data.get('expires_in', '?')}秒 / refresh_tokenは通常18ヶ月有効)")


if __name__ == "__main__":
    main()
