import requests, os, keepa
from dotenv import load_dotenv
load_dotenv()

token = os.getenv('EBAY_TOKEN')
keepa_api = keepa.Keepa(os.getenv('KEEPA_API_KEY'))

# B002CD6V10のUPC/EAN/MPN情報を取得
asin = 'B002CD6V10'
products = keepa_api.query([asin], domain='JP', history=True, offers=20, stock=True)
p = products[0]

upc_list = p.get('upcList') or []
ean_list = p.get('eanList') or []
part_num = p.get('partNumber') or ''
model    = p.get('model') or ''
brand    = p.get('brand') or 'Does Not Apply'

print(f"UPC: {upc_list}")
print(f"EAN: {ean_list}")
print(f"PartNumber: {part_num}")
print(f"Model: {model}")
print(f"Brand: {brand}")

# 使う値を決定
upc = upc_list[0] if upc_list else 'Does not apply'
mpn = part_num or model or 'Does Not Apply'

print(f"\n→ 使用するUPC: {upc}")
print(f"→ 使用するMPN: {mpn}")

# eBayに出品テスト
xml = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<AddItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">'
    '<RequesterCredentials>'
    '<eBayAuthToken>' + token + '</eBayAuthToken>'
    '</RequesterCredentials>'
    '<Item>'
    '<Title>Test Item Japan JICO Record Needle</Title>'
    '<Description>Test description from Japan</Description>'
    '<PrimaryCategory><CategoryID>14943</CategoryID></PrimaryCategory>'
    '<StartPrice>25.00</StartPrice>'
    '<ConditionID>1000</ConditionID>'
    '<Country>JP</Country>'
    '<Currency>USD</Currency>'
    '<Location>Tokyo, Japan</Location>'
    '<DispatchTimeMax>7</DispatchTimeMax>'
    '<ListingDuration>GTC</ListingDuration>'
    '<ListingType>FixedPriceItem</ListingType>'
    '<Quantity>1</Quantity>'
    '<ShipToLocations>Worldwide</ShipToLocations>'
    '<ProductListingDetails>'
    f'<UPC>{upc}</UPC>'
    '</ProductListingDetails>'
    '<PictureDetails>'
    '<PictureURL>https://images-na.ssl-images-amazon.com/images/I/41mJXuMWPQL.jpg</PictureURL>'
    '</PictureDetails>'
    '<ItemSpecifics>'
    f'<NameValueList><Name>Brand</Name><Value>{brand}</Value></NameValueList>'
    f'<NameValueList><Name>MPN</Name><Value>{mpn}</Value></NameValueList>'
    '</ItemSpecifics>'
    '<SellerProfiles>'
    '<SellerShippingProfile><ShippingProfileID>362218680023</ShippingProfileID></SellerShippingProfile>'
    '<SellerReturnProfile><ReturnProfileID>260040355023</ReturnProfileID></SellerReturnProfile>'
    '<SellerPaymentProfile><PaymentProfileID>319160588023</PaymentProfileID></SellerPaymentProfile>'
    '</SellerProfiles>'
    '</Item>'
    '</AddItemRequest>'
)
headers = {
    'X-EBAY-API-SITEID': '0',
    'X-EBAY-API-COMPATIBILITY-LEVEL': '967',
    'X-EBAY-API-IAF-TOKEN': token,
    'X-EBAY-API-CALL-NAME': 'AddItem',
    'Content-Type': 'text/xml',
}
resp = requests.post(
    'https://api.ebay.com/ws/api.dll',
    headers=headers,
    data=xml.encode('utf-8'),
    timeout=30
)
print(f"\n=== eBay APIレスポンス ===")
print(resp.text[:3000])
