#!/bin/bash
# =============================================================
# VPS セットアップスクリプト (Ubuntu 22.04 / 24.04)
# 使い方: bash setup_vps.sh
# =============================================================
set -e

echo "===== [1/5] システムパッケージ更新 ====="
sudo apt update && sudo apt upgrade -y

echo "===== [2/5] Chrome インストール ====="
wget -q https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
sudo apt install -y ./google-chrome-stable_current_amd64.deb
rm google-chrome-stable_current_amd64.deb
google-chrome --version

echo "===== [3/5] Python & pip インストール ====="
sudo apt install -y python3 python3-pip python3-venv git

echo "===== [4/5] リポジトリ クローン & Python ライブラリ インストール ====="
# すでにクローン済みの場合はスキップ
if [ ! -d "ebay-kaworu" ]; then
  git clone https://github.com/shinnakaiya-sys/ebay-dropship.git ebay-kaworu
fi
cd ebay-kaworu
pip3 install -r requirements_vps.txt

echo "===== [5/5] 環境変数ファイル (.env) を作成 ====="
if [ ! -f ".env" ]; then
  cat > .env <<'ENV'
KEEPA_API_KEY=ここに入力
EBAY_TOKEN=ここに入力
EBAY_APP_ID=ここに入力
EBAY_CLIENT_SECRET=ここに入力
SHEET_ID=ここに入力
SLACK_WEBHOOK=ここに入力
LINE_TOKEN=ここに入力
HEADLESS=1
ENV
  echo "  ⚠️  .env を編集してください: nano .env"
else
  echo "  .env は既に存在します。HEADLESS=1 が含まれているか確認してください。"
fi

echo ""
echo "===== セットアップ完了 ====="
echo ""
echo "次のステップ:"
echo "  1. nano .env               # APIキーを入力"
echo "  2. nano credentials.json   # Google認証JSONを貼り付け"
echo "  3. python3 lowest_scrape.py --limit 5  # 動作テスト"
echo "  4. crontab -e              # cron設定（下記参照）"
echo ""
echo "cron設定例（6時間ごと）:"
echo "  0 1,7,13,19 * * * cd $(pwd) && HEADLESS=1 python3 scrape_and_adjust.py >> logs/scrape.log 2>&1"
