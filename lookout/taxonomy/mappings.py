"""Taxonomy mapping: assign standardized product types and tags.

Four-strategy approach:
1. Normalize existing Shopify product types via OLD_TYPE_TO_NEW_TYPE
2. Infer type from matched Locally category via leaf extraction
3. Infer type from vendor name via VENDOR_TO_TYPE
4. Infer type from title keywords via TITLE_KEYWORD_RULES (regex)

Also includes:
- Tag generation (gender, activity, season, feature namespaces)
- Google Shopping category mapping
- Weight defaults for shipping estimates
"""

import csv
import re

# --- Excluded vendors (internal use) ---
EXCLUDED_VENDORS = (
    "The Switchback",
    "The Mountain Air",
    "The Mountain Air Back Shop",
    "The Mountain Air Deposits",
)

# ---------------------------------------------------------------------------
# Mapping 1: Locally category leaf name (lowercased) -> controlled product type
# ---------------------------------------------------------------------------
# The Locally category is a hierarchical path like
# "Apparel & Accessories > Footwear > Shoes > Running Shoes".
# We extract the leaf segment ("Running Shoes") and look it up here.
LOCALLY_LEAF_TO_TYPE = {
    # --- Animals & Pet Supplies ---
    "carriers and travel": "Dog Gear",
    "collars & leashes": "Dog Gear",
    "dog packs and accessories": "Dog Gear",
    "pet beds & pads": "Dog Gear",
    "pet bowls": "Dog Gear",
    "pet outdoor gear": "Dog Gear",
    "pet toys": "Dog Gear",
    # --- Apparel: Base Layer ---
    "base layer clothing": "Baselayer Top",
    "base layer bottoms": "Baselayer Bottom",
    "base layer suits": "Baselayer Top",
    "base layer tops": "Baselayer Top",
    "bras": "Underwear",
    "camisoles": "Tank Top",
    "underwear": "Underwear",
    # --- Apparel: Clothing Accessories ---
    "belts": "Belt",
    "fabric care": "Repair Kit",
    "gloves": "Glove",
    "headwear": "Hat",
    "caps/hats/beanies": "Hat",
    "facemasks/balaclavas": "Balaclava",
    "gaiters and scarves": "Neck Gaiter",
    "headbands": "Hat",
    "sun hats": "Hat",
    "visors": "Hat",
    "performance eyewear": "Sunglasses",
    "sunglasses": "Sunglasses",
    # --- Apparel: Clothing Bottoms ---
    "clothing bottoms": "Pants",
    "pants": "Pants",
    "shorts": "Shorts",
    "skirts and skorts": "Skirt",
    # --- Apparel: Clothing Tops ---
    "clothing tops": "Shirt",
    "jackets": "Jacket",
    "long sleeve shirts": "Shirt",
    "short sleeve shirts": "Shirt",
    "sweaters": "Sweater",
    "sweatshirts / hoodies": "Hoodie",
    "tanks & sleeveless tops": "Tank Top",
    # --- Apparel: Cycling Clothing ---
    "cycling - bottoms": "Shorts",
    "cycling - gloves": "Glove",
    "cycling - jerseys/tops": "Shirt",
    # --- Apparel: Dresses ---
    "dresses / jumpers": "Dress",
    "dresses": "Dress",
    # --- Apparel: Fishing Clothing ---
    "fishing clothing": "Shirt",
    "fishing hats & sungaiters": "Hat",
    "fishing ls shirts": "Shirt",
    "fishing pants": "Pants",
    "fishing shoes & sandals": "Sandal",
    "fishing shorts": "Shorts",
    "fishing vests": "Vest",
    "waders": "Pants",
    "wading boots": "Hiking Boot",
    # --- Apparel: Fitness Clothing ---
    "fitness clothing": "Shirt",
    "fitness bottoms": "Shorts",
    "fitness dresses/suits": "Dress",
    "fitness tops": "Shirt",
    "sports bras": "Underwear",
    # --- Apparel: Footwear ---
    "footwear": "Casual Shoe",
    "boots": "Hiking Boot",
    "hiking boots": "Hiking Boot",
    "law enforcement/military": "Hiking Boot",
    "lifestyle boots": "Casual Shoe",
    "mountaineering boots": "Hiking Boot",
    "rain boots": "Casual Shoe",
    "snowsport boots": "Ski Boot",
    "winter boots": "Casual Shoe",
    "work boots": "Casual Shoe",
    "footwear insoles": "Insole",
    "leg gaiters": "Gaiter",
    "sandals": "Sandal",
    "shoes": "Casual Shoe",
    "athletic shoes": "Running Shoe",
    "track & field shoes": "Running Shoe",
    "climbing shoes": "Climbing Shoe",
    "clogs": "Casual Shoe",
    "cycling shoes": "Casual Shoe",
    "hiking shoes": "Hiking Shoe",
    "running shoes": "Running Shoe",
    "sneakers": "Casual Shoe",
    "walking shoes": "Casual Shoe",
    "water shoes": "Sandal",
    "slippers": "Slipper",
    "socks": "Sock",
    "athletic socks": "Sock",
    "casual socks": "Sock",
    "compression socks": "Sock",
    "cycling socks": "Sock",
    "hiking socks": "Sock",
    "running socks": "Sock",
    "snow socks": "Sock",
    # --- Apparel: Outerwear ---
    "outerwear": "Jacket",
    "outerwear bottoms": "Pants",
    "outerwear suits": "Jacket",
    "outerwear tops": "Jacket",
    "rainwear": "Rain Jacket",
    # --- Apparel: Other ---
    "hunting apparel": "Jacket",
    "in water clothing": "Swimwear",
    "swimwear": "Swimwear",
    "boardshorts": "Shorts",
    "swimsuit coverups": "Dress",
    "swimsuits": "Swimwear",
    "infant/baby clothes": "",
    # --- Arts & Entertainment ---
    "stickers & decals": "Sticker",
    "gift card": "Gift Card",
    # --- Bags (ALL CAPS vendor format) ---
    "backpacks": "Backpack",
    "duffle": "Duffel",
    "hip packs": "Daypack",
    "totes": "Travel Bag",
    # --- Bottoms (ALL CAPS vendor format) ---
    "joggers": "Pants",
    "leggings": "Pants",
    "skirts": "Skirt",
    # --- Bras (ALL CAPS vendor format) ---
    "sportsbras": "Underwear",
    # --- Baby & Toddler ---
    "baby carrier": "Backpack",
    # --- Camera ---
    "camera bags & cases": "Travel Bag",
    # --- Electronics ---
    "lights": "Headlamp",
    "flashlights": "Headlamp",
    "headlamps": "Headlamp",
    "lanterns": "Lantern",
    "watches": "Watch",
    # --- Food & Beverage ---
    "drinks": "Food",
    "food": "Food",
    "camping food": "Food",
    # --- Headwear (ALL CAPS vendor format) ---
    "hair": "Hat",
    "hats": "Hat",
    # --- Hardware ---
    "tools": "Multitool",
    "multitools": "Multitool",
    # --- Health & Beauty ---
    "first aid": "First Aid Kit",
    "insect repellent": "Skin Care",
    "makeup and skincare": "Skin Care",
    "soaps and hygiene": "Skin Care",
    # --- Home & Garden / Kitchen ---
    "kitchen & dining": "Camp Cookware",
    "food and beverage containers": "Water Bottle",
    "drink containers": "Water Bottle",
    "food containers": "Camp Cookware",
    "grill and outdoor cooking accessories": "Camp Cookware",
    "outdoor grills": "Camp Stove",
    "tablewear": "Camp Cookware",
    # --- Layers (ALL CAPS vendor format) ---
    "sweatshirts": "Hoodie",
    # --- Luggage & Bags ---
    "bags & packs": "Backpack",
    "day packs / bookbags": "Daypack",
    "dry bags": "Travel Bag",
    "kid carriers & accessories": "Backpack",
    "pack accessories": "",
    "storage bags & boxes": "Travel Bag",
    "stuff sacks": "Travel Bag",
    "tech packs": "Backpack",
    "waist packs": "Daypack",
    "computer & tablet cases": "Travel Bag",
    "luggage": "Luggage",
    "duffel bags": "Duffel",
    "garment bags": "Travel Bag",
    "suitcases": "Luggage",
    "travel gear bags": "Travel Bag",
    "shoulder bags": "Travel Bag",
    "briefcases": "Travel Bag",
    "laptop bags & sleeves": "Travel Bag",
    "messenger bags": "Travel Bag",
    "purses": "Travel Bag",
    "totes / handbags": "Travel Bag",
    "travel packs": "Travel Bag",
    "packing accessories": "Travel Bag",
    "toiletry kits": "Travel Bag",
    "travel organizers": "Travel Bag",
    # --- Media ---
    "books": "Book",
    # --- Outerwear (ALL CAPS vendor format) ---
    # "jackets" and "vests" already covered above
    # --- Socks (ALL CAPS vendor format) ---
    "crew": "Sock",
    "no show": "Sock",
    # --- Sporting Goods: Camping & Hiking ---
    "camp furniture": "Camp Furniture",
    "camp chairs": "Camp Furniture",
    "cots": "Camp Furniture",
    "hammocks": "Camp Furniture",
    "tables": "Camp Furniture",
    "cookware": "Camp Cookware",
    "kitchen accessories": "Camp Cookware",
    "pots & pans": "Camp Cookware",
    "tableware": "Camp Cookware",
    "utensils": "Camp Cookware",
    "stoves & accessories": "Camp Stove",
    "water treatment": "Water Filter",
    "hydration": "Hydration Pack",
    "hands free hydration systems": "Hydration Pack",
    "mattresses": "Sleeping Pad",
    "shelters": "Tent",
    "bivy sacks": "Sleeping Bag",
    "tarps": "Tent",
    "tents": "Tent",
    "sleeping bags": "Sleeping Bag",
    "trekking poles": "Trekking Pole",
    # --- Sporting Goods: Climbing ---
    "climbing": "Climbing Accessory",
    "chalk & chalk bags": "Chalk Bag",
    "climbing accessories": "Climbing Accessory",
    "climbing hardware": "Carabiner",
    "climbing harnesses": "Climbing Harness",
    "climbing helmets": "Climbing Helmet",
    "climbing ropes": "Climbing Rope",
    "climbing trainers & crash pads": "Crash Pad",
    "ice climbing gear": "Climbing Accessory",
    # --- Sporting Goods: Cycling ---
    "cycling water bottles and holders": "Water Bottle",
    # --- Sporting Goods: Running ---
    "protective eyewear": "Sunglasses",
    # --- Sporting Goods: Winter Sports ---
    "poles": "Ski Pole",
    "ski bindings": "Ski Binding",
    "ski boots": "Ski Boot",
    "skis": "Ski",
    "snowsport goggles": "Goggle",
    "snowsport helmets": "Ski Helmet",
    "snowboard bindings": "Snowboard Binding",
    "snowboard boots": "Snowboard Boot",
    "snowboards": "Snowboard",
    "snowshoes": "Snowshoe",
    "snowsport luggage": "Travel Bag",
    # --- Tops (ALL CAPS vendor format) ---
    "long sleeve": "Shirt",
    "short sleeve": "Shirt",
    "sleeveless": "Tank Top",
    # --- Vehicles & Parts ---
    "automotive carriers & racks": "Rack Accessory",
    "bike mounts and carriers": "Bike Rack",
    "cargo boxes & baskets": "Cargo Box",
    "roof racks & accessories": "Roof Rack",
    "truck & van racks": "Roof Rack",
    "trunk & hitch mounts": "Rack Accessory",
    "watersport carriers": "Rack Accessory",
    "winter gear mounts": "Ski Rack",
    "automotive rooftop awnings": "Rack Accessory",
    # --- Water (ALL CAPS vendor format) ---
    "swim tops": "Swimwear",
}

# ---------------------------------------------------------------------------
# Mapping 2: Existing Shopify product type -> normalized controlled type
# ---------------------------------------------------------------------------
OLD_TYPE_TO_NEW_TYPE = {
    # --- Apparel ---
    "Activewear": "Shirt",
    "Balaclavas": "Balaclava",
    "Beanies": "Beanie",
    "Belts": "Belt",
    "CLOTHING": "",
    "Clothing Tops": "Shirt",
    "Coats & Jackets": "Jacket",
    "Dresses": "Dress",
    "Gloves & Mittens": "Glove",
    "Hats": "Hat",
    "Headwear": "Hat",
    "Jackets": "Jacket",
    "Long Johns": "Baselayer Bottom",
    "Neck Gaiters": "Neck Gaiter",
    "Pants": "Pants",
    "Rain Coats": "Rain Jacket",
    "Rain Pants": "Pants",
    "Shirts": "Shirt",
    "Shirts & Tops": "Shirt",
    "Shorts": "Shorts",
    "Snow Pants & Suits": "Snow Pants",
    "Socks": "Sock",
    "Sun Gloves": "Glove",
    "Underwear": "Underwear",
    "Vests": "Vest",
    "Bras": "Underwear",
    "Baby & Toddler Clothing": "",
    "Baby & Toddler Hats": "Hat",
    # --- Footwear ---
    "Shoes": "Casual Shoe",
    "Gaiters": "Gaiter",
    "Insoles & Inserts": "Insole",
    "Shoe Gaiter": "Gaiter",
    "Footbed": "Insole",
    "Shoe Care & Tools": "Gear Care",
    "Shoe Care Kits": "Gear Care",
    "Shoe Treatments & Dyes": "Gear Care",
    "Shoelaces": "Camp Accessory",
    # --- Ski & Snow ---
    "Downhill Skis": "Ski",
    "Skis": "Ski",
    "Ski Bindings": "Ski Binding",
    "Ski Boots": "Ski Boot",
    "Ski Poles": "Ski Pole",
    "Ski & Snowboard Goggles": "Goggle",
    "Ski & Snowboard Helmets": "Ski Helmet",
    "Ski & Snowboard Bags": "Travel Bag",
    "Ski & Snowboard Goggle Accessories": "Eyewear Accessory",
    "Ski & Snowboard Tuning Tools": "Repair Kit",
    "Skiing & Snowboarding": "",
    "Snowboards": "Snowboard",
    "Snowboard Bindings": "Snowboard Binding",
    "Snowboard Binding Parts": "Snowboard Binding",
    "Snowboard Boots": "Snowboard Boot",
    "Snowboard Bag": "Travel Bag",
    "Snowshoes": "Snowshoe",
    "Winter Sports & Activities": "",
    # --- Climbing ---
    "Climbing": "Climbing Accessory",
    "Climbing Ascenders & Descenders": "Climbing Accessory",
    "Climbing Chalk Bags": "Chalk Bag",
    "Climbing Crash Pads": "Crash Pad",
    "Climbing Gloves": "Glove",
    "Climbing Harnesses": "Climbing Harness",
    "Climbing Helmets": "Climbing Helmet",
    "Climbing Protection Devices": "Climbing Accessory",
    "Climbing Rope": "Climbing Rope",
    "Climbing Rope Bags": "Climbing Accessory",
    "Climbing Webbing": "Climbing Accessory",
    "Carabiners": "Carabiner",
    "Belay Devices": "Belay Device",
    "Quickdraws": "Quickdraw",
    "Crampons": "Climbing Accessory",
    "Grip Spray & Chalk": "Climbing Accessory",
    "Ice Climbing Tools": "Climbing Accessory",
    "Indoor Climbing Holds": "Climbing Accessory",
    "Pulleys, Blocks & Sheaves": "Climbing Accessory",
    "Ropes & Hardware Cable": "Climbing Rope",
    # --- Hike & Camp ---
    "Backpack": "Backpack",
    "Backpacks": "Backpack",
    "Backpack Accessory": "Backpack",
    "Backpack Covers": "Backpack",
    "Tents": "Tent",
    "Tent Accessories": "Tent Accessory",
    "Tent Footprints": "Tent Accessory",
    "Tent Poles & Stakes": "Tent Accessory",
    "Tent Vestibules": "Tent Accessory",
    "Tarps": "Tent",
    "Sleeping Bags": "Sleeping Bag",
    "Sleeping Bag Liners": "Sleeping Bag",
    "Sleeping Pads": "Sleeping Pad",
    "Air Mattress & Sleeping Pad Accessories": "Sleeping Pad",
    "Portable Cooking Stoves": "Camp Stove",
    "Portable Cooking Stove Accessories": "Camp Stove",
    "Camping Cookware & Dinnerware": "Camp Cookware",
    "Cookware": "Camp Cookware",
    "Camp Furniture": "Camp Furniture",
    "Folding Chairs & Stools": "Camp Furniture",
    "Headlamps": "Headlamp",
    "Camping Lights & Lanterns": "Lantern",
    "Hiking Poles": "Trekking Pole",
    "Hiking Pole Accessories": "Trekking Pole",
    "Water Bottles": "Water Bottle",
    "Hydration Systems": "Hydration Pack",
    "Hydration System Accessories": "Hydration Pack",
    "Water Filters": "Water Filter",
    "Portable Water Filters & Purifiers": "Water Filter",
    "Camping Tools": "Multitool",
    "Camping & Hiking": "",
    "Compression Sacks": "Travel Bag",
    "Hammocks": "Camp Furniture",
    "Hammock Parts & Accessories": "Camp Furniture",
    "Pillows": "Camp Pillow",
    # --- Racks & Travel ---
    "Motor Vehicle Carrying Rack Accessories": "Rack Accessory",
    "Motor Vehicle Carrying Racks": "Rack Accessory",
    "Motor Vehicle Cargo Nets": "Rack Accessory",
    "Vehicle Base Rack Systems": "Roof Rack",
    "Vehicle Bicycle Racks": "Bike Rack",
    "Vehicle Bicycle Rack Accessories": "Bike Rack",
    "Vehicle Boat Racks": "Rack Accessory",
    "Vehicle Cargo Racks": "Cargo Box",
    "Vehicle Ski & Snowboard Racks": "Ski Rack",
    "Vehicle Ski & Snowboard Rack Accessories": "Ski Rack",
    "Vehicle Water Sport Board Racks": "Rack Accessory",
    "Locks & Keys": "Rack Accessory",
    "Tie Down Straps": "Rack Accessory",
    "Duffel Bags": "Duffel",
    "Fanny Packs": "Daypack",
    "Luggage & Bags": "Travel Bag",
    "Luggage Covers": "Travel Bag",
    "Luggage Tags": "Travel Bag",
    "Suitcases": "Luggage",
    "Cosmetic & Toiletry Bags": "Travel Bag",
    "Travel Bottles & Containers": "Travel Bag",
    "Travel Pouches": "Travel Bag",
    # --- Accessories ---
    "Sunglasses": "Sunglasses",
    "Sunglasses Retainers": "Eyewear Accessory",
    "Rings": "Jewelry",
    "Jewelry": "Jewelry",
    "Wallets & Money Clips": "Travel Accessory",
    "Keychains": "Camp Accessory",
    # --- Other ---
    "Dog Supplies": "Dog Gear",
    "Dog Apparel": "Dog Gear",
    "Dog Beds": "Dog Gear",
    "Dog Toys": "Dog Gear",
    "Pet Collars & Harnesses": "Dog Gear",
    "Pet First Aid & Emergency Kits": "Dog Gear",
    "Pet Supplies": "Dog Gear",
    "Decorative Stickers": "Sticker",
    "Books": "Book",
    "Print Books": "Book",
    "Notebooks & Notepads": "Book",
    "Maps": "Map",
    "Food Items": "Food",
    "Trail & Snack Mixes": "Food",
    "Toasted Marshmallow Magic": "Food",
    "Seasonings & Spices": "Food",
    "Fabric Repair Kits": "Repair Kit",
    "Multifunction Tools & Knives": "Multitool",
    "Utility Knives": "Multitool",
    "Hunting & Survival Knives": "Multitool",
    "Axes": "Multitool",
    "Skin Care": "Skin Care",
    "Skin Insect Repellent": "Skin Care",
    "Sunscreen": "Skin Care",
    "Lip Balms": "Skin Care",
    "Foot Care": "Skin Care",
    "Deodorant & Anti-Perspirant": "Skin Care",
    "Lotion & Moisturizer": "Skin Care",
    "Bar Soap": "Skin Care",
    "Liquid Hand Soap": "Skin Care",
    "Hand Sanitizers & Wipes": "Skin Care",
    "Adult Hygienic Wipes": "Skin Care",
    "Hygienic Wipes": "Skin Care",
    "Personal Care": "Skin Care",
    "First Aid": "First Aid Kit",
    "First Aid Kits": "First Aid Kit",
    "Medical Tape & Bandages": "First Aid Kit",
    "Emergency Blankets": "First Aid Kit",
    "Emergency Preparedness": "First Aid Kit",
    "Chemical Hand Warmers": "Hand Warmer",
    "Baby Carriers": "Backpack",
    "Drinkware": "Water Bottle",
    "Mugs": "Water Bottle",
    "Flasks": "Water Bottle",
    "Thermoses": "Water Bottle",
    "Food & Beverage Carriers": "Water Bottle",
    "Replacement Drink Lids": "Water Bottle",
    "Bottle Openers": "Multitool",
    "Bowls": "Camp Cookware",
    "Coffee Filters": "Camp Cookware",
    "Coffee Maker & Espresso Machine Accessories": "Camp Cookware",
    "Coffee Makers & Espresso Machines": "Camp Cookware",
    "Electric & Stovetop Espresso Pots": "Camp Cookware",
    "French Presses": "Camp Cookware",
    "Kitchen Tools & Utensils": "Camp Cookware",
    "Percolators": "Camp Cookware",
    "Navigational Compasses": "Compass",
    "Binoculars": "Binoculars",
    "Travel Pillows": "Travel Accessory",
    "Greeting & Note Cards": "Gift",
    "Sport & Safety Whistles": "Camp Accessory",
    "Fabric & Upholstery Protectors": "Repair Kit",
    "Utility Buckles": "Camp Accessory",
    "Gear Ties": "Camp Accessory",
    "Hooks, Buckles & Fasteners": "Camp Accessory",
    "Bungee Cords": "Camp Accessory",
    "Light Ropes & Strings": "Lantern",
    "Storage & Organization": "Travel Accessory",
    "Propane": "Camp Stove",
    "Fuel": "Camp Stove",
    "Fuel Containers & Tanks": "Camp Stove",
    "Firewood & Fuel": "Fire Starter",
    "Clear Kerosene": "Camp Stove",
    "Lighters & Matches": "Fire Starter",
    "Batteries": "Electronics",
    "Power Adapters & Chargers": "Electronics",
    "Solar Panels": "Electronics",
    "GPS Navigation Systems": "Electronics",
    "GPS Tracking Devices": "Electronics",
    "Satellite Phones": "Electronics",
    "Headphones": "Electronics",
    "Flying Discs": "Toy",
    "Dice Sets & Games": "Toy",
    "Toys": "Toy",
    "Towels": "Camp Accessory",
    "Parasols & Rain Umbrellas": "Camp Accessory",
    "Portable Showers & Privacy Enclosures": "Camp Accessory",
    "Portable Toilets & Showers": "Camp Accessory",
    "Portable Toilets & Urination Devices": "Camp Accessory",
    "Toilet Paper": "Camp Accessory",
    "Earplugs": "Travel Accessory",
    "Eye Pillows": "Travel Accessory",
    "Mosquito Nets & Insect Screens": "Camp Accessory",
    "Craft Fasteners & Closures": "Camp Accessory",
    "Wine Carrier Bags": "Gift",
    "Bicycle Child Seat Accessories": "",
    "Bicycle Protective Pads": "Camp Accessory",
    "Boating & Water Sports": "",
    "Musical Instruments": "Toy",
    "String Instruments": "Toy",
    "Surf Leashes": "",
    "Outdoor Recreation": "",
    "Coolers": "Camp Cookware",
    "Shovels & Spades": "Multitool",
    "Handwarmer": "Hand Warmer",
    "Sample": "",
    "Used": "",
    "Travel Converters & Adapters": "Travel Accessory",
    # --- Lowercase variants (data entry variations) ---
    "Running Shoes": "Running Shoe",
    "Trail Running Shoes": "Trail Runner",
    "bean": "Beanie",
    "climbing": "Climbing Accessory",
    "flashlight": "Headlamp",
    "gloves": "Glove",
    "hats": "Hat",
    "hiking pole": "Trekking Pole",
    "jackets": "Jacket",
    "socks": "Sock",
}

# ---------------------------------------------------------------------------
# Mapping 3: Vendor -> product type (vendors with homogeneous catalogs)
# ---------------------------------------------------------------------------
VENDOR_TO_TYPE = {
    "Amandalee Design": "Sticker",
    "Sticker Art": "Sticker",
    "Keep Nature Wild": "Sticker",
    "Backpacker's Pantry": "Food",
    "Foundation Outdoors": "Food",
    "Mountain House": "Food",
    "Peak Refuel": "Food",
    "CLIF Bar": "Food",
    "Righteous Felon": "Food",
    "Trail Butter": "Food",
    "Gatorade": "Food",
    "National Geographic Maps": "Map",
    "Sierra Maps": "Map",
    "Tom Harrison": "Map",
    "The Mountaineers Books": "Book",
    "Wilderness Press": "Book",
    "Bloomsbury Publishing": "Book",
    "Ingram": "Book",
    "California Native Plant Society": "Book",
    "Field Notes": "Book",
    # Gifts & novelty
    "Kala": "Toy",
    "PlanToys": "Toy",
    "World Footbag": "Toy",
    "Abbott": "Toy",
    "Heyday Books": "Gift",
    # Jewelry
    "Bronwen": "Jewelry",
    "Groove Life": "Jewelry",
    # Eyewear
    "Goodr": "Sunglasses",
    # Gear care
    "Nikwax": "Gear Care",
    "Grangers": "Gear Care",
    "Jason Markk": "Gear Care",
    # Snow accessories
    "Crab Grab": "Snowboard Accessory",
    # Navigation
    "Suunto": "Compass",
    "Brunton": "Compass",
    "Nocs Provisions": "Binoculars",
    # Warmers
    "Ignik Outdoors": "Hand Warmer",
    # Camp gear
    "BearVault": "Camp Accessory",
    "Chicken Tramper Ultralight Gear": "Camp Accessory",
    # Footwear
    "Blundstone": "Casual Shoe",
    "Birkenstock": "Sandal",
    # Clothing basics
    "Exofficio": "Underwear",
    "Smartwool": "Sock",
    "Solmate": "Sock",
    "Mons Royale": "Baselayer Top",
    "COAL": "Beanie",
    "Sunday Afternoons": "Hat",
    # Skin care
    "Wildland Protection": "Skin Care",
    "Tecnu": "Skin Care",
    "Adventure Medical Kits": "First Aid Kit",
    # Hydration & cook
    "Nalgene": "Water Bottle",
    "Sawyer": "Water Filter",
    "CNOC Outdoors": "Water Bottle",
    "Jetboil": "Camp Stove",
    "GSI Outdoors": "Camp Cookware",
    # Electronics
    "Goal Zero": "Electronics",
    "BioLite": "Electronics",
    # Eyewear accessories
    "Chums": "Eyewear Accessory",
    "Ombraz": "Eyewear Accessory",
    # Travel
    "humangear": "Travel Accessory",
    # Climbing
    "Metolius": "Climbing Accessory",
    # Dog gear
    "Ruffwear": "Dog Gear",
    # Footwear/snow brands
    "32": "Snowboard Boot",
    "LEKI": "Trekking Pole",
    "K2 Sports": "Ski Pole",
    "YETI": "Water Bottle",
    "Klean Kanteen": "Camp Cookware",
}

# ---------------------------------------------------------------------------
# Mapping 4: Title keyword rules (ordered, first match wins)
# Evaluated against "vendor + ' ' + title" (case-insensitive)
# ---------------------------------------------------------------------------
TITLE_KEYWORD_RULES = [
    # --- Ski & Snow (specific before generic) ---
    (r"\bsnowboard\s*boot", "Snowboard Boot"),
    (r"\bski\s*boot", "Ski Boot"),
    (r"\bsnowboard\s*binding", "Snowboard Binding"),
    (r"\bski\s*binding", "Ski Binding"),
    (r"\bsnowboard\b(?!.*\b(?:boot|binding|bag|rack))", "Snowboard"),
    (r"\bski\s*pole", "Ski Pole"),
    (r"\bski\s*bag|ski\s*roller", "Travel Bag"),
    (r"\bskis\b", "Ski"),
    (r"\b(?:snow\s*)?goggle", "Goggle"),
    (r"\b(?:snowsport|snow\s*sport|mips)\s*helmet", "Ski Helmet"),
    (r"\bsnowshoe", "Snowshoe"),
    (r"\bstomp\b", "Snowboard Accessory"),
    (r"\btorque\s*driver\b|\bbullet\s*tool\b", "Snowboard Accessory"),
    (r"\bw/?\s*(?:griffon|squire|shift|pivot|marker|look|tyrolia)\s*binding\b", "Ski"),
    (r"\btour\s*pole\b", "Ski Pole"),
    # --- Outerwear ---
    (r"\brain\s*(?:jacket|coat|shell)", "Rain Jacket"),
    (r"\brainsuit", "Rain Jacket"),
    (r"\binsulated\s*bib\b", "Snow Pants"),
    (r"\bsnow\s*pant", "Snow Pants"),
    (r"\bptx\b", "Rain Jacket"),
    (r"\b(?:insulated\s*)?(?:jacket|parka|anorak|shell)\b", "Jacket"),
    (r"\b(?:insulated\s*)?vest\b", "Vest"),
    (r"\bfleece\s*(?:pullover|shirt|1/2|quarter|half)", "Fleece"),
    (r"\bhoodx?ie?\b|\bhoody\b|\bhoodie\b", "Hoodie"),
    (r"\bfleece\b", "Fleece"),
    (r"\bsweater\b", "Sweater"),
    # --- Tops ---
    (r"\bt-shirt\b|\btee\b", "T-Shirt"),
    (r"\btank\b", "Tank Top"),
    (r"\b(?:flannel|button.?up|oxford|l/?s\b.*shirt|long\s*sleeve)", "Shirt"),
    (r"\b(?:shirt|henley)\b", "Shirt"),
    (r"\bsnapshirt\b|\bshortsleeve\b|\btwill\b", "Shirt"),
    (r"\bmicro\s*d\b", "Fleece"),
    (r"\bpull-?on\b|\bpullover\b", "Fleece"),
    (r"\b(?:1/[24]|quarter|half)\s*zip\b", "Fleece"),
    # --- Bottoms ---
    (r"\bbib\b", "Snow Pants"),
    (r"\b(?:jogger|chino|pants?|sweatpants?|wideleg)\b", "Pants"),
    (r"\bshorts?\b(?!.*sleeve)", "Shorts"),
    (r"\blegging", "Pants"),
    (r"\bdress\b", "Dress"),
    (r"\bskirt\b", "Skirt"),
    # --- Footwear (specific before generic) ---
    (r"\bcloudsurfer\b|\bcloudvista\b|\bcloudventure\b|\bcaldera\b|\bprodigio\b", "Trail Runner"),
    (r"\bcloudrock\b|\boutway\b", "Hiking Shoe"),
    (r"\btrail\s*run", "Trail Runner"),
    (r"\brunning\s*shoe", "Running Shoe"),
    (r"\bhiking\s*boot", "Hiking Boot"),
    (r"\bhiking\s*shoe", "Hiking Shoe"),
    (r"\bapproach\s*shoe", "Approach Shoe"),
    (r"\bclimbing\s*shoe", "Climbing Shoe"),
    (r"\bsandal", "Sandal"),
    (r"\bslipper", "Slipper"),
    (r"\bboot\b", "Casual Shoe"),
    (r"\bsock\b|\bsocks\b", "Sock"),
    # --- Headwear ---
    (r"\bbeanies?\b", "Beanie"),
    (r"\b(?:clava|balaclava)\b", "Balaclava"),
    (r"\b(?:hat|cap|trucker|snapback|brim)\b", "Hat"),
    (r"\bbuff\b.*\bcoolnet", "Neck Gaiter"),
    (r"\bneck\s*gaiter\b|\btube\b", "Neck Gaiter"),
    (r"\bbandana", "Neck Gaiter"),
    # --- Accessories ---
    (r"\bgloves?\b|\bmittens?\b", "Glove"),
    (r"\bsunglasses\b|\bsunglass\b", "Sunglasses"),
    (r"\bretainer\b", "Eyewear Accessory"),
    (r"\b(?:necklace|earring|bracelet|charm)\b", "Jewelry"),
    (r"\bbelt\b", "Belt"),
    # --- Hike & Camp ---
    (r"\bbackpack\b", "Backpack"),
    (r"\bdaypack\b|\bday\s*pack\b", "Daypack"),
    (r"\b(?:talon|daybreak)\s*\d+\b", "Daypack"),
    (r"\bkid\s*comfort\b", "Backpack"),
    (r"\bduffel\b|\bduffle\b", "Duffel"),
    (r"\b(?:stakes?|guy\s*line|shock\s*cord|zipper\s*pulls?)\b", "Tent Accessory"),
    (r"\btent\b", "Tent"),
    (r"\bsleeping\s*(?:bag|quilt)\b", "Sleeping Bag"),
    (r"\bsleeping\s*pad\b", "Sleeping Pad"),
    (r"\bpillow\b", "Camp Pillow"),
    (r"\bcamp\s*stove\b|\bstove\b", "Camp Stove"),
    (
        r"\b(?:pot|pan|cookware|tableware|bowl|plate|spork|pint\s*cups?|forks?|food\s*box)\b",
        "Camp Cookware",
    ),
    (r"\bcamp\s*(?:chair|furniture)\b|\badirondack\b|\b(?:chair|stool)\b", "Camp Furniture"),
    (r"\bheadlamp\b", "Headlamp"),
    (r"\bflashlight\b", "Headlamp"),
    (r"\blantern\b|\bluci\b", "Lantern"),
    (r"\b(?:compass)\b", "Compass"),
    (r"\bbinocular", "Binoculars"),
    (r"\btrekking\s*pole", "Trekking Pole"),
    (r"\bwater\s*(?:bottle|container|carrier)\b|\bbota\b|\bhydroflask\b|\bflask\b", "Water Bottle"),
    (r"\bwater\s*filter\b|\bsqueeze\b.*\bsystem\b|\bquickhose\b", "Water Filter"),
    (r"\b(?:bite\s*valve|valve\s*(?:cover|sheath)|quick\s*connect\s*kit)\b", "Hydration Pack"),
    (r"\bhydration\s*pack\b", "Hydration Pack"),
    (r"\bfirst\s*aid\b", "First Aid Kit"),
    (r"\btick\s*(?:key|buddy|removal)\b|\bafter\s*bite\b", "First Aid Kit"),
    (r"\b(?:fire\s*start\w*|magnesium\s*fire\w*|stormproof\s*match)\b", "Fire Starter"),
    (r"\b(?:match\s*(?:kit|case)|long\s*burn\s*match)\b", "Fire Starter"),
    (r"\btinder\b|\bfirestarter\b", "Fire Starter"),
    (r"\blighter\b", "Fire Starter"),
    (r"\bsweetfire\b", "Fire Starter"),
    (r"\bnoctilight\b", "Lantern"),
    (r"\bheadband\s*replacement\b", "Headlamp"),
    (r"\bwind\s*screen\b", "Camp Stove"),
    (r"\bfuel\s*stabilizer\b", "Camp Stove"),
    (r"\belectric\s*pump\b", "Sleeping Pad"),
    # --- Camp accessories ---
    (r"\bbutt\s*napkins?\b|\bwag\s*bag\b|\bwaste\s*kit\b", "Camp Accessory"),
    (r"\bumbrell", "Camp Accessory"),
    (r"\bwhistle\b", "Camp Accessory"),
    (r"\bmosquito\b", "Camp Accessory"),
    (r"\btowel\b", "Camp Accessory"),
    (r"\bparacord\b", "Camp Accessory"),
    (r"\bbuckle\b", "Camp Accessory"),
    # --- Climbing ---
    (r"\bpetzl\b.*\b(?:\d+\.\d+|contact|arial|volta)\b", "Climbing Rope"),
    (r"\bpetzl\b.*\b(?:connect|evolv)\b", "Climbing Accessory"),
    (r"\bniteline\b", "Climbing Rope"),
    (r"\b(?:ice\s*axe|crampon|traction\s*device)\b", "Climbing Accessory"),
    (r"\bclimbing\s*rope\b|\bstatic\s*rope\b", "Climbing Rope"),
    (r"\bclimbing\s*harness\b|\bharness\b", "Climbing Harness"),
    (r"\bchalk\s*(?:bag|bucket)\b", "Chalk Bag"),
    (r"\bcrash\s*pad\b", "Crash Pad"),
    (r"\b(?:auto\s*lock|screw\s*lock)\b", "Carabiner"),
    (r"\bcarabiner\b", "Carabiner"),
    (r"\bhelmet\b", "Ski Helmet"),
    # --- Racks ---
    (r"\broof\s*rack\b", "Roof Rack"),
    (r"\bbike\s*rack\b", "Bike Rack"),
    (r"\bski\s*rack\b", "Ski Rack"),
    (r"\bcargo\s*box\b", "Cargo Box"),
    # --- Other ---
    (r"\bsticker\b|\bpatch\b", "Sticker"),
    (r"\b(?:book|field\s*guide|trail\s*guide)\b", "Book"),
    (r"\bnote\s*card\b|\bgreeting\s*card\b", "Gift"),
    (r"\bmap\b", "Map"),
    (
        r"\b(?:meal|granola|chili|cobbler|enchilada|bolognese|pasta|noodle|stew|risotto|skillet|pancake|oatmeal|energy\s*(?:gel|chew|bar))\b",
        "Food",
    ),
    (r"\bmapleaid\b|\buntapped\b.*\bgels?\b", "Food"),
    (r"\bgift\s*card\b", "Gift Card"),
    (r"\brepair\s*kit\b", "Repair Kit"),
    (r"\b(?:tenacious\s*tape|repair\s*tape)\b", "Repair Kit"),
    (r"\bmulti-?tool\b|\bknife\b|\btrowel\b|\bshovel\b", "Multitool"),
    (r"\b(?:dog|collar|leash)\b.*\b(?:gear|toy|collar|leash|bowl)\b", "Dog Gear"),
    (r"\binsect\s*repell", "Skin Care"),
    (r"\bsunscreen\b", "Skin Care"),
    (r"\bsoap\b", "Skin Care"),
    (r"\btoothbrush\b|\btoothpaste\b", "Skin Care"),
    # --- Gear care ---
    (r"\b(?:sno\s*seal|aquaseal|revivex|seam\s*grip)\b", "Gear Care"),
    (r"\b(?:waterproof\w*\s*(?:spray|wax|treatment|sealant)|water\s*repel\w*|dwr)\b", "Gear Care"),
    (
        r"\b(?:down\s*wash|tech\s*wash|merino\s*wash|wool\s*wash|fabric\s*(?:wash|clean))\b",
        "Gear Care",
    ),
    (
        r"\b(?:shoe\s*(?:care|clean|repair|treat)|cleaning\s*gel|boot\s*cream|renovating\s*cream)\b",
        "Gear Care",
    ),
    (r"\bgreenland\s*wax\b|\bsolarproof\b", "Gear Care"),
    (r"\bzipper\s*(?:clean|lubric)", "Gear Care"),
    (r"\bwax\b.*\b(?:fabric|leather)\b|\b(?:fabric|leather)\b.*\bwax\b", "Gear Care"),
    # --- Hand warmers ---
    (r"\b(?:hand|toe|foot)\s*warm", "Hand Warmer"),
    (r"\bheatbank\b", "Hand Warmer"),
    # --- Electronics ---
    (r"\b(?:inreach|gps|eTrex|fenix)\b", "Electronics"),
    (r"\bsolar\s*panel\b", "Electronics"),
    (r"\b(?:power\s*bank|(?:usb|wall)\s*charger|recharg\w+\s*battery)\b", "Electronics"),
    # --- Travel accessories ---
    (r"\bwallet\b", "Travel Accessory"),
    (r"\b(?:card\s*holder|money\s*clip)\b", "Travel Accessory"),
    (r"\btsa\b", "Travel Accessory"),
    (r"\btoiletry\b", "Travel Accessory"),
    (r"\btravel\s*(?:adapter|bottles?|cubes?)\b", "Travel Accessory"),
    (r"\brfid\b", "Travel Accessory"),
    (r"\bsleep\s*mask\b", "Travel Accessory"),
    (r"\bbi-?fold\b", "Travel Accessory"),
    (r"\bgo\s*to(?:ob|ub)\b", "Travel Accessory"),
    (r"\bbidet\b", "Travel Accessory"),
    (r"\bbetalock\b", "Travel Accessory"),
    (r"\bgreenland\s*pocket\b", "Travel Accessory"),
    (r"\bpacking\b.*\bbundle\b|\bpacking\s*cube\b", "Travel Accessory"),
    # --- Travel / bags ---
    (r"\bnylon\s*shoulder\s*bag\b", "Travel Bag"),
    (r"\bsling\b|\bcrossbody\b|\bpouch\b|\btote\b", "Travel Bag"),
    (r"\bhip\s*pack\b|\bfanny\s*pack\b", "Daypack"),
    (r"\btravel\s*backpack\b", "Backpack"),
    # --- Thule keys ---
    (r"\bthule\b.*\bkey\b", "Rack Accessory"),
    # --- Coolnet / Buff products ---
    (r"\bcoolnet\b|\bbuff\b", "Neck Gaiter"),
    # --- Compression / running socks ---
    (r"\bcompression\b.*\bsock", "Sock"),
    # --- Visor / sleeves (sun protection) ---
    (r"\bvisor\b", "Hat"),
    # --- Toys & gifts ---
    (r"\bukulele\b|\bukelele\b", "Toy"),
    (r"\b(?:frisbee|footbag|flying\s*disc|ultimate\s*disc)\b", "Toy"),
    (r"\bdice\b", "Toy"),
    (r"\bmicrosend\b|\btiny\s*tents?\b", "Toy"),
    (r"\b(?:ornament|decor)\b", "Gift"),
    (r"\bkoozie\b", "Gift"),
    # --- Misc catches ---
    (r"\b(?:leash|surf\s*leash)\b", "Dog Gear"),
    (r"\bcord\s*lock\b", "Camp Accessory"),
    (r"\b(?:aquatainer|water\s*jug)\b", "Water Bottle"),
    (r"\b(?:elbow|knee)\s*pad\b", "Camp Accessory"),
    (r"\bpack\s*cover\b", "Camp Accessory"),
    (r"\bsuspender\b", "Belt"),
    (r"\b(?:re-ties|zip\s*ties?)\b", "Camp Accessory"),
    (r"\b(?:scrubber|cleansing\s*wipe|pet\s*wipe)\b", "Skin Care"),
    (r"\b(?:pressure|camp|pocket)\s*shower\b", "Camp Accessory"),
    (r"\bphone\s*case\b", "Electronics"),
    (r"\binsulator\b|\bstorage\s*bag\b|\bstuff\s*sack\b", "Camp Accessory"),
    (r"\bstrap\s*set\b", "Camp Accessory"),
    (r"\brambler\b|\bhotshot\b", "Water Bottle"),
    (r"\blaces?\b", "Camp Accessory"),
    (r"\bcrew\b", "Shirt"),
]

# ---------------------------------------------------------------------------
# Gender mapping: Locally gender string -> gender tag
# ---------------------------------------------------------------------------
GENDER_TO_TAG = {
    "Men": "gender:mens",
    "men": "gender:mens",
    "men's": "gender:mens",
    "mens": "gender:mens",
    "male": "gender:mens",
    "Women": "gender:womens",
    "women": "gender:womens",
    "women's": "gender:womens",
    "womens": "gender:womens",
    "female": "gender:womens",
    "Unisex": "gender:unisex",
    "unisex": "gender:unisex",
    "Kids": "gender:kids",
    "kids": "gender:kids",
    "kid's": "gender:kids",
    "Boys": "gender:kids",
    "boys": "gender:kids",
    "Girls": "gender:kids",
    "girls": "gender:kids",
}

# ---------------------------------------------------------------------------
# Activity inference: path substring -> activity tag (first match wins)
# ---------------------------------------------------------------------------
ACTIVITY_PATH_RULES = [
    ("snowboard", "activity:snowboarding"),
    ("ski equipment", "activity:skiing"),
    ("snowsport", "activity:skiing"),
    ("winter sports", "activity:skiing"),
    ("climbing shoe", "activity:climbing"),
    ("climbing", "activity:climbing"),
    ("running shoe", "activity:running"),
    ("running", "activity:running"),
    ("hiking boot", "activity:hiking"),
    ("hiking shoe", "activity:hiking"),
    ("trekking", "activity:hiking"),
    ("camping", "activity:camping"),
    ("cycling", "activity:cycling"),
    ("fishing", "activity:fishing"),
    ("boating", "activity:water-sports"),
    ("water sports", "activity:water-sports"),
    ("swimwear", "activity:swimming"),
    ("fitness", "activity:fitness"),
]

# ---------------------------------------------------------------------------
# Season inference from product type
# ---------------------------------------------------------------------------
WINTER_TYPES = {
    "Snow Pants",
    "Ski",
    "Ski Boot",
    "Ski Binding",
    "Ski Pole",
    "Snowboard",
    "Snowboard Boot",
    "Snowboard Binding",
    "Goggle",
    "Ski Helmet",
    "Snowshoe",
    "Balaclava",
    "Beanie",
    "Ski Rack",
    "Ski Jacket",
    "Snow Jacket",
    "Snowboard Accessory",
    "Hand Warmer",
}

SUMMER_TYPES = {
    "Sandal",
    "Swimwear",
    "Tank Top",
}

# ---------------------------------------------------------------------------
# Softgoods: product types that receive gender tags
# Only apparel, footwear, and body-interface gear (gendered sizing exists)
# ---------------------------------------------------------------------------
SOFTGOODS_TYPES = {
    # Apparel
    "Jacket",
    "Rain Jacket",
    "Fleece",
    "Hoodie",
    "Sweater",
    "Vest",
    "Shirt",
    "T-Shirt",
    "Tank Top",
    "Pants",
    "Shorts",
    "Snow Pants",
    "Dress",
    "Skirt",
    "Baselayer Top",
    "Baselayer Bottom",
    "Underwear",
    "Swimwear",
    "Beanie",
    "Balaclava",
    "Hat",
    "Neck Gaiter",
    "Glove",
    "Sock",
    "Belt",
    # Footwear
    "Hiking Boot",
    "Hiking Shoe",
    "Running Shoe",
    "Trail Runner",
    "Approach Shoe",
    "Climbing Shoe",
    "Casual Shoe",
    "Sandal",
    "Slipper",
    "Ski Boot",
    "Snowboard Boot",
    "Insole",
    "Gaiter",
    # Body-interface gear (gendered sizing)
    "Climbing Harness",
    "Backpack",
    "Daypack",
    "Sleeping Bag",
    "Hydration Pack",
}

# ---------------------------------------------------------------------------
# Activity inference from product type (fallback when no Locally category)
# Maps product type -> list of activity tags
# ---------------------------------------------------------------------------
ACTIVITY_FROM_TYPE = {
    # Camping
    "Tent": ["activity:camping"],
    "Sleeping Bag": ["activity:camping"],
    "Sleeping Pad": ["activity:camping"],
    "Camp Stove": ["activity:camping"],
    "Camp Cookware": ["activity:camping"],
    "Camp Furniture": ["activity:camping"],
    "Lantern": ["activity:camping"],
    "Water Filter": ["activity:camping"],
    # Hiking + Camping
    "Headlamp": ["activity:hiking", "activity:camping"],
    "Trekking Pole": ["activity:hiking", "activity:camping"],
    # Hiking
    "Hiking Boot": ["activity:hiking"],
    "Hiking Shoe": ["activity:hiking"],
    # Running
    "Running Shoe": ["activity:running"],
    "Trail Runner": ["activity:running"],
    # Climbing
    "Climbing Shoe": ["activity:climbing"],
    "Climbing Harness": ["activity:climbing"],
    "Climbing Rope": ["activity:climbing"],
    "Climbing Helmet": ["activity:climbing"],
    "Carabiner": ["activity:climbing"],
    "Belay Device": ["activity:climbing"],
    "Quickdraw": ["activity:climbing"],
    "Chalk Bag": ["activity:climbing"],
    "Crash Pad": ["activity:climbing"],
    "Climbing Accessory": ["activity:climbing"],
    # Skiing
    "Ski": ["activity:skiing"],
    "Ski Boot": ["activity:skiing"],
    "Ski Binding": ["activity:skiing"],
    "Ski Pole": ["activity:skiing"],
    "Goggle": ["activity:skiing"],
    "Ski Helmet": ["activity:skiing"],
    "Ski Rack": ["activity:skiing"],
    # Snowboarding
    "Snowboard": ["activity:snowboarding"],
    "Snowboard Boot": ["activity:snowboarding"],
    "Snowboard Binding": ["activity:snowboarding"],
    "Snowshoe": ["activity:snowboarding"],
    "Snowboard Accessory": ["activity:snowboarding"],
    # Travel
    "Duffel": ["activity:travel"],
    "Luggage": ["activity:travel"],
    "Travel Bag": ["activity:travel"],
    # Cycling
    "Bike Rack": ["activity:cycling"],
    # Camping
    "Tent Accessory": ["activity:camping"],
    "Camp Pillow": ["activity:camping"],
    "Camp Accessory": ["activity:camping"],
    "Fire Starter": ["activity:camping"],
    # Hiking
    "Compass": ["activity:hiking"],
    "Binoculars": ["activity:hiking"],
}


# ---------------------------------------------------------------------------
# Type-group inference: maps product types to a collection-grouping tag.
# Enables Shopify smart collections to use (Tag=type-group:X AND Tag=gender:Y)
# since Shopify can't express (Type=A OR Type=B) AND Tag=Y natively.
# Only needed for product types that share a gendered collection.
# ---------------------------------------------------------------------------
TYPE_TO_GROUP = {
    # Jackets & Outerwear
    "Jacket": "type-group:jacket",
    "Rain Jacket": "type-group:jacket",
    "Ski Jacket": "type-group:jacket",
    "Snow Jacket": "type-group:jacket",
    # Tops
    "Shirt": "type-group:top",
    "T-Shirt": "type-group:top",
    "Tank Top": "type-group:top",
    "Hoodie": "type-group:top",
    "Sweater": "type-group:top",
    # Bottoms
    "Pants": "type-group:bottom",
    "Shorts": "type-group:bottom",
    "Snow Pants": "type-group:bottom",
}


# ---------------------------------------------------------------------------
# Feature inference from title keywords
# ---------------------------------------------------------------------------
FEATURE_FROM_TITLE = [
    (r"\b(?:waterproof|gore-?tex|gtx|h2no|wp)\b", "feature:waterproof"),
    (r"\b(?:insulated|primaloft|thermoplume|thermogreen)\b", "feature:insulated"),
    (r"\b(?:ultralight|\bul\b)\b", "feature:ultralight"),
    (r"\bpackable\b", "feature:packable"),
    (r"\brecycled\b", "feature:recycled"),
    (r"\b(?:windproof|wind\s*resistant|pertex)\b", "feature:windproof"),
    (r"\bdown\b", "feature:down"),
]

# Types where "down" in title means down insulation (not a direction)
_DOWN_CONTEXT_TYPES = {
    "Jacket",
    "Vest",
    "Hoodie",
    "Sleeping Bag",
    "Sweater",
    "Parka",
}


# ---------------------------------------------------------------------------
# Google Shopping: Product type -> Google Product Category
# (from generate_google_shopping.py)
# ---------------------------------------------------------------------------
PRODUCT_TYPE_TO_GOOGLE_CATEGORY = {
    # Apparel -- Outerwear
    "Jacket": "Apparel & Accessories > Clothing > Outerwear > Coats & Jackets",
    "Rain Jacket": "Apparel & Accessories > Clothing > Outerwear > Coats & Jackets",
    "Vest": "Apparel & Accessories > Clothing > Outerwear > Vests",
    "Fleece": "Apparel & Accessories > Clothing > Outerwear > Coats & Jackets",
    # Apparel -- Tops
    "Shirt": "Apparel & Accessories > Clothing > Shirts & Tops",
    "T-Shirt": "Apparel & Accessories > Clothing > Shirts & Tops",
    "Tank Top": "Apparel & Accessories > Clothing > Shirts & Tops",
    "Hoodie": "Apparel & Accessories > Clothing > Activewear > Sweatshirts",
    "Sweater": "Apparel & Accessories > Clothing > Shirts & Tops",
    "Baselayer Top": "Apparel & Accessories > Clothing > Underwear & Socks > Thermal Underwear",
    # Apparel -- Bottoms
    "Pants": "Apparel & Accessories > Clothing > Pants",
    "Snow Pants": "Apparel & Accessories > Clothing > Pants",
    "Shorts": "Apparel & Accessories > Clothing > Shorts",
    "Skirt": "Apparel & Accessories > Clothing > Skirts",
    "Dress": "Apparel & Accessories > Clothing > Dresses",
    "Baselayer Bottom": "Apparel & Accessories > Clothing > Underwear & Socks > Thermal Underwear",
    # Apparel -- Other
    "Underwear": "Apparel & Accessories > Clothing > Underwear & Socks > Underwear",
    "Swimwear": "Apparel & Accessories > Clothing > Swimwear",
    # Apparel -- Accessories
    "Hat": "Apparel & Accessories > Clothing Accessories > Hats",
    "Beanie": "Apparel & Accessories > Clothing Accessories > Hats",
    "Beanies": "Apparel & Accessories > Clothing Accessories > Hats",
    "Glove": "Apparel & Accessories > Clothing Accessories > Gloves & Mittens",
    "Gloves & Mittens": "Apparel & Accessories > Clothing Accessories > Gloves & Mittens",
    "Neck Gaiter": "Apparel & Accessories > Clothing Accessories > Scarves & Shawls",
    "Balaclava": "Apparel & Accessories > Clothing Accessories > Balaclavas",
    "Gaiter": "Apparel & Accessories > Clothing Accessories > Leg Warmers",
    "Belt": "Apparel & Accessories > Clothing Accessories > Belts",
    "Sunglasses": "Apparel & Accessories > Clothing Accessories > Sunglasses",
    "Eyewear Accessory": "Apparel & Accessories > Clothing Accessories > Eyewear Accessories",
    "Sock": "Apparel & Accessories > Clothing > Underwear & Socks > Socks",
    "Insole": "Apparel & Accessories > Shoe Accessories > Insoles & Inserts",
    # Footwear
    "Casual Shoe": "Apparel & Accessories > Shoes",
    "Running Shoe": "Apparel & Accessories > Shoes > Athletic Shoes",
    "Trail Runner": "Apparel & Accessories > Shoes > Athletic Shoes",
    "Hiking Shoe": "Apparel & Accessories > Shoes > Athletic Shoes",
    "Hiking Boot": "Apparel & Accessories > Shoes > Boots",
    "Climbing Shoe": "Apparel & Accessories > Shoes > Athletic Shoes",
    "Ski Boot": "Apparel & Accessories > Shoes > Boots",
    "Snowboard Boot": "Apparel & Accessories > Shoes > Boots",
    "Sandal": "Apparel & Accessories > Shoes > Sandals",
    "Slipper": "Apparel & Accessories > Shoes > Slippers",
    "Snowshoe": "Sporting Goods > Outdoor Recreation > Winter Sports & Activities > Snowshoeing > Snowshoes",
    # Bags & Packs
    "Backpack": "Sporting Goods > Outdoor Recreation > Camping & Hiking > Backpacks",
    "Daypack": "Sporting Goods > Outdoor Recreation > Camping & Hiking > Backpacks",
    "Hydration Pack": "Sporting Goods > Outdoor Recreation > Camping & Hiking > Hydration Packs",
    "Travel Bag": "Luggage & Bags > Travel Bags",
    "Duffel": "Luggage & Bags > Duffel Bags",
    "Luggage": "Luggage & Bags > Luggage",
    "Chalk Bag": "Sporting Goods > Outdoor Recreation > Climbing > Climbing Chalk Bags",
    # Camping & Hiking
    "Tent": "Sporting Goods > Outdoor Recreation > Camping & Hiking > Tents",
    "Tent Accessory": "Sporting Goods > Outdoor Recreation > Camping & Hiking > Tent Accessories",
    "Sleeping Bag": "Sporting Goods > Outdoor Recreation > Camping & Hiking > Sleeping Bags",
    "Sleeping Pad": "Sporting Goods > Outdoor Recreation > Camping & Hiking > Sleeping Pads",
    "Camp Pillow": "Sporting Goods > Outdoor Recreation > Camping & Hiking > Camping Pillows",
    "Camp Furniture": "Sporting Goods > Outdoor Recreation > Camping & Hiking > Camp Furniture",
    "Camp Stove": "Sporting Goods > Outdoor Recreation > Camping & Hiking > Camp Stoves",
    "Camp Cookware": "Sporting Goods > Outdoor Recreation > Camping & Hiking > Camp Cookware",
    "Camp Accessory": "Sporting Goods > Outdoor Recreation > Camping & Hiking",
    "Headlamp": "Sporting Goods > Outdoor Recreation > Camping & Hiking > Camping Lights & Lanterns",
    "Lantern": "Sporting Goods > Outdoor Recreation > Camping & Hiking > Camping Lights & Lanterns",
    "Water Filter": "Sporting Goods > Outdoor Recreation > Camping & Hiking > Water Filters & Purifiers",
    "Water Bottle": "Kitchen & Dining > Drinkware > Water Bottles",
    "Fire Starter": "Sporting Goods > Outdoor Recreation > Camping & Hiking > Fire Starters",
    "Compass": "Sporting Goods > Outdoor Recreation > Camping & Hiking > Compasses",
    "First Aid Kit": "Health & Beauty > Health Care > First Aid",
    "Repair Kit": "Sporting Goods > Outdoor Recreation > Camping & Hiking",
    # Climbing
    "Climbing Accessory": "Sporting Goods > Outdoor Recreation > Climbing",
    "Climbing Harness": "Sporting Goods > Outdoor Recreation > Climbing > Climbing Harnesses",
    "Climbing Rope": "Sporting Goods > Outdoor Recreation > Climbing > Climbing Ropes",
    "Climbing Helmet": "Sporting Goods > Outdoor Recreation > Climbing > Climbing Helmets",
    "Carabiner": "Sporting Goods > Outdoor Recreation > Climbing > Carabiners",
    "Belay Device": "Sporting Goods > Outdoor Recreation > Climbing > Belay Devices",
    "Crash Pad": "Sporting Goods > Outdoor Recreation > Climbing > Crash Pads",
    "Quickdraw": "Sporting Goods > Outdoor Recreation > Climbing > Quickdraws",
    # Winter Sports
    "Ski": "Sporting Goods > Outdoor Recreation > Winter Sports & Activities > Skiing > Skis",
    "Ski Binding": "Sporting Goods > Outdoor Recreation > Winter Sports & Activities > Skiing > Ski Bindings",
    "Ski Pole": "Sporting Goods > Outdoor Recreation > Winter Sports & Activities > Skiing > Ski Poles",
    "Ski Helmet": "Sporting Goods > Outdoor Recreation > Winter Sports & Activities > Skiing > Ski Helmets",
    "Ski Rack": "Vehicles & Parts > Vehicle Parts & Accessories > Vehicle Racks",
    "Goggle": "Sporting Goods > Outdoor Recreation > Winter Sports & Activities > Skiing > Ski Goggles",
    "Snowboard": "Sporting Goods > Outdoor Recreation > Winter Sports & Activities > Snowboarding > Snowboards",
    "Snowboard Binding": "Sporting Goods > Outdoor Recreation > Winter Sports & Activities > Snowboarding > Snowboard Bindings",
    "Snowboard Accessory": "Sporting Goods > Outdoor Recreation > Winter Sports & Activities > Snowboarding",
    "Winter Sports & Activities": "Sporting Goods > Outdoor Recreation > Winter Sports & Activities",
    # Trekking
    "Trekking Pole": "Sporting Goods > Outdoor Recreation > Camping & Hiking > Trekking Poles",
    # Vehicle
    "Roof Rack": "Vehicles & Parts > Vehicle Parts & Accessories > Vehicle Racks",
    "Rack Accessory": "Vehicles & Parts > Vehicle Parts & Accessories > Vehicle Rack Accessories",
    "Bike Rack": "Vehicles & Parts > Vehicle Parts & Accessories > Vehicle Racks > Vehicle Bike Racks",
    "Cargo Box": "Vehicles & Parts > Vehicle Parts & Accessories > Vehicle Racks > Vehicle Cargo Racks",
    # Other gear
    "Multitool": "Tools & Hardware > Hand Tools > Multifunction Tools & Knives",
    "Binoculars": "Cameras & Optics > Optics > Binoculars",
    "Monoculars": "Cameras & Optics > Optics > Monoculars",
    "Electronics": "Electronics",
    "Dog Gear": "Animals & Pet Supplies > Pet Supplies > Dog Supplies",
    "Hand Warmer": "Sporting Goods > Outdoor Recreation > Camping & Hiking",
    "Gear Care": "Sporting Goods > Outdoor Recreation > Camping & Hiking",
    "Skin Care": "Health & Beauty > Personal Care > Cosmetics > Skin Care",
    "Travel Accessory": "Luggage & Bags > Luggage Accessories",
    "Toy": "Toys & Games",
    "Jewelry": "Apparel & Accessories > Jewelry",
    # Media
    "Book": "Media > Books",
    "Map": "Media > Books > Maps & Atlases",
    # Misc
    "Sticker": "Arts & Entertainment > Crafts > Stickers",
    "Decorative Stickers": "Arts & Entertainment > Crafts > Stickers",
    "Food": "Food, Beverages & Tobacco > Food Items",
    "Food Items": "Food, Beverages & Tobacco > Food Items",
    "Gift": "Arts & Entertainment > Party & Celebration > Gift Giving",
    "Bicycle Child Seat Accessories": "Vehicles & Parts > Vehicle Parts & Accessories",
    # Plural/variant spellings of canonical types
    "Hats": "Apparel & Accessories > Clothing Accessories > Hats",
    "Shirts": "Apparel & Accessories > Clothing > Shirts & Tops",
    "Shirts & Tops": "Apparel & Accessories > Clothing > Shirts & Tops",
    "CLOTHING": "Apparel & Accessories > Clothing",
    "Shoes": "Apparel & Accessories > Shoes",
    "Ski Poles": "Sporting Goods > Outdoor Recreation > Winter Sports & Activities > Skiing > Ski Poles",
    "Ski & Snowboard Bags": "Luggage & Bags > Sports Bags",
    "Skiing & Snowboarding": "Sporting Goods > Outdoor Recreation > Winter Sports & Activities",
    "Trekking Poles": "Sporting Goods > Outdoor Recreation > Camping & Hiking > Trekking Poles",
    "Tents": "Sporting Goods > Outdoor Recreation > Camping & Hiking > Tents",
    "Tent Footprints": "Sporting Goods > Outdoor Recreation > Camping & Hiking > Tent Accessories",
    "Backpacks": "Sporting Goods > Outdoor Recreation > Camping & Hiking > Backpacks",
    "Neck Gaiters": "Apparel & Accessories > Clothing Accessories > Scarves & Shawls",
    "Mugs": "Kitchen & Dining > Drinkware",
    "Zipper Pulls": "Sporting Goods > Outdoor Recreation > Camping & Hiking",
    "Drinking Games": "Toys & Games",
    "Books": "Media > Books",
    "Print Books": "Media > Books",
    "Toys": "Toys & Games",
    "Brooches & Lapel Pins": "Apparel & Accessories > Jewelry",
    "General Purpose Batteries": "Electronics > Electronics Accessories > Power > Batteries",
    "Power Adapters & Chargers": "Electronics > Electronics Accessories > Power",
    "Bumper Sticker": "Arts & Entertainment > Crafts > Stickers",
    "Greeting & Note Cards": "Arts & Entertainment > Party & Celebration > Greeting & Note Cards",
    "Kitchen Tools & Utensils": "Kitchen & Dining > Kitchen Tools & Utensils",
    "Camping Cookware & Dinnerware": "Sporting Goods > Outdoor Recreation > Camping & Hiking > Camp Cookware",
    "Water Bottles": "Kitchen & Dining > Drinkware > Water Bottles",
    "Skis": "Sporting Goods > Outdoor Recreation > Winter Sports & Activities > Skiing > Skis",
    "Ski & Snowboard Goggle Accessories": "Sporting Goods > Outdoor Recreation > Winter Sports & Activities",
    "Vehicle Cargo Racks": "Vehicles & Parts > Vehicle Parts & Accessories > Vehicle Racks > Vehicle Cargo Racks",
    "Fireplace Tool": "Home & Garden > Heating, Cooling & Air > Fireplaces & Accessories",
}

# ---------------------------------------------------------------------------
# Google Shopping: Gender / age_group mapping
# (from generate_google_shopping.py)
# ---------------------------------------------------------------------------
TAG_TO_GOOGLE_GENDER = {
    "gender:mens": "male",
    "gender:womens": "female",
    "gender:unisex": "unisex",
    "gender:kids": "unisex",  # Google uses age_group for the kids distinction
}

TAG_TO_GOOGLE_AGE_GROUP = {
    "gender:kids": "kids",
    # everything else defaults to "adult"
}

# Title keyword fallback for products without gender tags
TITLE_GENDER_KEYWORDS = {
    "male": [r"\bMen'?s\b", r"\bMen\b"],
    "female": [r"\bWomen'?s\b", r"\bWomen\b", r"\bWmn'?s?\b"],
    "kids": [r"\bKids?\b", r"\bBoys?\b", r"\bGirls?\b", r"\bYouth\b", r"\bJr\.?\b", r"\bJunior\b"],
}

# ---------------------------------------------------------------------------
# Google Shopping: Size system detection
# (from generate_google_shopping.py)
# ---------------------------------------------------------------------------
EU_SIZING_VENDORS = {"La Sportiva", "SCARPA", "Birkenstock"}

MONDO_TYPES = {"Ski Boot"}

# ---------------------------------------------------------------------------
# Google Shopping: Season tag map (tags -> custom label)
# (from generate_google_shopping.py)
# ---------------------------------------------------------------------------
SEASON_TAG_MAP = {
    "season:spring-summer": "spring-summer",
    "season:spring": "spring-summer",
    "season:summer": "spring-summer",
    "season:fall-winter": "fall-winter",
    "season:fall": "fall-winter",
    "season:winter": "fall-winter",
    "season:year-round": "year-round",
}

# ---------------------------------------------------------------------------
# Google Shopping: Price tiers (custom label)
# (from generate_google_shopping.py)
# ---------------------------------------------------------------------------
PRICE_TIERS = [
    (50, "under-50"),
    (100, "50-100"),
    (200, "100-200"),
]

# ---------------------------------------------------------------------------
# Google Shopping: Weight defaults (lbs, converted to grams for output)
# Reasonable type-based defaults for products lacking weight data.
# (from generate_google_shopping.py)
# ---------------------------------------------------------------------------
PRODUCT_TYPE_WEIGHT_DEFAULTS_LB = {
    # Apparel
    "Jacket": 1.5,
    "Rain Jacket": 0.8,
    "Vest": 0.8,
    "Fleece": 1.0,
    "Shirt": 0.6,
    "T-Shirt": 0.4,
    "Tank Top": 0.3,
    "Hoodie": 1.2,
    "Sweater": 1.0,
    "Baselayer Top": 0.5,
    "Pants": 1.2,
    "Snow Pants": 2.0,
    "Shorts": 0.5,
    "Skirt": 0.5,
    "Dress": 0.6,
    "Baselayer Bottom": 0.5,
    "Underwear": 0.2,
    "Swimwear": 0.3,
    # Accessories
    "Hat": 0.3,
    "Beanie": 0.2,
    "Beanies": 0.2,
    "Glove": 0.4,
    "Gloves & Mittens": 0.4,
    "Neck Gaiter": 0.2,
    "Balaclava": 0.2,
    "Gaiter": 0.3,
    "Belt": 0.3,
    "Sock": 0.2,
    "Insole": 0.2,
    # Eyewear
    "Sunglasses": 0.1,
    "Eyewear Accessory": 0.1,
    "Goggle": 0.5,
    # Footwear
    "Casual Shoe": 1.5,
    "Running Shoe": 1.2,
    "Trail Runner": 1.2,
    "Hiking Shoe": 1.8,
    "Hiking Boot": 2.5,
    "Climbing Shoe": 1.5,
    "Ski Boot": 8.0,
    "Snowboard Boot": 6.0,
    "Sandal": 0.8,
    "Slipper": 0.5,
    "Snowshoe": 4.0,
    # Bags
    "Backpack": 4.0,
    "Daypack": 1.5,
    "Hydration Pack": 1.0,
    "Travel Bag": 2.0,
    "Duffel": 2.0,
    "Luggage": 8.0,
    "Chalk Bag": 0.3,
    # Camping
    "Tent": 5.0,
    "Tent Accessory": 0.5,
    "Sleeping Bag": 3.0,
    "Sleeping Pad": 1.5,
    "Camp Pillow": 0.5,
    "Camp Furniture": 5.0,
    "Camp Stove": 1.5,
    "Camp Cookware": 1.0,
    "Camp Accessory": 0.5,
    "Headlamp": 0.3,
    "Lantern": 0.5,
    "Water Filter": 0.5,
    "Water Bottle": 0.8,
    "Fire Starter": 0.2,
    "Compass": 0.2,
    "First Aid Kit": 0.5,
    "Repair Kit": 0.3,
    # Climbing
    "Climbing Accessory": 0.3,
    "Climbing Harness": 1.0,
    "Climbing Rope": 5.0,
    "Climbing Helmet": 0.8,
    "Carabiner": 0.2,
    "Belay Device": 0.5,
    "Crash Pad": 12.0,
    "Quickdraw": 0.2,
    # Winter
    "Ski": 8.0,
    "Ski Binding": 4.0,
    "Ski Pole": 1.0,
    "Ski Helmet": 1.2,
    "Ski Rack": 5.0,
    "Snowboard": 7.0,
    "Snowboard Binding": 3.0,
    "Snowboard Accessory": 0.5,
    "Trekking Pole": 1.0,
    # Vehicle
    "Roof Rack": 10.0,
    "Rack Accessory": 3.0,
    "Bike Rack": 15.0,
    "Cargo Box": 45.0,
    # Other
    "Multitool": 0.3,
    "Binoculars": 1.5,
    "Monoculars": 0.5,
    "Electronics": 0.5,
    "Dog Gear": 0.5,
    "Hand Warmer": 0.2,
    "Gear Care": 0.3,
    "Skin Care": 0.3,
    "Travel Accessory": 0.3,
    "Toy": 0.3,
    "Jewelry": 0.1,
    "Book": 0.8,
    "Map": 0.2,
    "Sticker": 0.05,
    "Food": 0.5,
    "Gift": 0.3,
}

# Dimensional/bulky types where wrong weight = lost shipping money
DIMENSIONAL_TYPES = {
    "Roof Rack",
    "Rack Accessory",
    "Cargo Box",
    "Bike Rack",
    "Ski",
    "Snowboard",
    "Ski Boot",
    "Snowboard Boot",
    "Tent",
    "Sleeping Bag",
    "Backpack",
    "Duffel",
    "Travel Bag",
    "Ski Pole",
    "Trekking Pole",
    "Crash Pad",
    "Luggage",
    "Camp Furniture",
    "Climbing Rope",
    "Snowshoe",
}

LB_TO_GRAMS = 453.592


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def get_type_from_locally_category(category) -> str:
    """Extract product type from a Locally category path using leaf matching."""
    if not category:
        return ""
    cat_lower = category.lower().strip()
    # Extract the leaf (last segment after " > ")
    leaf = cat_lower.rsplit(" > ", 1)[-1].strip()
    return LOCALLY_LEAF_TO_TYPE.get(leaf, "")


def get_type_from_title_keywords(vendor, title) -> str:
    """Match vendor+title against keyword rules. Returns first match or ''."""
    text = f"{vendor} {title}".lower()
    for pattern, product_type in TITLE_KEYWORD_RULES:
        if re.search(pattern, text, re.IGNORECASE):
            return product_type
    return ""


def get_gender_from_title(title) -> str:
    """Infer gender tag from title patterns."""
    t = (title or "").lower()
    if re.search(r"\b(?:women'?s|wmns|woman'?s|girls)\b", t):
        return "gender:womens"
    if re.search(r"\b(?:men'?s|mens)\b", t):
        return "gender:mens"
    if re.search(r"\b(?:kids'?|kid'?s|boys|youth|toddler|junior|baby)\b", t):
        return "gender:kids"
    return ""


def get_activity_from_category(category) -> str:
    """Infer activity tag from Locally category path segments."""
    if not category:
        return ""
    cat_lower = category.lower()
    for substring, tag in ACTIVITY_PATH_RULES:
        if substring in cat_lower:
            return tag
    return ""


def get_season_from_type(product_type) -> str:
    """Infer season tag from the assigned product type."""
    if product_type in WINTER_TYPES:
        return "season:winter"
    if product_type in SUMMER_TYPES:
        return "season:summer"
    return ""


def get_features_from_title(title, product_type="") -> list[str]:
    """Infer feature tags from title keywords. Returns list of matching tags."""
    t = (title or "").lower()
    features = []
    for pattern, tag in FEATURE_FROM_TITLE:
        if tag == "feature:down" and product_type not in _DOWN_CONTEXT_TYPES:
            continue
        if re.search(pattern, t, re.IGNORECASE):
            features.append(tag)
    return features


def get_activity_from_type(product_type) -> list[str]:
    """Infer activity tags from product type. Returns list of activity tags."""
    return ACTIVITY_FROM_TYPE.get(product_type, [])


def assign_taxonomy(current_type, category, vendor, title, gender="") -> dict:
    """Assign product type and tags using the 4-strategy priority approach.

    This is the core taxonomy engine. It determines the product type and
    generates appropriate tags for a single product.

    Args:
        current_type: Existing Shopify product_type (may be empty)
        category: Locally category path (may be empty)
        vendor: Product vendor name
        title: Product title
        gender: Locally gender string (may be empty)

    Returns:
        dict with keys:
            new_type: Assigned product type (controlled vocabulary)
            new_tags: Comma-separated string of generated tags
    """
    current_type = (current_type or "").strip()
    category = (category or "").strip()
    vendor = (vendor or "").strip()
    title = (title or "").strip()

    # --- Assign product type (priority order) ---
    new_type = ""

    # Priority 1: Normalize existing type
    if current_type:
        new_type = OLD_TYPE_TO_NEW_TYPE.get(current_type, current_type)

    # Priority 2: Locally category leaf
    if not new_type and category:
        new_type = get_type_from_locally_category(category)

    # Priority 3: Vendor-based inference
    if not new_type and vendor in VENDOR_TO_TYPE:
        new_type = VENDOR_TO_TYPE[vendor]

    # Priority 4: Title keyword matching
    if not new_type:
        new_type = get_type_from_title_keywords(vendor, title)

    # --- Assign tags ---
    new_tags_parts = []

    # Activity: prefer Locally category path, fall back to type
    activity_tag = get_activity_from_category(category)
    if activity_tag:
        new_tags_parts.append(activity_tag)
    else:
        activity_tags = get_activity_from_type(new_type)
        new_tags_parts.extend(activity_tags)

    # Gender: title signal always wins (explicit "Women's Snowboard"),
    # Locally gender only applied for softgoods (avoids "unisex" on tents).
    # Ungendered softgoods default to men's -- store convention: products
    # without gender in the name are typically men's (XL/XXL sizing, etc.)
    title_gender = get_gender_from_title(title)
    if title_gender:
        new_tags_parts.append(title_gender)
    elif new_type in SOFTGOODS_TYPES:
        locally_gender = GENDER_TO_TAG.get(gender, "")
        if locally_gender:
            new_tags_parts.append(locally_gender)
        else:
            new_tags_parts.append("gender:mens")

    # Season: from product type
    season_tag = get_season_from_type(new_type)
    if season_tag:
        new_tags_parts.append(season_tag)

    # Feature tags from title
    feature_tags = get_features_from_title(title, new_type)
    new_tags_parts.extend(feature_tags)

    # Type-group tag for multi-type gendered collections
    group_tag = TYPE_TO_GROUP.get(new_type)
    if group_tag:
        new_tags_parts.append(group_tag)

    new_tags = ", ".join(new_tags_parts)

    return {"new_type": new_type, "new_tags": new_tags}


def generate_mapping(conn, output_path) -> dict:
    """Generate a taxonomy mapping CSV for all retail products.

    Uses four-strategy priority for type assignment:
    1. Normalize existing Shopify product_type via OLD_TYPE_TO_NEW_TYPE
    2. Infer from matched Locally category (leaf extraction)
    3. Vendor-based inference via VENDOR_TO_TYPE
    4. Title keyword inference via TITLE_KEYWORD_RULES
    5. Leave blank for manual review

    Writes CSV with columns: handle, title, vendor, current_type, current_tags,
    locally_category, new_type, new_tags.

    Returns:
        dict with key 'products_mapped' indicating total rows written.
    """
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT DISTINCT
            sp.handle,
            sp.title,
            sp.vendor,
            sp.product_type,
            sp.tags,
            NULL AS category,
            NULL AS gender
        FROM shopify_products sp
        WHERE sp.vendor NOT IN (?, ?, ?, ?)
        ORDER BY sp.handle
    """,
        EXCLUDED_VENDORS,
    )
    rows = cursor.fetchall()

    # Deduplicate by handle (take first match with a category if available)
    seen_handles = {}
    for row in rows:
        handle = row["handle"]
        if handle not in seen_handles:
            seen_handles[handle] = row
        else:
            existing = seen_handles[handle]
            if not existing["category"] and row["category"]:
                seen_handles[handle] = row

    fieldnames = [
        "handle",
        "title",
        "vendor",
        "current_type",
        "current_tags",
        "locally_category",
        "new_type",
        "new_tags",
    ]

    products_mapped = 0
    with open(output_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()

        for handle in sorted(seen_handles.keys()):
            row = seen_handles[handle]
            current_type = (row["product_type"] or "").strip()
            category = (row["category"] or "").strip()
            gender = (row["gender"] or "").strip()
            vendor = (row["vendor"] or "").strip()
            title = (row["title"] or "").strip()

            result = assign_taxonomy(current_type, category, vendor, title, gender)

            writer.writerow(
                {
                    "handle": handle,
                    "title": title,
                    "vendor": vendor,
                    "current_type": current_type,
                    "current_tags": row["tags"] or "",
                    "locally_category": category,
                    "new_type": result["new_type"],
                    "new_tags": result["new_tags"],
                }
            )
            products_mapped += 1

    return {"products_mapped": products_mapped}


def apply_mapping(conn, mapping_path, replace_tags=False) -> dict:
    """Apply a taxonomy mapping CSV to the update queue.

    Reads the mapping CSV. For each row:
    - If new_type differs from current_type, queues a product_type update.
    - If new_tags exist, queues a tags update.

    Args:
        conn: Database connection
        mapping_path: Path to the mapping CSV file
        replace_tags: If True, new_value is set to ONLY the new namespaced tags
            (no merge with old). If False (default), merges old + new tags.

    Source is set to 'taxonomy_map'.

    Returns:
        dict with key 'updates_queued' indicating total updates inserted.
    """
    updates_queued = 0
    cursor = conn.cursor()
    cursor.execute("DELETE FROM update_queue WHERE source = 'taxonomy_map'")
    conn.commit()

    with open(mapping_path, newline="") as f:
        reader = csv.DictReader(f)

        for row in reader:
            handle = row["handle"]
            current_type = row.get("current_type", "")
            new_type = row.get("new_type", "")
            current_tags = row.get("current_tags", "")
            new_tags_str = row.get("new_tags", "")

            # Queue product_type update if new_type differs
            if new_type and new_type != current_type:
                cursor.execute(
                    """
                    INSERT INTO update_queue
                        (shopify_handle, field, old_value, new_value, source, status)
                    VALUES (?, 'product_type', ?, ?, 'taxonomy_map', 'pending')
                """,
                    (handle, current_type, new_type),
                )
                updates_queued += 1

            # Queue tags update if new_tags exist
            if new_tags_str:
                if replace_tags:
                    final_tags_str = new_tags_str
                else:
                    existing_tags = (
                        [t.strip() for t in current_tags.split(",") if t.strip()]
                        if current_tags
                        else []
                    )
                    new_tag_list = [t.strip() for t in new_tags_str.split(",") if t.strip()]

                    merged = list(existing_tags)
                    for tag in new_tag_list:
                        if tag not in merged:
                            merged.append(tag)

                    final_tags_str = ", ".join(merged)

                if final_tags_str != current_tags:
                    cursor.execute(
                        """
                        INSERT INTO update_queue
                            (shopify_handle, field, old_value, new_value, source, status)
                        VALUES (?, 'tags', ?, ?, 'taxonomy_map', 'pending')
                    """,
                        (handle, current_tags, final_tags_str),
                    )
                    updates_queued += 1

    conn.commit()
    return {"updates_queued": updates_queued}


# ---------------------------------------------------------------------------
# Merchandising Configuration
# ---------------------------------------------------------------------------

EXCLUDED_VENDORS = (
    "The Switchback",
    "The Mountain Air",
    "The Mountain Air Back Shop",
    "The Mountain Air Deposits",
)

MERCH_WEIGHTS = {
    "sales_velocity": 0.35,
    "margin": 0.20,
    "inventory_health": 0.20,
    "new_arrival_boost": 0.15,
    "low_inventory_penalty": 0.10,
}

NEW_ARRIVAL_DAYS = 30
LOW_INVENTORY_THRESHOLD = 3

LOCATIONS = {
    "The Mountain Air": {"id": 44797132845, "active": True},
    "The Switchback": {"id": 71628587255, "active": True},
}
