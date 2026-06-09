import time

_cache = {
    "data": None,
    "timestamp": 0
}

TTL = 30 

def get_cache():
    now = time.time()

    if _cache["data"] and (now - _cache["timestamp"] < TTL):
        return _cache["data"]
    
    return None

def set_cache(data):
    _cache["data"] = data
    _cache["timestamp"] = time.time()