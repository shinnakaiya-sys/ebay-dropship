"""
暫定措置: SetStoreCategoriesがブロックされている間、既存22カテゴリーへ商品を振り分ける。
本来の14カテゴリー構成への正式移行は、SetStoreCategoriesの問題解決後にrun_migration.pyで行う。

使い方:
  python3 interim_assign.py
  (途中で失敗した場合、同じコマンドを再実行すれば続きから再開する)
"""
import json
import sys
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

sys.path.insert(0, "/Users/nakaiya_shin/ebay-kaworu")
from config import CONFIG  # noqa: E402
from category_map_existing import map_item_existing  # noqa: E402

NS = "{urn:ebay:apis:eBLBaseComponents}"
EBAY_API_URL = "https://api.ebay.com/ws/api.dll"
TOKEN = CONFIG["EBAY_TOKEN"]
SCRATCH = "/Users/nakaiya_shin/ebay-kaworu/store_category_migration"
AUDIT_PATH = f"{SCRATCH}/store_audit.json"
STATE_PATH = f"{SCRATCH}/interim_assign_state.json"

HEADERS_BASE = {
    "X-EBAY-API-SITEID": "0",
    "X-EBAY-API-COMPATIBILITY-LEVEL": "967",
    "X-EBAY-API-IAF-TOKEN": TOKEN,
    "Content-Type": "text/xml",
}


def is_usage_limit_error(text: str) -> bool:
    return "usage limit" in text.lower() or "10007" in text


def get_existing_categories():
    xml_body = f"""<?xml version="1.0" encoding="utf-8"?>
<GetStoreRequest xmlns="urn:ebay:apis:eBLBaseComponents">
  <RequesterCredentials><eBayAuthToken>{TOKEN}</eBayAuthToken></RequesterCredentials>
</GetStoreRequest>"""
    headers = {**HEADERS_BASE, "X-EBAY-API-CALL-NAME": "GetStore"}
    resp = requests.post(EBAY_API_URL, headers=headers, data=xml_body.encode("utf-8"), timeout=30)
    root = ET.fromstring(resp.text)
    store = root.find(f"{NS}Store")
    id_map = {}
    cust = store.find(f"{NS}CustomCategories")
    for c in cust.findall(f"{NS}CustomCategory"):
        name = c.findtext(f"{NS}Name")
        cid = c.findtext(f"{NS}CategoryID")
        id_map[name] = cid
    return id_map


def revise_store_category(item_id, category_id):
    xml_body = f"""<?xml version="1.0" encoding="utf-8"?>
<ReviseItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">
  <RequesterCredentials><eBayAuthToken>{TOKEN}</eBayAuthToken></RequesterCredentials>
  <Item>
    <ItemID>{item_id}</ItemID>
    <Storefront>
      <StoreCategoryID>{category_id}</StoreCategoryID>
    </Storefront>
  </Item>
</ReviseItemRequest>"""
    headers = {**HEADERS_BASE, "X-EBAY-API-CALL-NAME": "ReviseItem"}
    for attempt in range(2):
        try:
            resp = requests.post(EBAY_API_URL, headers=headers, data=xml_body.encode("utf-8"), timeout=15)
            root = ET.fromstring(resp.text)
            ack = root.findtext(f"{NS}Ack") or ""
            if ack in ("Success", "Warning"):
                return item_id, "OK", None
            if is_usage_limit_error(resp.text):
                return item_id, "LIMIT", resp.text[:300]
            msgs = [e.findtext(f"{NS}LongMessage") for e in root.findall(f"{NS}Errors")]
            last_err = f"Ack={ack} {msgs}"
        except Exception as e:
            last_err = str(e)
        time.sleep(0.5)
    return item_id, "FAIL", last_err


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
    print("既存カテゴリー一覧を取得中...", flush=True)
    id_map = get_existing_categories()
    print(f"  {len(id_map)} 件取得: {list(id_map.keys())}", flush=True)

    with open(AUDIT_PATH, encoding="utf-8") as f:
        data = json.load(f)

    state = load_state()

    plan = []  # (item_id, target_category_id)
    skipped_noop = 0
    for l in data["listings"]:
        item_id = l["item_id"]
        if state.get(item_id) == "OK":
            continue
        target_name = map_item_existing(l.get("ebay_category_name", ""))
        target_id = id_map.get(target_name)
        if not target_id:
            print(f"  !! no id for '{target_name}', skipping item {item_id}", flush=True)
            continue
        current_id = l.get("store_category_id") or ""
        if current_id == target_id:
            skipped_noop += 1
            state[item_id] = "OK"
            continue
        plan.append((item_id, target_id))

    total = len(plan)
    print(f"[interim_assign] {total} items to reassign "
          f"(already matching: {skipped_noop}, already done: {sum(1 for v in state.values() if v=='OK') - skipped_noop})",
          flush=True)

    hit_limit = False
    done = 0
    with ThreadPoolExecutor(max_workers=10) as ex:
        futures = {ex.submit(revise_store_category, iid, cid): iid for iid, cid in plan}
        for fut in as_completed(futures):
            item_id, status, err = fut.result()
            state[item_id] = status
            done += 1
            if status == "LIMIT":
                hit_limit = True
            if status == "FAIL":
                print(f"  !! FAIL {item_id}: {err}", flush=True)
            if done % 100 == 0:
                print(f"  [interim_assign] {done}/{total}", flush=True)
                save_state(state)

    save_state(state)
    ok = sum(1 for v in state.values() if v == "OK")
    fail = sum(1 for v in state.values() if v == "FAIL")
    limit = sum(1 for v in state.values() if v == "LIMIT")
    print(f"[interim_assign] done. OK={ok} FAIL={fail} LIMIT={limit}", flush=True)
    if hit_limit:
        print("[interim_assign] !! ReviseItem usage limit hit. Re-run this script later to resume remaining items.", flush=True)


if __name__ == "__main__":
    main()
