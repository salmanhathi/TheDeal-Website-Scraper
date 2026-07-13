import re
import json
import asyncio
import aiohttp
import urllib3
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE_URL = "https://www.thedealoutlet.com/ae-en/{}.html"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
}

PLACEHOLDER_SIGNALS = [
    'noimagelarge', 'noimage', 'no_image',
    '/dw58870029/', 'blank.gif', '1x1', 'pixel',
]

CONCURRENCY  = 80
TIMEOUT_SEC  = 15
RETRY_STATUSES = {'Timeout', 'Connection Error', 'Error', 'HTTP 429', 'HTTP 500', 'HTTP 409'}
MAX_RETRIES    = 4
RETRY_DELAYS   = [5, 15, 30, 60]


# ── Helpers ───────────────────────────────────────────────────────────────────

def pad_product_id(pid):
    pid_str = str(pid).strip()
    if '.' in pid_str:
        pid_str = pid_str.split('.')[0]
    pid_str = re.sub(r'[^0-9]', '', pid_str)
    return pid_str.zfill(12) if pid_str else '000000000000'

def is_placeholder(src):
    return any(p in (src or '').lower() for p in PLACEHOLDER_SIGNALS)

def make_session():
    import requests
    from requests.adapters import HTTPAdapter
    s = requests.Session()
    s.mount('https://', HTTPAdapter(pool_connections=10, pool_maxsize=20))
    s.headers.update(HEADERS)
    return s


# ── Extractors ────────────────────────────────────────────────────────────────

def extract_title(soup):
    og = soup.find('meta', property='og:title')
    if og and og.get('content'):
        return og['content'].strip()
    h1 = soup.find('h1')
    if h1:
        return h1.get_text(strip=True)
    return soup.title.string.strip() if soup.title else None


def extract_brand(soup):
    for sel in ['[itemprop="brand"] [itemprop="name"]', '[itemprop="brand"]', '.product-brand']:
        el = soup.select_one(sel)
        if el:
            b = el.get_text(strip=True)
            if b:
                return b
    title_str = soup.title.string.strip() if soup.title else ''
    og = soup.find('meta', property='og:title')
    og_title = og['content'].strip() if og and og.get('content') else ''
    m = re.match(r'^Buy (.+?) for AED', title_str)
    if m and og_title:
        full = m.group(1).strip()
        words_full = full.split()
        words_og = og_title.split()
        for i in range(len(words_full)):
            if words_full[i:i + len(words_og)] == words_og:
                brand = ' '.join(words_full[:i]).strip()
                if brand:
                    return brand
    return None


def extract_prices(soup):
    result = {"sale_price": None, "original_price": None}
    sale_el = soup.select_one('h2.sales span.value, .sales span.value')
    if sale_el:
        result["sale_price"] = sale_el.get('content') or re.sub(r'[^\d.]', '', sale_el.get_text())
    orig_el = soup.select_one(
        'span.strike-through.list span.value, .strike-through.list .value, .strike-through .value')
    if orig_el:
        result["original_price"] = orig_el.get('content') or re.sub(r'[^\d.]', '', orig_el.get_text())
    if not result["sale_price"]:
        for sel in ['[itemprop="price"]', '.price-sales', 'meta[property="product:price:amount"]']:
            el = soup.select_one(sel)
            if el:
                v = el.get('content') or re.sub(r'[^\d.]', '', el.get_text())
                if v:
                    result["sale_price"] = v
                    break
    if not result["sale_price"]:
        title = soup.title.string if soup.title else ''
        m = re.search(r'for AED\s*([\d,.]+)', title or '')
        if m:
            result["sale_price"] = m.group(1).replace(',', '')
    if not result["sale_price"]:
        for script in soup.find_all('script', type='application/ld+json'):
            try:
                data = json.loads(script.string or '')
                if isinstance(data, dict):
                    offers = data.get('offers', {})
                    if isinstance(offers, dict):
                        p = offers.get('price') or offers.get('lowPrice')
                        if p:
                            result["sale_price"] = str(p)
                            if not result["original_price"]:
                                hp = offers.get('highPrice')
                                if hp:
                                    result["original_price"] = str(hp)
                            break
            except Exception:
                pass
    return result


def extract_stock(soup):
    """Returns (in_stock: bool, stock_qty: int|None, button_label: str)"""
    in_stock = None
    stock_qty = None
    button_label = ''

    # Primary: data-available attribute
    avail_el = soup.select_one('.js-availability-container')
    if avail_el:
        data_available = avail_el.get('data-available', '').lower()
        if data_available == 'true':
            in_stock = True
        elif data_available == 'false':
            in_stock = False

    # Add to cart button text & disabled state
    cart_btn = soup.select_one('.js-add-to-cart.add-to-cart')
    if cart_btn:
        button_label = cart_btn.get_text(strip=True)
        if in_stock is None:
            if 'sold out' in button_label.lower() or cart_btn.get('disabled') is not None:
                in_stock = False
            else:
                in_stock = True

    # Bonus: GTM JSON
    if in_stock is None:
        gtm_btn = soup.select_one('.js-add-to-cart[data-gtm-enhancedecommerce-onclick]')
        if gtm_btn:
            try:
                gtm = json.loads(gtm_btn['data-gtm-enhancedecommerce-onclick'])
                items = gtm.get('ecommerce', {}).get('items', [])
                if items and 'item_in_stock' in items[0]:
                    in_stock = bool(items[0]['item_in_stock'])
            except Exception:
                pass

    # Quantity select
    qty_select = soup.select_one('.js-quantity-select, select.quantity-select')
    if qty_select:
        options = qty_select.find_all('option')
        if options:
            try:
                stock_qty = int(options[-1].get('value', len(options)))
            except (ValueError, TypeError):
                stock_qty = len(options)
        if in_stock is None:
            in_stock = True

    if in_stock is None:
        in_stock = False

    return in_stock, stock_qty, button_label


def extract_images(soup):
    images = []
    seen = set()

    def add(url):
        if not url or url.startswith('data:') or '.svg' in url.lower():
            return
        if is_placeholder(url):
            return
        key = url.split('?')[0]
        if key not in seen:
            seen.add(key)
            images.append(url)

    def src_of(tag):
        return (tag.get('src') or tag.get('data-src') or
                tag.get('data-zoom-image') or tag.get('data-lazy') or
                tag.get('data-original') or '')

    for tag in soup.select('.row.js-image-row img'):
        add(src_of(tag))

    if not images:
        for sel in ['.product-images-desktop img', '.js-img-parent-div img',
                    '.primary-images img', '.pdp-images img',
                    '.image-container img', '.product-gallery img',
                    '.carousel img', '.slick-slide img']:
            for tag in soup.select(sel):
                add(src_of(tag))
            if images:
                break

    if not images:
        og = soup.find('meta', property='og:image')
        og_url = og['content'] if og and og.get('content') else None
        if og_url and not is_placeholder(og_url):
            add(og_url)

    return images


def extract_category(soup):
    for sel in ['.breadcrumb.container', 'ol.breadcrumb', 'nav ol', '.breadcrumb']:
        bc = soup.select_one(sel)
        if bc:
            items = [li.get_text(strip=True) for li in bc.select('li, .breadcrumb-item')
                     if li.get_text(strip=True)]
            items = [i for i in items if i.lower() not in ('home', '')]
            if len(items) >= 2:
                return items[-2]
            elif len(items) == 1:
                return items[0]
    return None


# ── Mode-aware HTML parser ────────────────────────────────────────────────────

MODES = ('all', 'stock', 'images', 'prices')

def empty_result(padded_id, url):
    return {
        'product_id':     padded_id,
        'url':            url,
        'status':         'Unknown',
        'name':           '',
        'brand':          '',
        'category':       '',
        'original_price': '',
        'sale_price':     '',
        'in_stock':       None,
        'stock_qty':      None,
        'button_label':   '',
        'has_images':     False,
        'image_count':    0,
        'error':          '',
    }


def parse_html(html, padded_id, url, mode='all'):
    result = empty_result(padded_id, url)
    soup = BeautifulSoup(html, 'lxml')

    # Soft-404 detection
    canonical = soup.find('link', rel='canonical')
    final_url = canonical['href'] if canonical and canonical.get('href') else url
    if 'search' in final_url or final_url.rstrip('/') == 'https://www.thedealoutlet.com/ae-en':
        result['status'] = 'Not Found'
        return result
    title_tag = soup.find('title')
    if title_tag and ('404' in title_tag.text or 'not found' in title_tag.text.lower()):
        result['status'] = 'Not Found'
        return result

    currency = 'AED'
    cur_el = soup.select_one('meta[itemprop="priceCurrency"]')
    if cur_el and cur_el.get('content'):
        currency = cur_el['content']

    if mode == 'all':
        result['name']     = extract_title(soup) or ''
        result['brand']    = extract_brand(soup) or ''
        result['category'] = extract_category(soup) or ''
        prices = extract_prices(soup)
        if prices['sale_price']:
            result['sale_price'] = f"{currency} {prices['sale_price']}"
        if prices['original_price']:
            result['original_price'] = f"{currency} {prices['original_price']}"
        in_stock, qty, btn = extract_stock(soup)
        result['in_stock']     = in_stock
        result['stock_qty']    = qty
        result['button_label'] = btn
        imgs = extract_images(soup)
        result['has_images']  = len(imgs) > 0
        result['image_count'] = len(imgs)

    elif mode == 'stock':
        result['name']  = extract_title(soup) or ''
        result['brand'] = extract_brand(soup) or ''
        in_stock, qty, btn = extract_stock(soup)
        result['in_stock']     = in_stock
        result['stock_qty']    = qty
        result['button_label'] = btn

    elif mode == 'images':
        result['name']  = extract_title(soup) or ''
        result['brand'] = extract_brand(soup) or ''
        imgs = extract_images(soup)
        result['has_images']  = len(imgs) > 0
        result['image_count'] = len(imgs)

    elif mode == 'prices':
        result['name']  = extract_title(soup) or ''
        result['brand'] = extract_brand(soup) or ''
        prices = extract_prices(soup)
        if prices['sale_price']:
            result['sale_price'] = f"{currency} {prices['sale_price']}"
        if prices['original_price']:
            result['original_price'] = f"{currency} {prices['original_price']}"

    result['status'] = 'Found' if result['name'] else 'Found (No Details)'
    return result


# ── Async core ────────────────────────────────────────────────────────────────

async def fetch_one(session, semaphore, product_id, parse_pool, mode):
    padded_id = pad_product_id(str(product_id))
    url = BASE_URL.format(padded_id)
    result = empty_result(padded_id, url)

    async with semaphore:
        try:
            async with session.get(url, ssl=False, allow_redirects=True) as resp:
                if resp.status == 404:
                    result['status'] = 'Not Found'
                    return result
                if resp.status != 200:
                    result['status'] = f'HTTP {resp.status}'
                    return result
                html = await resp.text(encoding='utf-8', errors='replace')

            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                parse_pool, parse_html, html, padded_id, url, mode
            )
        except asyncio.TimeoutError:
            result['status'] = 'Timeout'
            result['error']  = f'Timed out after {TIMEOUT_SEC}s'
        except Exception as e:
            result['status'] = 'Connection Error'
            result['error']  = str(e)[:100]

    return result


async def scrape_all_async(product_ids, mode='all', progress_callback=None):
    timeout    = aiohttp.ClientTimeout(total=TIMEOUT_SEC)
    semaphore  = asyncio.Semaphore(CONCURRENCY)
    connector  = aiohttp.TCPConnector(limit=CONCURRENCY + 20, ssl=False)
    results    = []
    done       = 0
    total      = len(product_ids)
    parse_pool = ThreadPoolExecutor(max_workers=8)

    async with aiohttp.ClientSession(
        headers=HEADERS, timeout=timeout, connector=connector
    ) as session:
        tasks = [fetch_one(session, semaphore, pid, parse_pool, mode) for pid in product_ids]
        for coro in asyncio.as_completed(tasks):
            r = await coro
            results.append(r)
            done += 1
            if progress_callback and (done % 500 == 0 or done == total):
                progress_callback(done, total)

    parse_pool.shutdown(wait=False)
    return results


# ── Public API ────────────────────────────────────────────────────────────────

def scrape_products_bulk(product_ids, mode='all', progress_callback=None):
    import time
    results = asyncio.run(scrape_all_async(product_ids, mode, progress_callback))
    result_map = {r['product_id']: r for r in results}

    for attempt in range(MAX_RETRIES):
        failed_ids = [
            pid for pid, r in result_map.items()
            if r.get('status') in RETRY_STATUSES
            or (r.get('status', '').startswith('HTTP ') and r.get('status') not in ('HTTP 404',))
        ]
        if not failed_ids:
            break
        delay = RETRY_DELAYS[attempt]
        has_429 = any(result_map[pid].get('status') == 'HTTP 429' for pid in failed_ids)
        if has_429:
            delay = max(delay, 30)
        time.sleep(delay)

        retry_results = asyncio.run(scrape_all_async(failed_ids, mode))
        for r in retry_results:
            pid = r['product_id']
            if r.get('status') not in RETRY_STATUSES:
                result_map[pid] = r
            else:
                result_map[pid]['error'] = (
                    f"Failed after {attempt+2} attempts: {r.get('status')} {r.get('error','')}"
                ).strip()[:120]

    return list(result_map.values())


def scrape_product(product_id, mode='all'):
    results = scrape_products_bulk([product_id], mode)
    return results[0] if results else {}
