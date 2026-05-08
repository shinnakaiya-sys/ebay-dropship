"""
Slack / LINE への通知モジュール
"""

import requests
import json


class Notifier:
    def __init__(self, config: dict):
        self.slack_webhook = config.get("SLACK_WEBHOOK")
        self.line_token    = config.get("LINE_TOKEN")

    def send(self, alerts: list[dict]):
        """アラートリストを通知"""
        if not alerts:
            return

        message = self._format_message(alerts)

        if self.slack_webhook:
            self._send_slack(message)

        if self.line_token:
            self._send_line(message)

    def _format_message(self, alerts: list[dict]) -> str:
        lines = [f"📊 eBayドロップシッピング 毎日チェック結果（{len(alerts)}件のアクション）\n"]
        for a in alerts:
            lines.append(f"{a['type']} {a['product']}")
            lines.append(f"  → {a['message']}\n")
        return "\n".join(lines)

    def _send_slack(self, message: str):
        try:
            resp = requests.post(
                self.slack_webhook,
                json={"text": message},
                timeout=10,
            )
            if resp.status_code == 200:
                print("  📨 Slack通知送信")
            else:
                print(f"  ⚠️  Slack通知失敗: {resp.status_code}")
        except Exception as e:
            print(f"  ⚠️  Slack通知エラー: {e}")

    def _send_line(self, message: str):
        try:
            resp = requests.post(
                "https://notify-api.line.me/api/notify",
                headers={"Authorization": f"Bearer {self.line_token}"},
                data={"message": message},
                timeout=10,
            )
            if resp.status_code == 200:
                print("  📨 LINE通知送信")
            else:
                print(f"  ⚠️  LINE通知失敗: {resp.status_code}")
        except Exception as e:
            print(f"  ⚠️  LINE通知エラー: {e}")
