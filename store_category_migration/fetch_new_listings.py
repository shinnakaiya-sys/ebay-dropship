"""
新規出品(store_audit.jsonに未登録のitem_id)を検出し、最小エントリとして追記する。
カテゴリー情報はrun_migration.py --refetch が後で埋める。
"""
import json
import sys
import time
import xml.etree.ElementTree as ET

import requests

sys.path.insert(0, "/Users/nakaiya_shin/ebay-kaworu")
from config import CONFIG  # noqa: E402

NS = "{urn:ebay:apis:eBLBaseComponents}"
EBAY_API_URL = "https://api.ebay.com/ws/api.dll"
TOKEN = CONFIG["EBAY_TOKEN"]
SCRATCH = "/Users/nakaiya_shin/ebay-kaworu/store_category_migration"
AUDIT_PATH = f"{SCRATCH}/store_audit.json"

HEADERS_BASE = {
    "X-EBAY-API-SITEID": "0",
    "X-EBAY-API-COMPATIBILITY-LEVEL": "967",
    "X-EBAY-API-IAF-TOKEN": TOKEN,
    "Content-Type": "text/xml",
}


def get_all_active_item_ids():
    ids = []
    page = 1
    total_pages = 1
    while page <= total_pages:
        xml_body = f"""<?xml version="1.0" encoding="utf-8"?>
<GetMyeBaySellingRequest xmlns="urn:ebay:apis:eBLBaseComponents">
  <RequesterCredentials><eBayAuthToken>{TOKEN}</eBayAuthToken></RequesterCredentials>
  <ActiveList>
    <Sort>TimeLeft</Sort>
    <Pagination>
      <EntriesPerPage>200</EntriesPerPage>
      <PageNumber>{page}</PageNumber>
    </Pagination>
  </ActiveList>
  <DetailLevel>ReturnAll</DetailLevel>
</GetMyeBaySellingRequest>"""
        headers = {**HEADERS_BASE, "X-EBAY-API-CALL-NAME": "GetMyeBaySelling"}
        resp = requests.post(EBAY_API_URL, headers=headers, data=xml_body.encode("utf-8"), timeout=30)
        root = ET.fromstring(resp.text)
        ack = root.findtext(f"{NS}Ack") or ""
        if ack not in ("Success", "Warning"):
            msgs = [e.findtext(f"{NS}LongMessage") for e in root.findall(f"{NS}Errors")]
            print(f"  !! GetMyeBaySelling失敗(page {page}): {msgs}", flush=True)
            break
        active = root.find(f"{NS}ActiveList")
        if active is None:
            break
        item_array = active.find(f"{NS}ItemArray")
        page_count = 0
        if item_array is not None:
            for item in item_array.findall(f"{NS}Item"):
                iid = item.findtext(f"{NS}ItemID") or ""
                title = item.findtext(f"{NS}Title") or ""
                price = item.findtext(f"{NS}SellingStatus/{NS}CurrentPrice") or ""
                qty = item.findtext(f"{NS}QuantityAvailable") or item.findtext(f"{NS}Quantity") or ""
                ids.append({"item_id": iid, "title": title, "price_usd": price, "quantity": qty})
                page_count += 1
        pagination = active.find(f"{NS}PaginationResult")
        if pagination is not None:
            total_pages = int(pagination.findtext(f"{NS}TotalNumberOfPages") or 1)
        print(f"  page {page}/{total_pages}: {page_count}件", flush=True)
        page += 1
        time.sleep(0.3)
    return ids


def main():
    print("現在の全出品を取得中...", flush=True)
    current = get_all_active_item_ids()
    print(f"現在の出品総数: {len(current)}", flush=True)

    with open(AUDIT_PATH, encoding="utf-8") as f:
        data = json.load(f)

    existing_ids = {l["item_id"] for l in data["listings"]}
    new_items = [c for c in current if c["item_id"] not in existing_ids]
    print(f"未登録(新規)出品: {len(new_items)}件", flush=True)

    for c in new_items:
        data["listings"].append({
            "item_id": c["item_id"],
            "title": c["title"],
            "store_category_id": "",
            "store_category2_id": "",
            "ebay_category_id": "",
            "ebay_category_name": "",
            "price_usd": c["price_usd"],
            "quantity": c["quantity"],
        })

    with open(AUDIT_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"store_audit.json 更新完了。総件数: {len(data['listings'])}", flush=True)


if __name__ == "__main__":
    main()
