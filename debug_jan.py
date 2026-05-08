"""
KeepaのJAN→ASIN変換方法を調べるデバッグスクリプト
"""
import keepa, os, inspect
from dotenv import load_dotenv
load_dotenv()

api = keepa.Keepa(os.getenv('KEEPA_API_KEY'))

# 利用可能なメソッドを確認
methods = [m for m in dir(api) if not m.startswith('_')]
print("=== Keepa APIメソッド一覧 ===")
for m in methods:
    print(f"  {m}")

# search_productsのシグネチャを確認
if hasattr(api, 'search_products'):
    print("\n=== search_products シグネチャ ===")
    print(inspect.signature(api.search_products))

# product_finderのシグネチャを確認
if hasattr(api, 'product_finder'):
    print("\n=== product_finder シグネチャ ===")
    print(inspect.signature(api.product_finder))
