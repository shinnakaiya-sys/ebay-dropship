"""
kaworu2021 ストアカテゴリー再編 本番実行スクリプト。

手順:
  1. (--refetch) store_audit.json でebay_category_nameが空の商品だけ再取得
  2. (--create-categories) SetStoreCategoriesで新13カテゴリー(+子)を作成し、IDマッピングを保存
  3. (--assign) 全出品にReviseItemで新カテゴリーを設定(チェックポイント付き・再開可能)

使い方:
  python3 run_migration.py --refetch --create-categories --assign
  (途中で失敗した場合、同じコマンドを再実行すれば続きから再開する)
"""
import argparse
import json
import sys
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

sys.path.insert(0, "/Users/nakaiya_shin/ebay-kaworu")
from config import CONFIG  # noqa: E402
from category_map import NEW_TREE, map_item  # noqa: E402

NS = "{urn:ebay:apis:eBLBaseComponents}"
EBAY_API_URL = "https://api.ebay.com/ws/api.dll"
TOKEN = CONFIG["EBAY_TOKEN"]
SCRATCH = "/Users/nakaiya_shin/ebay-kaworu/store_category_migration"
AUDIT_PATH = f"{SCRATCH}/store_audit.json"
CATID_PATH = f"{SCRATCH}/new_category_ids.json"
ASSIGN_STATE_PATH = f"{SCRATCH}/assign_state.json"

HEADERS_BASE = {
    "X-EBAY-API-SITEID": "0",
    "X-EBAY-API-COMPATIBILITY-LEVEL": "967",
    "X-EBAY-API-IAF-TOKEN": TOKEN,
    "Content-Type": "text/xml",
}


def is_usage_limit_error(text: str) -> bool:
    return "usage limit" in text.lower() or "10007" in text


# ──────────────────────────────────────────────
# STEP 1: 未取得ぶんのカテゴリー再取得
# ──────────────────────────────────────────────

def get_item_storecat(item_id):
    xml_body = f"""<?xml version="1.0" encoding="utf-8"?>
<GetItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">
  <RequesterCredentials><eBayAuthToken>{TOKEN}</eBayAuthToken></RequesterCredentials>
  <ItemID>{item_id}</ItemID>
  <DetailLevel>ReturnAll</DetailLevel>
</GetItemRequest>"""
    headers = {**HEADERS_BASE, "X-EBAY-API-CALL-NAME": "GetItem"}
    for attempt in range(2):
        try:
            resp = requests.post(EBAY_API_URL, headers=headers, data=xml_body.encode("utf-8"), timeout=10)
            root = ET.fromstring(resp.text)
            ack = root.findtext(f"{NS}Ack") or ""
            if ack not in ("Success", "Warning"):
                if is_usage_limit_error(resp.text):
                    return item_id, None, None, None, None, "LIMIT"
                continue
            item = root.find(f"{NS}Item")
            sf = item.find(f"{NS}Storefront") if item is not None else None
            sc1 = sf.findtext(f"{NS}StoreCategoryID") if sf is not None else ""
            sc2 = sf.findtext(f"{NS}StoreCategory2ID") if sf is not None else ""
            cat_id = item.findtext(f"{NS}PrimaryCategory/{NS}CategoryID") if item is not None else ""
            cat_name = item.findtext(f"{NS}PrimaryCategory/{NS}CategoryName") if item is not None else ""
            return item_id, sc1 or "", sc2 or "", cat_id or "", cat_name or "", None
        except Exception:
            time.sleep(0.5)
    return item_id, "", "", "", "", "FAIL"


def step_refetch():
    with open(AUDIT_PATH, encoding="utf-8") as f:
        data = json.load(f)
    listings = data["listings"]
    by_id = {l["item_id"]: l for l in listings}
    todo = [l["item_id"] for l in listings if not l.get("ebay_category_name")]
    print(f"[refetch] {len(todo)} items missing ebay_category_name, fetching...", flush=True)
    if not todo:
        return True

    hit_limit = False
    done = 0
    with ThreadPoolExecutor(max_workers=12) as ex:
        futures = {ex.submit(get_item_storecat, iid): iid for iid in todo}
        for fut in as_completed(futures):
            item_id, sc1, sc2, cat_id, cat_name, err = fut.result()
            if err == "LIMIT":
                hit_limit = True
                continue
            if err:
                continue
            by_id[item_id]["store_category_id"] = sc1
            by_id[item_id]["store_category2_id"] = sc2
            by_id[item_id]["ebay_category_id"] = cat_id
            by_id[item_id]["ebay_category_name"] = cat_name
            done += 1
            if done % 100 == 0:
                print(f"  [refetch] {done}/{len(todo)}", flush=True)
                data["listings"] = list(by_id.values())
                with open(AUDIT_PATH, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)

    data["listings"] = list(by_id.values())
    with open(AUDIT_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    remaining = sum(1 for l in data["listings"] if not l.get("ebay_category_name"))
    print(f"[refetch] done. {done} fetched, {remaining} still missing (will map to 'Other Japan Goods').", flush=True)
    if hit_limit:
        print("[refetch] !! GetItem usage limit hit during refetch. Stopping refetch early.", flush=True)
    return True


# ──────────────────────────────────────────────
# STEP 2: 新カテゴリー作成
# ──────────────────────────────────────────────

def xml_escape(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
             .replace('"', "&quot;").replace("'", "&apos;"))


def step_create_categories():
    parts = []
    order = 1
    for parent_name, children in NEW_TREE:
        child_xml = "".join(
            f"<ChildCategory><Name>{xml_escape(c)}</Name></ChildCategory>" for c in children
        )
        parts.append(
            f"<CustomCategory><Name>{xml_escape(parent_name)}</Name><Order>{order}</Order>{child_xml}</CustomCategory>"
        )
        order += 1

    xml_body = f"""<?xml version="1.0" encoding="utf-8"?>
<SetStoreCategoriesRequest xmlns="urn:ebay:apis:eBLBaseComponents">
  <RequesterCredentials><eBayAuthToken>{TOKEN}</eBayAuthToken></RequesterCredentials>
  <CustomCategories>{''.join(parts)}</CustomCategories>
</SetStoreCategoriesRequest>"""

    headers = {**HEADERS_BASE, "X-EBAY-API-CALL-NAME": "SetStoreCategories"}
    resp = requests.post(EBAY_API_URL, headers=headers, data=xml_body.encode("utf-8"), timeout=60)
    root = ET.fromstring(resp.text)
    ack = root.findtext(f"{NS}Ack") or ""
    print(f"[create_categories] Ack={ack}", flush=True)
    if ack not in ("Success", "Warning"):
        print(resp.text[:2000], flush=True)
        return False

    # 反映確認のため GetStore を呼び直してID割当を取得
    time.sleep(2)
    get_xml = f"""<?xml version="1.0" encoding="utf-8"?>
<GetStoreRequest xmlns="urn:ebay:apis:eBLBaseComponents">
  <RequesterCredentials><eBayAuthToken>{TOKEN}</eBayAuthToken></RequesterCredentials>
</GetStoreRequest>"""
    get_headers = {**HEADERS_BASE, "X-EBAY-API-CALL-NAME": "GetStore"}
    resp2 = requests.post(EBAY_API_URL, headers=get_headers, data=get_xml.encode("utf-8"), timeout=30)
    root2 = ET.fromstring(resp2.text)
    store = root2.find(f"{NS}Store")
    id_map = {}
    if store is not None:
        cust = store.find(f"{NS}CustomCategories")
        if cust is not None:
            for c in cust.findall(f"{NS}CustomCategory"):
                pname = c.findtext(f"{NS}Name")
                pid = c.findtext(f"{NS}CategoryID")
                id_map[pname] = {"id": pid, "children": {}}
                for child in c.findall(f"{NS}ChildCategory"):
                    cname = child.findtext(f"{NS}Name")
                    cid = child.findtext(f"{NS}CategoryID")
                    id_map[pname]["children"][cname] = cid

    with open(CATID_PATH, "w", encoding="utf-8") as f:
        json.dump(id_map, f, ensure_ascii=False, indent=2)
    print(f"[create_categories] saved ID map to {CATID_PATH}", flush=True)

    missing = [p for p, _ in NEW_TREE if p not in id_map]
    if missing:
        print(f"[create_categories] !! WARNING missing parents in result: {missing}", flush=True)
        return False
    return True


# ──────────────────────────────────────────────
# STEP 3: 全出品を新カテゴリーへ振り分け
# ──────────────────────────────────────────────

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


def load_assign_state():
    try:
        with open(ASSIGN_STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save_assign_state(state):
    with open(ASSIGN_STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def step_assign():
    with open(AUDIT_PATH, encoding="utf-8") as f:
        data = json.load(f)
    with open(CATID_PATH, encoding="utf-8") as f:
        id_map = json.load(f)

    state = load_assign_state()  # item_id -> "OK" | "FAIL" | "LIMIT"

    plan = []  # (item_id, target_category_id)
    for l in data["listings"]:
        item_id = l["item_id"]
        if state.get(item_id) == "OK":
            continue
        parent, child = map_item(l.get("ebay_category_name", ""))
        pinfo = id_map.get(parent)
        if not pinfo:
            print(f"  !! no id for parent '{parent}', skipping item {item_id}", flush=True)
            continue
        if child and child in pinfo["children"]:
            cat_id = pinfo["children"][child]
        else:
            cat_id = pinfo["id"]
        plan.append((item_id, cat_id))

    total = len(plan)
    print(f"[assign] {total} items to assign (already done: {sum(1 for v in state.values() if v=='OK')})", flush=True)

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
            if done % 100 == 0:
                print(f"  [assign] {done}/{total}", flush=True)
                save_assign_state(state)

    save_assign_state(state)
    ok = sum(1 for v in state.values() if v == "OK")
    fail = sum(1 for v in state.values() if v == "FAIL")
    limit = sum(1 for v in state.values() if v == "LIMIT")
    print(f"[assign] done. OK={ok} FAIL={fail} LIMIT={limit}", flush=True)
    if hit_limit:
        print("[assign] !! ReviseItem usage limit hit. Re-run this script later to resume remaining items.", flush=True)
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--refetch", action="store_true")
    ap.add_argument("--create-categories", action="store_true")
    ap.add_argument("--assign", action="store_true")
    args = ap.parse_args()

    if args.refetch:
        step_refetch()
    if args.create_categories:
        ok = step_create_categories()
        if not ok:
            print("!! category creation failed, aborting before assign step.", flush=True)
            sys.exit(1)
    if args.assign:
        step_assign()


if __name__ == "__main__":
    main()
