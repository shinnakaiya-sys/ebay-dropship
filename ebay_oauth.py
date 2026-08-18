"""
eBay ユーザーOAuthトークン共通ヘルパー

.env の EBAY_REFRESH_TOKEN 1本から、スコープ別にaccess_tokenを取得・キャッシュする。
1つのrefresh_tokenで、認可時に許可した複数スコープをリクエスト時に個別指定して使い分けられる
（例: sell.analytics.readonly と sell.marketing を同じrefresh_tokenから使い分け）。

未取得のスコープ、または一度も認可していないスコープを指定した場合は
ebay_oauth_authorize.py で再認可してrefresh_tokenを更新すること。
"""

import base64
import time

import requests

SCOPE_ANALYTICS = "https://api.ebay.com/oauth/api_scope/sell.analytics.readonly"
SCOPE_MARKETING = "https://api.ebay.com/oauth/api_scope/sell.marketing"

# スコープごとのaccess_tokenキャッシュ
_token_cache: dict[str, dict] = {}


def get_user_token(config: dict, scope: str) -> str:
    """refresh_token から指定スコープのユーザーaccess_tokenを取得・キャッシュする"""
    cached = _token_cache.get(scope)
    if cached and time.time() < cached["expires_at"] - 60:
        return cached["token"]

    app_id = config.get("EBAY_APP_ID", "")
    secret = config.get("EBAY_CLIENT_SECRET", "")
    refresh_token = config.get("EBAY_REFRESH_TOKEN", "")
    if not (app_id and secret and refresh_token):
        print("  ⚠️  EBAY_REFRESH_TOKEN / EBAY_APP_ID / EBAY_CLIENT_SECRET が未設定です（.env を確認してください）")
        return ""

    creds = base64.b64encode(f"{app_id}:{secret}".encode()).decode()
    resp = requests.post(
        "https://api.ebay.com/identity/v1/oauth2/token",
        headers={
            "Authorization": f"Basic {creds}",
            "Content-Type":  "application/x-www-form-urlencoded",
        },
        data={
            "grant_type":    "refresh_token",
            "refresh_token": refresh_token,
            "scope":         scope,
        },
        timeout=10,
    )
    if resp.status_code != 200:
        scope_name = scope.rsplit("/", 1)[-1]
        print(f"  ⚠️  ユーザートークン取得失敗({scope_name}): HTTP {resp.status_code} {resp.text[:300]}")
        print(f"     → refresh_tokenに{scope_name}スコープが含まれているか確認してください"
              "（ebay_oauth_authorize.py で再認可）")
        return ""

    data = resp.json()
    token = data.get("access_token", "")
    _token_cache[scope] = {"token": token, "expires_at": time.time() + data.get("expires_in", 7200)}
    return token
