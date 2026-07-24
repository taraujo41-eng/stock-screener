def parse_price(item, key):
    lst = item.get(key)
    if lst and isinstance(lst, list) and len(lst) > 0:
        return float(lst[0].get("price", 0))
    return None

import json
item = {'askList': [{'price': '116.55', 'volume': '89'}], 'bidList': [{'price': '111.75', 'volume': '65'}], 'impVol': '3.2317'}
print("Bid:", parse_price(item, "bidList"))
print("Ask:", parse_price(item, "askList"))
print("IV:", float(item.get("impVol", 0)) if item.get("impVol") else None)
