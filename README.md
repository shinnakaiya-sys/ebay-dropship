# eBayドロップシッピング 毎日チェックシステム

Amazon.co.jp（Keepa）× eBay × Google Sheets で
価格・在庫を毎日自動チェックする無在庫ドロップシッピング管理システム

---

## システム概要

```
毎日 AM9:00 自動実行（GitHub Actions）
         ↓
Keepa API → Amazon.co.jp の価格・在庫を確認
         ↓
判定ロジック
  ├── 在庫切れ     → eBay出品を停止
  ├── 在庫復活     → eBay再出品（価格再計算）
  └── 価格変動5%↑↓ → eBay価格を自動更新
         ↓
Google Sheets に全ログを記録
         ↓
Slack / LINE に変動を通知
```

---

## ファイル構成

```
├── main.py              # メイン実行（毎日チェック）
├── ebay_lister.py       # eBay自動出品
├── config.py            # 設定（環境変数読み込み）
├── keepa_checker.py     # Keepa価格・在庫チェック
├── ebay_checker.py      # eBay出品状態確認・更新
├── sheets_manager.py    # Google Sheets 読み書き
├── notifier.py          # Slack/LINE通知
├── .env.example         # 環境変数テンプレート
└── .github/
    └── workflows/
        └── daily_check.yml  # GitHub Actions設定
```

---

## セットアップ手順

### Step 1: ライブラリインストール
```bash
pip install keepa requests gspread google-auth pandas python-dotenv
```

### Step 2: APIキーの取得

| サービス | 取得先 | 備考 |
|---------|--------|------|
| Keepa API | https://keepa.com/#!api | 月$19〜 |
| eBay Token | https://developer.ebay.com | 無料 |
| Google Sheets | GCP → サービスアカウント作成 | 無料 |
| Slack Webhook | Slack App設定 | 任意 |
| LINE Notify | https://notify-bot.line.me | 任意 |

### Step 3: .env ファイルを作成
```bash
cp .env.example .env
# .envを編集してAPIキーを設定
```

### Step 4: Google Sheets の設定
1. 新しいスプレッドシートを作成
2. URLから Sheet ID をコピー（`/d/XXXXXXX/` の部分）
3. GCPでサービスアカウントを作成 → `credentials.json` をダウンロード
4. スプレッドシートをサービスアカウントのメールアドレスと「編集者」で共有

### Step 5: 動作確認
```bash
python main.py
```

初回実行時にシートが自動作成されます。

---

## Google Sheets の構成

### 📥 出品待ちリスト（手動で追加）
| 列 | 項目 | 例 |
|----|------|-----|
| A | ASIN | B07XXXXX |
| B | ステータス | 待機中 / 出品完了 / スキップ |
| C | 登録日 | 2026-05-07 |
| D | メモ | リサーチ済み・人気商品 |

### 📋 商品マスタ（自動登録）
| 列 | 項目 | 例 |
|----|------|-----|
| A | ASIN | B07XXXXX |
| B | eBay商品ID | 1234567890 |
| C | 商品名 | ○○○ワイヤレスイヤホン |
| D | 仕入れ基準価格 | 3800 |
| E | eBay売値(USD) | 45.99 |
| F | ステータス | 出品中 |
| G | 最終チェック日 | 2026-05-07 09:00 |
| H | 登録日 | 2026-04-01 |
| I | メモ | 人気商品 |

### 📈 価格履歴（自動記録）
毎日のAmazon・eBay価格を自動ログ

### 🔔 アラートログ（自動記録）
在庫切れ・価格変動の対応履歴

### 📊 サマリー（自動記録）
実行日時・アクション件数の集計

---

## eBay自動出品の使い方

```bash
# 出品待ちリストを一括出品
python ebay_lister.py

# 1件だけテスト出品（ASIN指定）
python ebay_lister.py --asin B07XXXXXXX

# 出品データの確認のみ（実際には出品しない）
python ebay_lister.py --dry-run
```

### 出品フロー
```
Sheetsの「出品待ちリスト」にASINを追加（ステータス: 待機中）
        ↓
python ebay_lister.py を実行
        ↓
Keepaで最新価格・画像・商品詳細を自動取得
        ↓
タイトル・説明文・価格を自動生成してeBayに出品
        ↓
取得したeBay商品IDを「商品マスタ」に自動登録
        ↓
以降は main.py（毎日チェック）が価格・在庫を監視
```

---



1. GitHubリポジトリを作成してコードをプッシュ
2. Settings → Secrets and variables → Actions で以下を登録：
   - `KEEPA_API_KEY`
   - `EBAY_TOKEN`
   - `SHEET_ID`
   - `GSHEET_CREDENTIALS`（credentials.jsonの中身をそのままペースト）
   - `SLACK_WEBHOOK`（任意）
   - `LINE_TOKEN`（任意）
3. 毎日 AM9:00（日本時間）に自動実行されます

---

## カスタマイズ

`config.py` の値を変更して調整できます：

```python
"PRICE_CHANGE_THRESHOLD": 0.05,  # 5%以上変動で更新
"TARGET_MARGIN":          0.20,  # 目標利益率 20%
"EBAY_FEE_RATE":          0.1325,# eBay手数料 13.25%
"SHIPPING_USD":           15.0,  # 国際送料（ドル）
"JPY_TO_USD":             155.0, # 為替レート
```
