"""Small helpers for callback-safe paginated Telegram lists."""
PAGE_SIZE = 8

def page_slice(items, page=0, page_size=PAGE_SIZE):
    if page_size < 1: raise ValueError("page_size must be positive")
    page_count = max(1, (len(items) + page_size - 1) // page_size)
    page = max(0, min(int(page), page_count - 1))
    start = page * page_size
    return items[start:start + page_size], page, page_count
