"""
新ストアカテゴリーツリーの定義と、既存eBay公式カテゴリー(title側で確認したPrimaryCategory)から
新カテゴリーへのマッピングルール。
"""

# (親カテゴリー名, [子カテゴリー名, ...])  子が空リストなら子カテゴリーなし
NEW_TREE = [
    ("Toys, Models & Hobby Kits", [
        "Plastic Models & Kits", "Model Trains & Railroads", "Action Figures & Anime Goods",
        "RC Vehicles & Parts", "Trading Cards & CCG", "Diecast & Toy Vehicles",
        "Puzzles & Games", "Other Toys & Hobbies",
    ]),
    ("Sporting Goods", [
        "Fishing Gear & Tackle", "Golf", "Tennis & Racquet Sports", "Cycling",
        "Camping & Outdoor", "Other Sporting Goods",
    ]),
    ("Home, Kitchen & Tools", [
        "Kitchen & Dining", "Tools & Workshop Equipment", "Precision & Industrial Tools",
        "Office & Stationery", "Yard & Garden", "Home Improvement & Decor",
    ]),
    ("Jewelry & Watches", ["Watches", "Fashion & Fine Jewelry"]),
    ("Cameras & Photography", [
        "Lenses & Filters", "Camera Accessories", "Digital Cameras & Camcorders",
        "Binoculars, Telescopes & Film",
    ]),
    ("Music, Movies & Media", ["CDs", "Vinyl Records", "DVD & Blu-ray", "Other Music Media"]),
    ("Musical Instruments & Pro Audio", [
        "Guitars & Basses", "Keyboards & Pianos", "Wind, Brass & Percussion",
        "DJ & Pro Audio Equipment", "Other Instruments & Accessories",
    ]),
    ("Consumer Electronics & Computers", [
        "TV, Video & Home Audio", "Portable Audio & Headphones", "Radio & Vehicle Electronics",
        "Computers & Networking", "Cell Phone Accessories", "Other Electronics",
    ]),
    ("Video Games & Consoles", ["Video Games", "Consoles", "Accessories & Parts"]),
    ("Health & Beauty", [
        "Shaving & Hair Removal", "Hair, Skin & Makeup", "Nail & Oral Care", "Health Care & Wellness",
    ]),
    ("Collectibles & Anime Merchandise", ["Comics, Pens & Memorabilia"]),
    ("Clothing & Craft Supplies", ["Clothing & Accessories", "Craft Supplies"]),
    ("Other Japan Goods", []),
]

# eBay公式カテゴリー トップレベル -> (新親, 新子=デフォルト)
TOP1_DEFAULT = {
    "Toys & Hobbies": ("Toys, Models & Hobby Kits", "Other Toys & Hobbies"),
    "Sporting Goods": ("Sporting Goods", "Other Sporting Goods"),
    "Home & Garden": ("Home, Kitchen & Tools", "Home Improvement & Decor"),
    "Business & Industrial": ("Home, Kitchen & Tools", "Precision & Industrial Tools"),
    "Jewelry & Watches": ("Jewelry & Watches", "Fashion & Fine Jewelry"),
    "Cameras & Photo": ("Cameras & Photography", "Camera Accessories"),
    "Music": ("Music, Movies & Media", "Other Music Media"),
    "Movies & TV": ("Music, Movies & Media", "DVD & Blu-ray"),
    "Musical Instruments & Gear": ("Musical Instruments & Pro Audio", "Other Instruments & Accessories"),
    "Consumer Electronics": ("Consumer Electronics & Computers", "Other Electronics"),
    "Computers/Tablets & Networking": ("Consumer Electronics & Computers", "Computers & Networking"),
    "Video Games & Consoles": ("Video Games & Consoles", "Accessories & Parts"),
    "Health & Beauty": ("Health & Beauty", "Health Care & Wellness"),
    "Collectibles": ("Collectibles & Anime Merchandise", "Comics, Pens & Memorabilia"),
    "Clothing, Shoes & Accessories": ("Clothing & Craft Supplies", "Clothing & Accessories"),
    "Crafts": ("Clothing & Craft Supplies", "Craft Supplies"),
    "Cell Phones & Accessories": ("Consumer Electronics & Computers", "Cell Phone Accessories"),
}

# (トップレベル, セカンドレベル) -> (新親, 新子)  個別指定。無ければTOP1_DEFAULTにフォールバック
TOP2_OVERRIDE = {
    ("Toys & Hobbies", "Models & Kits"): ("Toys, Models & Hobby Kits", "Plastic Models & Kits"),
    ("Toys & Hobbies", "Model Railroads & Trains"): ("Toys, Models & Hobby Kits", "Model Trains & Railroads"),
    ("Toys & Hobbies", "Action Figures & Accessories"): ("Toys, Models & Hobby Kits", "Action Figures & Anime Goods"),
    ("Toys & Hobbies", "Radio Control & Control Line"): ("Toys, Models & Hobby Kits", "RC Vehicles & Parts"),
    ("Toys & Hobbies", "Collectible Card Games"): ("Toys, Models & Hobby Kits", "Trading Cards & CCG"),
    ("Toys & Hobbies", "Diecast & Toy Vehicles"): ("Toys, Models & Hobby Kits", "Diecast & Toy Vehicles"),
    ("Toys & Hobbies", "Puzzles"): ("Toys, Models & Hobby Kits", "Puzzles & Games"),
    ("Toys & Hobbies", "Games"): ("Toys, Models & Hobby Kits", "Puzzles & Games"),
    ("Toys & Hobbies", "Educational"): ("Toys, Models & Hobby Kits", "Puzzles & Games"),
    ("Collectibles", "Animation Art & Merchandise"): ("Toys, Models & Hobby Kits", "Action Figures & Anime Goods"),
    ("Collectibles", "Non-Sport Trading Cards"): ("Toys, Models & Hobby Kits", "Trading Cards & CCG"),

    ("Sporting Goods", "Fishing"): ("Sporting Goods", "Fishing Gear & Tackle"),
    ("Sporting Goods", "Camping & Hiking"): ("Sporting Goods", "Camping & Outdoor"),
    ("Sporting Goods", "Golf"): ("Sporting Goods", "Golf"),
    ("Sporting Goods", "Tennis & Racquet Sports"): ("Sporting Goods", "Tennis & Racquet Sports"),
    ("Sporting Goods", "Cycling"): ("Sporting Goods", "Cycling"),

    ("Home & Garden", "Kitchen, Dining & Bar"): ("Home, Kitchen & Tools", "Kitchen & Dining"),
    ("Home & Garden", "Tools & Workshop Equipment"): ("Home, Kitchen & Tools", "Tools & Workshop Equipment"),
    ("Home & Garden", "Yard, Garden & Outdoor Living"): ("Home, Kitchen & Tools", "Yard & Garden"),
    ("Home & Garden", "School Supplies"): ("Home, Kitchen & Tools", "Office & Stationery"),
    ("Business & Industrial", "Office"): ("Home, Kitchen & Tools", "Office & Stationery"),
    ("Business & Industrial", "Restaurant & Food Service"): ("Home, Kitchen & Tools", "Kitchen & Dining"),

    ("Jewelry & Watches", "Watches, Parts & Accessories"): ("Jewelry & Watches", "Watches"),

    ("Cameras & Photo", "Lenses & Filters"): ("Cameras & Photography", "Lenses & Filters"),
    ("Cameras & Photo", "Digital Cameras"): ("Cameras & Photography", "Digital Cameras & Camcorders"),
    ("Cameras & Photo", "Camcorders"): ("Cameras & Photography", "Digital Cameras & Camcorders"),
    ("Cameras & Photo", "Binoculars & Telescopes"): ("Cameras & Photography", "Binoculars, Telescopes & Film"),
    ("Cameras & Photo", "Film Photography"): ("Cameras & Photography", "Binoculars, Telescopes & Film"),

    ("Music", "CDs"): ("Music, Movies & Media", "CDs"),
    ("Music", "Vinyl Records"): ("Music, Movies & Media", "Vinyl Records"),
    ("Movies & TV", "DVDs & Blu-ray Discs"): ("Music, Movies & Media", "DVD & Blu-ray"),
    ("Movies & TV", "VHS Tapes"): ("Music, Movies & Media", "DVD & Blu-ray"),

    ("Musical Instruments & Gear", "Guitars & Basses"): ("Musical Instruments & Pro Audio", "Guitars & Basses"),
    ("Musical Instruments & Gear", "Pianos, Keyboards & Organs"): ("Musical Instruments & Pro Audio", "Keyboards & Pianos"),
    ("Musical Instruments & Gear", "Wind & Woodwind"): ("Musical Instruments & Pro Audio", "Wind, Brass & Percussion"),
    ("Musical Instruments & Gear", "Brass"): ("Musical Instruments & Pro Audio", "Wind, Brass & Percussion"),
    ("Musical Instruments & Gear", "Percussion"): ("Musical Instruments & Pro Audio", "Wind, Brass & Percussion"),
    ("Musical Instruments & Gear", "String"): ("Musical Instruments & Pro Audio", "Wind, Brass & Percussion"),
    ("Musical Instruments & Gear", "DJ Equipment"): ("Musical Instruments & Pro Audio", "DJ & Pro Audio Equipment"),
    ("Musical Instruments & Gear", "Pro Audio Equipment"): ("Musical Instruments & Pro Audio", "DJ & Pro Audio Equipment"),
    ("Musical Instruments & Gear", "Karaoke Entertainment"): ("Musical Instruments & Pro Audio", "DJ & Pro Audio Equipment"),

    ("Consumer Electronics", "TV, Video & Home Audio"): ("Consumer Electronics & Computers", "TV, Video & Home Audio"),
    ("Consumer Electronics", "Portable Audio & Headphones"): ("Consumer Electronics & Computers", "Portable Audio & Headphones"),
    ("Consumer Electronics", "Radio Communication"): ("Consumer Electronics & Computers", "Radio & Vehicle Electronics"),
    ("Consumer Electronics", "Vehicle Electronics & GPS"): ("Consumer Electronics & Computers", "Radio & Vehicle Electronics"),

    ("Video Games & Consoles", "Video Games"): ("Video Games & Consoles", "Video Games"),
    ("Video Games & Consoles", "Video Game Consoles"): ("Video Games & Consoles", "Consoles"),
    ("Video Games & Consoles", "Video Game Accessories"): ("Video Games & Consoles", "Accessories & Parts"),

    ("Health & Beauty", "Shaving & Hair Removal"): ("Health & Beauty", "Shaving & Hair Removal"),
    ("Health & Beauty", "Hair Care & Styling"): ("Health & Beauty", "Hair, Skin & Makeup"),
    ("Health & Beauty", "Skin Care"): ("Health & Beauty", "Hair, Skin & Makeup"),
    ("Health & Beauty", "Makeup"): ("Health & Beauty", "Hair, Skin & Makeup"),
    ("Health & Beauty", "Bath & Body"): ("Health & Beauty", "Hair, Skin & Makeup"),
    ("Health & Beauty", "Nail Care, Manicure & Pedicure"): ("Health & Beauty", "Nail & Oral Care"),
    ("Health & Beauty", "Oral Care"): ("Health & Beauty", "Nail & Oral Care"),
}

FALLBACK_PARENT = "Other Japan Goods"


def map_item(ebay_category_name: str):
    """ '親:子:...' 形式のPrimaryCategoryフルパスから (新親, 新子 or None) を返す"""
    if not ebay_category_name:
        return (FALLBACK_PARENT, None)
    parts = ebay_category_name.split(":")
    top1 = parts[0]
    top2 = parts[1] if len(parts) > 1 else None

    if top2 and (top1, top2) in TOP2_OVERRIDE:
        return TOP2_OVERRIDE[(top1, top2)]
    if top1 in TOP1_DEFAULT:
        return TOP1_DEFAULT[top1]
    return (FALLBACK_PARENT, None)
