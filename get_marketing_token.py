"""
eBay Marketing API用 OAuth2トークン取得ツール

【実行方法】
  python get_marketing_token.py

【事前準備】
  eBay Developer App の "Auth Accepted URL" (RuName) に
  https://localhost:8080 を追加する必要があります。

  ※ developer.ebay.com にアクセスできない場合:
     https://developer.ebay.com (HTTPS) でアクセスしてください。
     または、アプリ設定画面の直接URL: https://developer.ebay.com/my/auth

【取得後】
  .env に以下を追加:
    EBAY_OAUTH_TOKEN=<表示されたAccess Token>
  GitHub Secrets にも EBAY_OAUTH_TOKEN を追加してください。
"""

import base64
import json
import os
import threading
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests
from dotenv import load_dotenv

load_dotenv()

APP_ID        = os.getenv("EBAY_APP_ID", "")
CLIENT_SECRET = os.getenv("EBAY_CLIENT_SECRET", "")
RUNAME        = os.getenv("EBAY_RUNAME", "")  # Developer App の RuName

REDIRECT_URI  = "https://localhost:8080"
TOKEN_URL     = "https://api.ebay.com/identity/v1/oauth2/token"
AUTH_BASE_URL = "https://auth.ebay.com/oauth2/authorize"

SCOPES = " ".join([
    "https://api.ebay.com/oauth/api_scope",
    "https://api.ebay.com/oauth/api_scope/sell.marketing",
    "https://api.ebay.com/oauth/api_scope/sell.inventory",
    "https://api.ebay.com/oauth/api_scope/sell.account",
])

# ブラウザから受け取ったcodeを保持する
_auth_code = {"value": None}


class _OAuthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        code = params.get("code", [None])[0]
        if code:
            _auth_code["value"] = code
            self.send_response(200)
            self.send_header("Content-type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(
                "<html><body><h2>認証完了 ✅</h2>"
                "<p>このウィンドウを閉じてターミナルに戻ってください。</p>"
                "</body></html>".encode()
            )
        else:
            error = params.get("error_description", ["不明なエラー"])[0]
            self.send_response(400)
            self.send_header("Content-type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(
                f"<html><body><h2>エラー ❌</h2><p>{error}</p></body></html>".encode()
            )

    def log_message(self, *_):
        pass  # ログ出力を抑制


def _exchange_code(code: str) -> dict:
    credentials = base64.b64encode(f"{APP_ID}:{CLIENT_SECRET}".encode()).decode()
    resp = requests.post(
        TOKEN_URL,
        headers={
            "Content-Type":  "application/x-www-form-urlencoded",
            "Authorization": f"Basic {credentials}",
        },
        data={
            "grant_type":   "authorization_code",
            "code":         code,
            "redirect_uri": RUNAME or REDIRECT_URI,
        },
        timeout=15,
    )
    return resp.json()


def main():
    if not APP_ID or not CLIENT_SECRET:
        print("❌ EBAY_APP_ID または EBAY_CLIENT_SECRET が .env に設定されていません")
        return

    # RuName の確認
    ru_name = RUNAME
    if not ru_name:
        print("\n⚠️  EBAY_RUNAME が .env に設定されていません。")
        print("   eBay Developer App の RuName を確認して入力してください。")
        print("   （Developer App → Keys → OAuth redirect URL の名前）")
        ru_name = input("\n   RuName を入力: ").strip()
        if not ru_name:
            print("❌ RuName が入力されませんでした")
            return

    # 認可URL を構築
    params = {
        "client_id":     APP_ID,
        "response_type": "code",
        "redirect_uri":  ru_name,
        "scope":         SCOPES,
        "prompt":        "login",
    }
    auth_url = AUTH_BASE_URL + "?" + urllib.parse.urlencode(params)

    print("\n" + "=" * 60)
    print("eBay Marketing API OAuth2 トークン取得")
    print("=" * 60)
    print("\n以下のURLをブラウザで開いてeBayアカウントでログインしてください:\n")
    print(auth_url)
    print()

    # ブラウザを自動で開く（失敗しても続行）
    try:
        webbrowser.open(auth_url)
        print("（ブラウザを自動で開きました）")
    except Exception:
        pass

    # リダイレクト先からcodeを受け取る方法を選択
    print("\n【認可コードの取得方法を選択してください】")
    print("  1. 自動取得（localhost:8080 で待機）")
    print("  2. 手動入力（リダイレクト後のURLからcodeを貼り付け）")
    choice = input("\n選択 [1/2]: ").strip()

    code = None

    if choice == "1":
        # ローカルサーバーで待機
        print("\n⏳ localhost:8080 でeBayからのリダイレクトを待機中...")
        print("   （ブラウザでログイン・認可後、自動で取得されます）\n")
        server = HTTPServer(("localhost", 8080), _OAuthHandler)
        server.timeout = 120  # 2分でタイムアウト

        def _serve():
            while _auth_code["value"] is None:
                server.handle_request()

        t = threading.Thread(target=_serve, daemon=True)
        t.start()
        t.join(timeout=125)
        code = _auth_code["value"]

        if not code:
            print("❌ タイムアウト（2分以内に認可されませんでした）")
            return
    else:
        # 手動入力
        print("\nブラウザで認可後、リダイレクトされたURLを丸ごと貼り付けてください:")
        redirected_url = input("URL: ").strip()
        parsed = urllib.parse.urlparse(redirected_url)
        params_parsed = urllib.parse.parse_qs(parsed.query)
        code = params_parsed.get("code", [None])[0]
        if not code:
            # code=xxxxx の形式でも受け付ける
            if redirected_url.startswith("code=") or "code=" in redirected_url:
                code = urllib.parse.parse_qs(redirected_url).get("code", [None])[0]
        if not code:
            print("❌ URLからcodeを取得できませんでした")
            return

    print(f"\n✅ 認可コード取得: {code[:20]}...")
    print("⏳ アクセストークンを取得中...")

    token_data = _exchange_code(code)

    if "access_token" in token_data:
        access_token  = token_data["access_token"]
        refresh_token = token_data.get("refresh_token", "")
        expires_in    = token_data.get("expires_in", 0)

        print("\n" + "=" * 60)
        print("✅ トークン取得成功！")
        print("=" * 60)
        print(f"\nAccess Token (有効期限: {expires_in // 3600}時間):")
        print(f"  {access_token}\n")
        if refresh_token:
            print(f"Refresh Token（長期保存用）:")
            print(f"  {refresh_token}\n")

        print("【.env に以下を追加してください】")
        print(f'  EBAY_OAUTH_TOKEN={access_token}')
        if refresh_token:
            print(f'  EBAY_REFRESH_TOKEN={refresh_token}')

        # .env に自動書き込みするか確認
        write = input("\n.env に自動書き込みしますか？ [y/N]: ").strip().lower()
        if write == "y":
            env_path = os.path.join(os.path.dirname(__file__), ".env")
            with open(env_path, "r") as f:
                content = f.read()

            def _upsert(content, key, value):
                if f"{key}=" in content:
                    lines = content.splitlines()
                    lines = [f"{key}={value}" if l.startswith(f"{key}=") else l for l in lines]
                    return "\n".join(lines) + "\n"
                return content + f"\n{key}={value}\n"

            content = _upsert(content, "EBAY_OAUTH_TOKEN", access_token)
            if refresh_token:
                content = _upsert(content, "EBAY_REFRESH_TOKEN", refresh_token)

            with open(env_path, "w") as f:
                f.write(content)
            print("✅ .env に書き込みました")
            print("\nGitHub Secrets にも EBAY_OAUTH_TOKEN を追加してください。")
    else:
        print(f"\n❌ トークン取得失敗:")
        print(json.dumps(token_data, indent=2, ensure_ascii=False))
        if token_data.get("error") == "invalid_grant":
            print("\n→ 認可コードの有効期限切れ（数分以内に使用する必要があります）")
            print("  最初からやり直してください。")
        elif token_data.get("error") == "invalid_client":
            print("\n→ APP_ID または CLIENT_SECRET が正しくありません")


if __name__ == "__main__":
    main()
