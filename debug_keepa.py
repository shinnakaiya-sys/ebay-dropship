import keepa, os
from dotenv import load_dotenv
load_dotenv()

api = keepa.Keepa(os.getenv('KEEPA_API_KEY'))
products = api.query(['B002CD6V10'], domain='JP', history=True, offers=20, stock=True)
p = products[0]

print("=== images キー ===")
images = p.get("images")
print(type(images), images)

print("\n=== imagesCSV キー ===")
print(p.get("imagesCSV"))
