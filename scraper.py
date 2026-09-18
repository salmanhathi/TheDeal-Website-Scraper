import re
import json
import asyncio
import sys
import time
import logging
import urllib3
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger(__name__)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Windows needs SelectorEventLoop for aiohttp to work properly
if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

try:
    import aiohttp
    AIOHTTP_AVAILABLE = True
except ImportError:
    AIOHTTP_AVAILABLE = False

BASE_URLS = {
    'ae': "https://www.thedealoutlet.com/ae-en/{}.html",
    'sa': "https://www.thedealoutlet.com/sa-en/{}.html",
}
BASE_URL = BASE_URLS['ae']  # default, kept for debug route compatibility

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

# ── Tuning ────────────────────────────────────────────────────────────────────
CONCURRENCY    = 120   # simultaneous connections
TIMEOUT_SEC    = 12    # per-request timeout (fail fast)
PARSE_WORKERS  = 10    # threads for BeautifulSoup parsing
CHUNK_SIZE     = 500   # save to disk every N products
RETRY_DELAY_1  = 10    # seconds before first retry pass
RETRY_DELAY_2  = 30    # seconds before second retry pass
RETRY_STATUSES = {
    'Timeout', 'Connection Error', 'Error',
    'HTTP 429', 'HTTP 500', 'HTTP 409', 'HTTP 503', 'HTTP 502',
}
MODES = ('all', 'stock', 'images', 'prices')


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
    """For debug route only."""
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
    in_stock = None
    stock_qty = None
    button_label = ''
    avail_el = soup.select_one('.js-availability-container')
    if avail_el:
        da = avail_el.get('data-available', '').lower()
        if da == 'true':
            in_stock = True
        elif da == 'false':
            in_stock = False
    cart_btn = soup.select_one('.js-add-to-cart.add-to-cart')
    if cart_btn:
        button_label = cart_btn.get_text(strip=True)
        if in_stock is None:
            in_stock = False if ('sold out' in button_label.lower() or
                                  cart_btn.get('disabled') is not None) else True
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
    # Override button label based on data-available (JS changes it dynamically)
    if in_stock is True:
        button_label = 'Add to Bag'
    elif in_stock is False:
        button_label = 'Sold Out'
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
                    '.image-container img', '.product-gallery img']:
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


# ── Result builder ────────────────────────────────────────────────────────────

def empty_result(padded_id, url):
    return {
        'product_id': padded_id, 'url': url, 'country': 'ae',
        'status': 'Unknown', 'name': '', 'brand': '', 'category': '',
        'original_price': '', 'sale_price': '',
        'in_stock': None, 'stock_qty': None, 'button_label': '',
        'has_images': False, 'image_count': 0, 'error': '',
    }


def parse_html(html, padded_id, url, mode='all'):
    result = empty_result(padded_id, url)
    result['country'] = 'sa' if '/sa-en/' in url else 'ae'
    soup = BeautifulSoup(html, 'lxml')

    canonical = soup.find('link', rel='canonical')
    final_url = canonical['href'] if canonical and canonical.get('href') else url
    if 'search' in final_url or final_url.rstrip('/') == 'https://www.thedealoutlet.com/ae-en':
        result['status'] = 'Not Found'
        return result
    title_tag = soup.find('title')
    if title_tag and ('404' in title_tag.text or 'not found' in title_tag.text.lower()):
        result['status'] = 'Not Found'
        return result

    country_default = {'ae': 'AED', 'sa': 'SAR'}
    currency = country_default.get(result.get('country', 'ae'), 'AED')
    cur_el = soup.select_one('meta[itemprop="priceCurrency"]')
    if cur_el and cur_el.get('content'):
        currency = cur_el['content']

    # Always get name + brand regardless of mode
    result['name']  = extract_title(soup) or ''
    result['brand'] = extract_brand(soup) or ''

    if mode in ('all', 'prices'):
        prices = extract_prices(soup)
        if prices['sale_price']:
            result['sale_price'] = f"{currency} {prices['sale_price']}"
        if prices['original_price']:
            result['original_price'] = f"{currency} {prices['original_price']}"

    if mode in ('all', 'stock'):
        in_stock, qty, btn = extract_stock(soup)
        result['in_stock']     = in_stock
        result['stock_qty']    = qty
        result['button_label'] = btn

    if mode in ('all', 'images'):
        imgs = extract_images(soup)
        result['has_images']  = len(imgs) > 0
        result['image_count'] = len(imgs)

    if mode == 'all':
        result['category'] = extract_category(soup) or ''

    result['status'] = 'Found' if result['name'] else 'Found (No Details)'
    return result


# ── Async engine ──────────────────────────────────────────────────────────────

async def _fetch_one(session, semaphore, product_id, parse_pool, mode, country='ae'):
    padded_id = pad_product_id(str(product_id))
    url = BASE_URLS.get(country, BASE_URLS['ae']).format(padded_id)
    result = empty_result(padded_id, url)
    result['country'] = 'sa' if '/sa-en/' in url else 'ae'

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


async def _run_pass(product_ids, mode, country='ae', label='Pass 1'):
    """Run one async scraping pass — no retries, just speed."""
    timeout   = aiohttp.ClientTimeout(total=TIMEOUT_SEC)
    semaphore = asyncio.Semaphore(CONCURRENCY)
    connector = aiohttp.TCPConnector(
        limit=CONCURRENCY + 20,
        ssl=False,
        limit_per_host=0,      # no per-host cap
        enable_cleanup_closed=True,
    )
    parse_pool = ThreadPoolExecutor(max_workers=PARSE_WORKERS)
    results    = []
    done       = 0
    total      = len(product_ids)

    async with aiohttp.ClientSession(
        headers=HEADERS, timeout=timeout, connector=connector
    ) as session:
        tasks = [
            _fetch_one(session, semaphore, pid, parse_pool, mode, country)
            for pid in product_ids
        ]
        for coro in asyncio.as_completed(tasks):
            r = await coro
            results.append(r)
            done += 1
            if done % 500 == 0 or done == total:
                fails = sum(1 for x in results if x['status'] in RETRY_STATUSES)
                logger.info(f"{label}: {done}/{total} done — {fails} failures so far")

    parse_pool.shutdown(wait=False)
    return results


# ── Public API ────────────────────────────────────────────────────────────────

def scrape_products_bulk(product_ids, mode='all', country='ae', progress_callback=None):
    """
    Fast 3-pass scraping:
      Pass 1 — all products at full concurrency, no waiting
      Pass 2 — retry failures after short delay
      Pass 3 — final retry for persistent failures
    """
    total = len(product_ids)
    logger.info(f"Starting Pass 1 — {total} products, concurrency={CONCURRENCY}")

    # ── Pass 1: full speed ────────────────────────────────────────────────────
    results    = asyncio.run(_run_pass(product_ids, mode, country, 'Pass 1'))
    result_map = {r['product_id']: r for r in results}

    failed = [pid for pid, r in result_map.items() if r['status'] in RETRY_STATUSES]
    logger.info(f"Pass 1 done — {total - len(failed)}/{total} succeeded, {len(failed)} to retry")

    if not failed:
        return list(result_map.values())

    # ── Pass 2: retry failures ────────────────────────────────────────────────
    logger.info(f"Waiting {RETRY_DELAY_1}s before Pass 2 ({len(failed)} items)…")
    time.sleep(RETRY_DELAY_1)

    retry2  = asyncio.run(_run_pass(failed, mode, country, 'Pass 2'))
    still_failing = []
    for r in retry2:
        pid = r['product_id']
        if r['status'] not in RETRY_STATUSES:
            result_map[pid] = r   # recovered
        else:
            still_failing.append(pid)

    logger.info(f"Pass 2 done — {len(failed) - len(still_failing)} recovered, {len(still_failing)} still failing")

    if not still_failing:
        return list(result_map.values())

    # ── Pass 3: final retry ───────────────────────────────────────────────────
    logger.info(f"Waiting {RETRY_DELAY_2}s before Pass 3 ({len(still_failing)} items)…")
    time.sleep(RETRY_DELAY_2)

    retry3 = asyncio.run(_run_pass(still_failing, mode, country, 'Pass 3'))
    for r in retry3:
        pid = r['product_id']
        if r['status'] not in RETRY_STATUSES:
            result_map[pid] = r  # recovered
        else:
            # Mark as permanent failure
            result_map[pid]['error'] = (
                f"Failed after 3 passes: {r['status']} {r.get('error', '')}"
            ).strip()[:120]

    final_fails = sum(1 for r in result_map.values() if r['status'] in RETRY_STATUSES)
    logger.info(f"All passes done — {total - final_fails}/{total} succeeded, {final_fails} permanent failures")

    return list(result_map.values())


def scrape_product(product_id, mode='all', country='ae'):
    """Single product — for debug route."""
    results = scrape_products_bulk([product_id], mode, country)
    return results[0] if results else {}
