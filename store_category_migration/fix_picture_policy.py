"""
画像ポリシー違反(500px未満)でReviseItemが拒否されている出品を修正する。
既存の低解像度画像をダウンロード→アップスケール→UploadSiteHostedPicturesで再ホスト
→ReviseItemで新画像URL+ストアカテゴリーを同時に設定する。

使い方:
  python3 fix_picture_policy.py --test   # 1件だけ試す
  python3 fix_picture_policy.py          # 全件実行(チェックポイント対応)
"""
import argparse
import io
import json
import sys
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from PIL import Image

sys.path.insert(0, "/Users/nakaiya_shin/ebay-kaworu")
from config import CONFIG  # noqa: E402
from category_map_existing import map_item_existing  # noqa: E402

NS = "{urn:ebay:apis:eBLBaseComponents}"
EBAY_API_URL = "https://api.ebay.com/ws/api.dll"
TOKEN = CONFIG["EBAY_TOKEN"]
SCRATCH = "/Users/nakaiya_shin/ebay-kaworu/store_category_migration"
AUDIT_PATH = f"{SCRATCH}/store_audit.json"
STATE_PATH = f"{SCRATCH}/picture_fix_state.json"
MIN_SIDE = 600  # 500px要件に余裕を持たせる

HEADERS_BASE = {
    "X-EBAY-API-SITEID": "0",
    "X-EBAY-API-COMPATIBILITY-LEVEL": "967",
    "X-EBAY-API-IAF-TOKEN": TOKEN,
}


def get_item_pictures_and_ebaycat(item_id):
    xml_body = f"""<?xml version="1.0" encoding="utf-8"?>
<GetItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">
  <RequesterCredentials><eBayAuthToken>{TOKEN}</eBayAuthToken></RequesterCredentials>
  <ItemID>{item_id}</ItemID>
  <DetailLevel>ReturnAll</DetailLevel>
</GetItemRequest>"""
    headers = {**HEADERS_BASE, "Content-Type": "text/xml", "X-EBAY-API-CALL-NAME": "GetItem"}
    resp = requests.post(EBAY_API_URL, headers=headers, data=xml_body.encode("utf-8"), timeout=15)
    root = ET.fromstring(resp.text)
    item = root.find(f"{NS}Item")
    pd = item.find(f"{NS}PictureDetails")
    urls = [u.text for u in pd.findall(f"{NS}PictureURL")] if pd is not None else []
    cat_name = item.findtext(f"{NS}PrimaryCategory/{NS}CategoryName") or ""
    return urls, cat_name


def upscale_image(img_bytes: bytes) -> bytes:
    im = Image.open(io.BytesIO(img_bytes))
    im = im.convert("RGB")
    w, h = im.size
    longest = max(w, h)
    if longest < MIN_SIDE:
        scale = MIN_SIDE / longest
        new_size = (max(1, round(w * scale)), max(1, round(h * scale)))
        im = im.resize(new_size, Image.LANCZOS)
    out = io.BytesIO()
    im.save(out, format="JPEG", quality=90)
    return out.getvalue()


def upload_site_hosted_picture(image_bytes: bytes, picture_name: str) -> str:
    xml_body = f"""<?xml version="1.0" encoding="utf-8"?>
<UploadSiteHostedPicturesRequest xmlns="urn:ebay:apis:eBLBaseComponents">
  <RequesterCredentials><eBayAuthToken>{TOKEN}</eBayAuthToken></RequesterCredentials>
  <PictureName>{picture_name}</PictureName>
</UploadSiteHostedPicturesRequest>"""
    headers = {**HEADERS_BASE, "X-EBAY-API-CALL-NAME": "UploadSiteHostedPictures"}
    files = [
        ("XML Payload", (None, xml_body, "text/xml")),
        ("image", ("image.jpg", image_bytes, "image/jpeg")),
    ]
    resp = requests.post(EBAY_API_URL, headers=headers, files=files, timeout=60)
    root = ET.fromstring(resp.text)
    ack = root.findtext(f"{NS}Ack") or ""
    if ack not in ("Success", "Warning"):
        msgs = [e.findtext(f"{NS}LongMessage") for e in root.findall(f"{NS}Errors")]
        raise RuntimeError(f"UploadSiteHostedPictures failed: Ack={ack} {msgs}")
    full_url = root.findtext(f"{NS}SiteHostedPictureDetails/{NS}FullURL")
    if not full_url:
        raise RuntimeError("UploadSiteHostedPictures: no FullURL in response")
    return full_url


def revise_item_pics_and_category(item_id, new_pic_urls, category_id):
    pics_xml = "".join(f"<PictureURL>{u}</PictureURL>" for u in new_pic_urls)
    xml_body = f"""<?xml version="1.0" encoding="utf-8"?>
<ReviseItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">
  <RequesterCredentials><eBayAuthToken>{TOKEN}</eBayAuthToken></RequesterCredentials>
  <Item>
    <ItemID>{item_id}</ItemID>
    <PictureDetails>{pics_xml}</PictureDetails>
    <Storefront>
      <StoreCategoryID>{category_id}</StoreCategoryID>
    </Storefront>
  </Item>
</ReviseItemRequest>"""
    headers = {**HEADERS_BASE, "Content-Type": "text/xml", "X-EBAY-API-CALL-NAME": "ReviseItem"}
    resp = requests.post(EBAY_API_URL, headers=headers, data=xml_body.encode("utf-8"), timeout=30)
    root = ET.fromstring(resp.text)
    ack = root.findtext(f"{NS}Ack") or ""
    if ack in ("Success", "Warning"):
        return True, None
    msgs = [e.findtext(f"{NS}LongMessage") for e in root.findall(f"{NS}Errors")]
    return False, f"Ack={ack} {msgs}"


def get_existing_categories():
    xml_body = f"""<?xml version="1.0" encoding="utf-8"?>
<GetStoreRequest xmlns="urn:ebay:apis:eBLBaseComponents">
  <RequesterCredentials><eBayAuthToken>{TOKEN}</eBayAuthToken></RequesterCredentials>
</GetStoreRequest>"""
    headers = {**HEADERS_BASE, "Content-Type": "text/xml", "X-EBAY-API-CALL-NAME": "GetStore"}
    resp = requests.post(EBAY_API_URL, headers=headers, data=xml_body.encode("utf-8"), timeout=30)
    root = ET.fromstring(resp.text)
    store = root.find(f"{NS}Store")
    id_map = {}
    cust = store.find(f"{NS}CustomCategories")
    for c in cust.findall(f"{NS}CustomCategory"):
        id_map[c.findtext(f"{NS}Name")] = c.findtext(f"{NS}CategoryID")
    return id_map


def process_item(item_id, target_category_id):
    try:
        urls, cat_name = get_item_pictures_and_ebaycat(item_id)
        if not urls:
            return item_id, "SKIP", "no pictures"

        new_urls = []
        for i, u in enumerate(urls):
            r = requests.get(u, timeout=20)
            r.raise_for_status()
            fixed = upscale_image(r.content)
            new_url = upload_site_hosted_picture(fixed, f"{item_id}_{i}")
            new_urls.append(new_url)

        ok, err = revise_item_pics_and_category(item_id, new_urls, target_category_id)
        if ok:
            return item_id, "OK", None
        return item_id, "FAIL", err
    except Exception as e:
        return item_id, "FAIL", str(e)


def load_state():
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save_state(state):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true", help="1件だけ処理して終了")
    args = ap.parse_args()

    with open(f"{SCRATCH}/interim_assign_state.json", encoding="utf-8") as f:
        assign_state = json.load(f)
    fail_ids = [k for k, v in assign_state.items() if v == "FAIL"]

    with open(AUDIT_PATH, encoding="utf-8") as f:
        data = json.load(f)
    by_id = {l["item_id"]: l for l in data["listings"]}

    print("既存カテゴリー一覧を取得中...", flush=True)
    id_map = get_existing_categories()

    state = load_state()
    todo = [iid for iid in fail_ids if state.get(iid) != "OK"]
    if args.test:
        todo = todo[:1]

    print(f"[fix_picture_policy] {len(todo)} items to process", flush=True)

    def target_cat_id(item_id):
        l = by_id.get(item_id, {})
        name = map_item_existing(l.get("ebay_category_name", ""))
        return id_map.get(name, id_map.get("Other"))

    done = 0
    with ThreadPoolExecutor(max_workers=4) as ex:
        futures = {ex.submit(process_item, iid, target_cat_id(iid)): iid for iid in todo}
        for fut in as_completed(futures):
            item_id, status, err = fut.result()
            state[item_id] = status
            done += 1
            print(f"  [{done}/{len(todo)}] {item_id}: {status}" + (f" ({err})" if err else ""), flush=True)
            save_state(state)

    ok = sum(1 for v in state.values() if v == "OK")
    fail = sum(1 for v in state.values() if v == "FAIL")
    skip = sum(1 for v in state.values() if v == "SKIP")
    print(f"[fix_picture_policy] done. OK={ok} FAIL={fail} SKIP={skip}", flush=True)


if __name__ == "__main__":
    main()
