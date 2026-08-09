"""
pr_video_generator.py
======================
出品画像からPR動画（パン・ズーム付きスライドショー）を自動生成し、
eBay Media API経由で出品ページにアップロードする。

処理の流れ:
  1. GetItem（Trading API）で出品画像URLを取得
  2. 画像をダウンロードし、ffmpegでKen Burnsエフェクト＋クロスフェードの動画を生成
  3. Media API（createVideo → uploadVideo）で動画をアップロード
  4. ReviseItem（Trading API）で出品ページに動画を紐付け

使い方:
  # Item ID直接指定（複数可）
  python3 pr_video_generator.py 318320977459 318287926133

  # CSVファイルから一括処理（"Item ID"列を使用）
  python3 pr_video_generator.py --csv improve_2_clicks_no_conversion.csv --limit 5

  # 動画生成のみ（アップロード・紐付けはしない）
  python3 pr_video_generator.py 318320977459 --dry-run

オプション:
  --csv <file>     Item ID列を含むCSVから対象を読み込む
  --limit N        CSV使用時、先頭N件のみ処理
  --dry-run        動画を生成してローカル保存するだけ（eBayへの反映なし）
  --force          既にVideoDetailsが設定済みの出品も上書きする
  --workdir <dir>  作業ディレクトリ（デフォルト: ./pr_video_work）
"""

import os
import sys
import csv
import time
import base64
import subprocess
import requests
from dotenv import dotenv_values
import xml.etree.ElementTree as ET

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV = dotenv_values(os.path.join(BASE_DIR, ".env"))

TRADING_API_URL = "https://api.ebay.com/ws/api.dll"
MEDIA_API_BASE  = "https://apim.ebay.com/commerce/media/v1_beta"
NS = {"e": "urn:ebay:apis:eBLBaseComponents"}

# ── 動画生成設定 ──────────────────────────────
VIDEO_SIZE      = 720   # 出力動画の一辺(px)
CLIP_DURATION   = 3.0   # 1枚あたりの表示秒数
CROSSFADE_DUR   = 0.5   # クロスフェード秒数
FPS             = 25
MAX_IMAGES      = 6     # 動画に使う画像の最大枚数（多すぎると尺が長くなるため）


# ==========================================
# Trading API（IAFトークン）
# ==========================================
def _trading_headers(call_name: str) -> dict:
    return {
        "X-EBAY-API-SITEID": "0",
        "X-EBAY-API-COMPATIBILITY-LEVEL": "967",
        "X-EBAY-API-IAF-TOKEN": ENV.get("EBAY_TOKEN", ""),
        "Content-Type": "text/xml; charset=utf-8",
        "X-EBAY-API-CALL-NAME": call_name,
    }


def get_item_images(item_id: str) -> dict:
    """GetItemで画像URL・タイトル・既存VideoIDを取得する。"""
    xml_body = f"""<?xml version="1.0" encoding="utf-8"?>
<GetItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">
  <RequesterCredentials><eBayAuthToken>{ENV.get('EBAY_TOKEN', '')}</eBayAuthToken></RequesterCredentials>
  <ItemID>{item_id}</ItemID>
  <DetailLevel>ReturnAll</DetailLevel>
</GetItemRequest>"""
    resp = requests.post(TRADING_API_URL, headers=_trading_headers("GetItem"),
                          data=xml_body.encode("utf-8"), timeout=15)
    # resp.text はcharset未指定時にISO-8859-1と誤判定されるため、必ず resp.content を渡すこと
    root = ET.fromstring(resp.content)
    item = root.find("e:Item", NS)
    if item is None:
        return {"ok": False}

    title = item.findtext("e:Title", namespaces=NS) or ""
    status = item.findtext("e:SellingStatus/e:ListingStatus", namespaces=NS)
    pics = [p.text for p in item.findall(".//e:PictureDetails/e:PictureURL", NS) if p.text]
    video_details = item.find("e:VideoDetails", NS)
    existing_video_id = video_details.findtext("e:VideoID", namespaces=NS) if video_details is not None else None

    return {
        "ok": True,
        "title": title,
        "status": status,
        "pictures": pics,
        "existing_video_id": existing_video_id,
    }


def attach_video_to_item(item_id: str, video_id: str) -> tuple[bool, str]:
    """ReviseItemで出品ページに動画を紐付ける。"""
    xml_body = f"""<?xml version="1.0" encoding="utf-8"?>
<ReviseItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">
  <RequesterCredentials><eBayAuthToken>{ENV.get('EBAY_TOKEN', '')}</eBayAuthToken></RequesterCredentials>
  <Item>
    <ItemID>{item_id}</ItemID>
    <VideoDetails>
      <VideoID>{video_id}</VideoID>
    </VideoDetails>
  </Item>
</ReviseItemRequest>"""
    resp = requests.post(TRADING_API_URL, headers=_trading_headers("ReviseItem"),
                          data=xml_body.encode("utf-8"), timeout=20)
    root = ET.fromstring(resp.content)
    ack = root.findtext("e:Ack", namespaces=NS)
    errors = []
    for e in root.findall("e:Errors", NS):
        sev = e.findtext("e:SeverityCode", namespaces=NS)
        msg = e.findtext("e:ShortMessage", namespaces=NS)
        if sev == "Error":
            errors.append(msg)
    ok = ack in ("Success", "Warning") and not errors
    return ok, "; ".join(errors) if errors else ack


# ==========================================
# OAuth（Media API用）
# ==========================================
def get_fresh_oauth_token() -> str:
    """リフレッシュトークンからsell.inventoryスコープ付きアクセストークンを取得する。"""
    client_id = ENV.get("EBAY_APP_ID", "")
    client_secret = ENV.get("EBAY_CLIENT_SECRET", "")
    refresh_token = ENV.get("EBAY_REFRESH_TOKEN", "")
    auth = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()

    resp = requests.post(
        "https://api.ebay.com/identity/v1/oauth2/token",
        headers={"Authorization": f"Basic {auth}", "Content-Type": "application/x-www-form-urlencoded"},
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "scope": (
                "https://api.ebay.com/oauth/api_scope/sell.inventory "
                "https://api.ebay.com/oauth/api_scope/sell.marketing"
            ),
        },
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


# ==========================================
# 画像ダウンロード
# ==========================================
def download_images(urls: list[str], workdir: str) -> list[str]:
    os.makedirs(workdir, exist_ok=True)
    paths = []
    for i, url in enumerate(urls[:MAX_IMAGES], 1):
        path = os.path.join(workdir, f"img{i}.jpg")
        r = requests.get(url, timeout=20)
        r.raise_for_status()
        with open(path, "wb") as f:
            f.write(r.content)
        paths.append(path)
    return paths


# ==========================================
# 動画生成（ffmpeg: Ken Burns + クロスフェード）
# ==========================================
def build_video(image_paths: list[str], output_path: str) -> None:
    if not image_paths:
        raise ValueError("画像が0枚のため動画を生成できません")

    workdir = os.path.dirname(output_path)
    clip_paths = []
    for i, img_path in enumerate(image_paths, 1):
        clip_path = os.path.join(workdir, f"clip{i}.mp4")
        vf = (
            f"scale={VIDEO_SIZE}:{VIDEO_SIZE}:force_original_aspect_ratio=decrease,"
            f"pad={VIDEO_SIZE}:{VIDEO_SIZE}:(ow-iw)/2:(oh-ih)/2:color=white,setsar=1,"
            f"zoompan=z='min(zoom+0.0015,1.15)':d={int(CLIP_DURATION * FPS)}:"
            f"s={VIDEO_SIZE}x{VIDEO_SIZE}:fps={FPS}"
        )
        subprocess.run(
            ["ffmpeg", "-y", "-loop", "1", "-i", img_path, "-t", str(CLIP_DURATION),
             "-vf", vf, "-c:v", "libx264", "-pix_fmt", "yuv420p", clip_path,
             "-loglevel", "error"],
            check=True,
        )
        clip_paths.append(clip_path)

    if len(clip_paths) == 1:
        os.replace(clip_paths[0], output_path)
        return

    inputs = []
    for p in clip_paths:
        inputs += ["-i", p]

    filter_parts = []
    prev_label = "0"
    offset = CLIP_DURATION - CROSSFADE_DUR
    for i in range(1, len(clip_paths)):
        out_label = f"v{i}" if i < len(clip_paths) - 1 else "vout"
        filter_parts.append(
            f"[{prev_label}][{i}]xfade=transition=fade:duration={CROSSFADE_DUR}:offset={offset}[{out_label}]"
        )
        prev_label = out_label
        offset += CLIP_DURATION - CROSSFADE_DUR

    filter_complex = "; ".join(filter_parts)
    subprocess.run(
        ["ffmpeg", "-y", *inputs, "-filter_complex", filter_complex,
         "-map", "[vout]", "-c:v", "libx264", "-pix_fmt", "yuv420p",
         "-movflags", "+faststart", output_path, "-loglevel", "error"],
        check=True,
    )


# ==========================================
# Media API（動画アップロード）
# ==========================================
def upload_video_to_ebay(video_path: str, title: str, description: str, oauth_token: str) -> str:
    """createVideo → uploadVideo でMedia APIに動画を登録し、videoIdを返す。"""
    size = os.path.getsize(video_path)

    resp = requests.post(
        f"{MEDIA_API_BASE}/video",
        headers={
            "Authorization": f"Bearer {oauth_token}",
            "Content-Type": "application/json",
            "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
        },
        json={
            "title": title[:80],
            "description": description[:500],
            "size": size,
            "classification": ["ITEM"],  # 文字列ではなく配列で指定する必要がある
        },
        timeout=15,
    )
    resp.raise_for_status()
    location = resp.headers.get("Location")
    if not location:
        raise RuntimeError(f"createVideo failed: {resp.text[:300]}")
    video_id = location.rstrip("/").split("/")[-1]

    with open(video_path, "rb") as f:
        data = f.read()
    upload_resp = requests.post(
        f"{MEDIA_API_BASE}/video/{video_id}/upload",
        headers={
            "Authorization": f"Bearer {oauth_token}",
            "Content-Type": "application/octet-stream",
            "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
        },
        data=data,
        timeout=60,
    )
    upload_resp.raise_for_status()

    # 処理完了(LIVE)まで待機
    for _ in range(12):
        status_resp = requests.get(
            f"{MEDIA_API_BASE}/video/{video_id}",
            headers={"Authorization": f"Bearer {oauth_token}", "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"},
            timeout=15,
        )
        status = status_resp.json().get("videoStatus") or status_resp.json().get("status")
        if status in ("LIVE", "READY", "AVAILABLE"):
            break
        time.sleep(5)

    return video_id


# ==========================================
# 1件分の処理
# ==========================================
def process_item(item_id: str, workdir_base: str, dry_run: bool, force: bool) -> dict:
    print(f"\n{'─'*55}")
    print(f"  Item ID: {item_id}")
    print(f"{'─'*55}")

    info = get_item_images(item_id)
    if not info.get("ok"):
        print("  → ❌ GetItem失敗。スキップ")
        return {"item_id": item_id, "status": "error", "reason": "GetItem失敗"}

    if info["status"] != "Active":
        print(f"  → ⚠️  出品が非Active（{info['status']}）。スキップ")
        return {"item_id": item_id, "status": "skipped", "reason": "非Active"}

    if info["existing_video_id"] and not force:
        print(f"  → ⚠️  既に動画あり（VideoID={info['existing_video_id']}）。--forceで上書き可。スキップ")
        return {"item_id": item_id, "status": "skipped", "reason": "既に動画あり"}

    if not info["pictures"]:
        print("  → ❌ 画像が0枚。スキップ")
        return {"item_id": item_id, "status": "skipped", "reason": "画像なし"}

    print(f"  タイトル: {info['title'][:60]}")
    print(f"  画像枚数: {len(info['pictures'])}（動画には最大{MAX_IMAGES}枚を使用）")

    workdir = os.path.join(workdir_base, item_id)
    os.makedirs(workdir, exist_ok=True)

    print("  [1/4] 画像ダウンロード中...")
    image_paths = download_images(info["pictures"], workdir)

    print("  [2/4] 動画生成中（ffmpeg）...")
    video_path = os.path.join(workdir, "pr_video.mp4")
    build_video(image_paths, video_path)
    size_kb = os.path.getsize(video_path) / 1024
    print(f"        → {video_path}（{size_kb:.0f} KB）")

    if dry_run:
        print("  → ✅ dry-run: 動画生成のみ完了（アップロードは行いません）")
        return {"item_id": item_id, "status": "dry_run", "video_path": video_path}

    print("  [3/4] eBay Media APIへアップロード中...")
    oauth_token = get_fresh_oauth_token()
    video_id = upload_video_to_ebay(
        video_path,
        title=f"{info['title'][:60]} - Promo Video",
        description="Auto-generated promo slideshow from listing images",
        oauth_token=oauth_token,
    )
    print(f"        → VideoID: {video_id}")

    print("  [4/4] 出品ページへ紐付け中（ReviseItem）...")
    ok, msg = attach_video_to_item(item_id, video_id)
    if ok:
        print(f"  → ✅ 完了: https://www.ebay.com/itm/{item_id}")
        return {"item_id": item_id, "status": "success", "video_id": video_id}
    else:
        print(f"  → ❌ 紐付け失敗: {msg}")
        return {"item_id": item_id, "status": "error", "reason": msg, "video_id": video_id}


# ==========================================
# メイン
# ==========================================
def main():
    args = sys.argv[1:]
    dry_run = "--dry-run" in args
    force = "--force" in args

    workdir_base = os.path.join(BASE_DIR, "pr_video_work")
    for i, a in enumerate(args):
        if a == "--workdir" and i + 1 < len(args):
            workdir_base = args[i + 1]

    limit = None
    for i, a in enumerate(args):
        if a == "--limit" and i + 1 < len(args):
            limit = int(args[i + 1])

    item_ids = []
    csv_path = None
    for i, a in enumerate(args):
        if a == "--csv" and i + 1 < len(args):
            csv_path = args[i + 1]

    if csv_path:
        with open(csv_path, encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                item_ids.append(str(row["Item ID"]))
        if limit:
            item_ids = item_ids[:limit]
    else:
        skip_args = {"--workdir", "--limit", "--csv"}
        skip_next = False
        for a in args:
            if skip_next:
                skip_next = False
                continue
            if a in skip_args:
                skip_next = True
                continue
            if a.startswith("--"):
                continue
            item_ids.append(a)

    if not item_ids:
        print(__doc__)
        sys.exit(1)

    mode = "[DRY-RUN]" if dry_run else "[本番]"
    print(f"\n{'='*55}")
    print(f"  PR動画生成 {mode}  対象: {len(item_ids)}件")
    print(f"{'='*55}")

    results = []
    for item_id in item_ids:
        try:
            r = process_item(item_id.strip(), workdir_base, dry_run, force)
        except Exception as e:
            print(f"  → ❌ 例外エラー: {e}")
            r = {"item_id": item_id, "status": "error", "reason": str(e)}
        results.append(r)
        time.sleep(1)

    print(f"\n{'='*55}")
    print("  完了サマリー")
    print(f"{'='*55}")
    for status in ("success", "dry_run", "skipped", "error"):
        matched = [r for r in results if r["status"] == status]
        if matched:
            print(f"  {status}: {len(matched)}件")
            for r in matched:
                extra = r.get("reason") or r.get("video_id") or ""
                print(f"    - {r['item_id']} {extra}")
    print(f"{'='*55}\n")


if __name__ == "__main__":
    main()
