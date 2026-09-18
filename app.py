import os
import json
import io
import csv
import logging
import tempfile
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

from flask import Flask, render_template, request, jsonify, send_file
from apscheduler.schedulers.background import BackgroundScheduler
import openpyxl

from scraper import scrape_product, scrape_products_bulk, pad_product_id, MODES
from emailer import send_results_email

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'pac-tdo-2024-secret')

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

_TMP = os.path.join(tempfile.gettempdir(), 'pac')
os.makedirs(_TMP, exist_ok=True)
RESULTS_FILE = os.path.join(_TMP, 'pac_results.json')
CONFIG_FILE  = os.path.join(_TMP, 'pac_config.json')
UPLOAD_FILE  = os.path.join(_TMP, 'pac_upload.xlsx')
WORKERS = 20


# ── Persistence ───────────────────────────────────────────────────────────────

def load_results():
    if os.path.exists(RESULTS_FILE):
        try:
            with open(RESULTS_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {'results': [], 'last_run': None, 'total': 0,
            'found': 0, 'not_found': 0, 'errors': 0, 'no_image': 0,
            'headers': [], 'mode': 'all', 'partial': False}

def save_results(data):
    with open(RESULTS_FILE, 'w') as f:
        json.dump(data, f)

def load_config():
    cfg = {
        'email_to':        os.environ.get('EMAIL_TO', ''),
        'email_from':      os.environ.get('EMAIL_FROM', ''),
        'email_password':  os.environ.get('EMAIL_PASSWORD', ''),
        'smtp_host':       os.environ.get('SMTP_HOST', 'smtp.gmail.com'),
        'smtp_port':       os.environ.get('SMTP_PORT', '587'),
        'schedule_hour':   os.environ.get('SCHEDULE_HOUR', '8'),
        'schedule_minute': os.environ.get('SCHEDULE_MINUTE', '0'),
        'send_email':      os.environ.get('SEND_EMAIL', 'true'),
        'default_mode':    'all',
    }
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE) as f:
                cfg.update(json.load(f))
        except Exception:
            pass
    return cfg

def save_config(cfg):
    safe = {k: v for k, v in cfg.items() if 'password' not in k.lower()}
    with open(CONFIG_FILE, 'w') as f:
        json.dump(safe, f)


# ── Excel reading ─────────────────────────────────────────────────────────────

def read_excel(filepath):
    wb = openpyxl.load_workbook(filepath, read_only=True, data_only=True)
    ws = wb.active
    products = []
    headers  = []
    for row_idx, row in enumerate(ws.iter_rows(values_only=True)):
        if not row or row[0] is None:
            continue
        first = str(row[0]).strip().lower()
        if row_idx == 0 and first in ('product id', 'productid', 'product_id', 'id', 'sku', 'barcode'):
            headers = [str(c) if c else '' for c in row]
            continue
        products.append({
            'id':    row[0],
            'extra': [str(c) if c is not None else '' for c in list(row[1:5])],
        })
    if not headers and products:
        headers = ['Product ID', 'Col 2', 'Col 3', 'Col 4', 'Col 5']
    return products, headers


# ── Core checker ──────────────────────────────────────────────────────────────

def build_state(results, headers, mode, prior_results=None, is_partial=False, country='ae'):
    all_results = (prior_results or []) + results
    return {
        'results':   all_results,
        'last_run':  datetime.now().isoformat(),
        'total':     len(all_results),
        'found':     sum(1 for r in all_results if r['status'] == 'Found'),
        'not_found': sum(1 for r in all_results if r['status'] == 'Not Found'),
        'errors':    sum(1 for r in all_results if r['status'] in ('Error', 'Timeout', 'Connection Error')),
        'no_image':  sum(1 for r in all_results if r['status'] == 'Found' and not r.get('has_images')),
        'no_stock':  sum(1 for r in all_results if r['status'] == 'Found' and r.get('in_stock') is False),
        'headers':   headers,
        'mode':      mode,
        'country':   country,
        'partial':   is_partial,
    }


def run_checker(mode='all', country='ae'):
    logger.info(f"Starting check — mode: {mode}, country: {country}")

    if not os.path.exists(UPLOAD_FILE):
        logger.warning("No upload file — skipping")
        return None

    try:
        products, headers = read_excel(UPLOAD_FILE)
    except Exception as e:
        logger.error(f"Excel read failed: {e}")
        return None

    if not products:
        return None

    # Resume support
    existing     = load_results()
    already_done = {r['product_id'] for r in existing.get('results', [])}
    if already_done and existing.get('partial'):
        logger.info(f"Resuming — {len(already_done)} already done")
        products = [p for p in products if pad_product_id(str(p['id'])) not in already_done]
        prior_results = existing['results']
    else:
        prior_results = []

    product_ids = [p['id'] for p in products]
    extra_map   = {str(p['id']): p.get('extra', ['', '', '', '']) for p in products}

    results = []
    CHUNK   = 500

    for chunk_start in range(0, len(product_ids), CHUNK):
        chunk_ids = product_ids[chunk_start:chunk_start + CHUNK]
        chunk_raw = scrape_products_bulk(chunk_ids, mode=mode, country=country)

        for r in chunk_raw:
            pid = str(r.get('product_id', ''))
            r['extra'] = extra_map.get(pid, extra_map.get(pid.lstrip('0'), ['', '', '', '']))
            results.append(r)

        is_partial = chunk_start + len(chunk_ids) < len(product_ids)
        state = build_state(results, headers, mode, prior_results, is_partial, country)
        save_results(state)
        logger.info(f"Saved: {len(prior_results) + len(results)}/{len(prior_results) + len(product_ids)}")

    state = build_state(results, headers, mode, prior_results, False)
    save_results(state)
    logger.info(f"Done — {state['total']} products")

    cfg = load_config()
    if cfg.get('send_email', 'true') == 'true' and cfg.get('email_to') and cfg.get('email_password'):
        try:
            send_results_email(state, cfg)
            logger.info("Email sent")
        except Exception as e:
            logger.error(f"Email failed: {e}")

    return state


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    state    = load_results()
    config   = load_config()
    has_file = os.path.exists(UPLOAD_FILE)
    return render_template('index.html', state=state, config=config, has_file=has_file)


@app.route('/upload', methods=['POST'])
def upload():
    if 'file' not in request.files:
        return jsonify({'error': 'No file provided'}), 400
    f = request.files['file']
    if not f.filename.lower().endswith(('.xlsx', '.xls')):
        return jsonify({'error': 'Please upload an Excel file (.xlsx)'}), 400
    f.save(UPLOAD_FILE)
    try:
        products, headers = read_excel(UPLOAD_FILE)
        return jsonify({'success': True, 'count': len(products), 'headers': headers,
                        'filename': f.filename})
    except Exception as e:
        return jsonify({'error': f'Could not read file: {str(e)}'}), 400


@app.route('/run', methods=['POST'])
def run_now():
    try:
        data = request.json or {}
        mode    = data.get('mode', 'all')
        country = data.get('country', 'ae')
        if mode not in MODES:
            mode = 'all'
        if country not in ('ae', 'sa'):
            country = 'ae'
        state = run_checker(mode, country)
        if state is None:
            return jsonify({'error': 'Upload a file first, or file contains no products'}), 400
        return jsonify({
            'success':   True,
            'total':     state['total'],
            'found':     state['found'],
            'not_found': state['not_found'],
            'errors':    state['errors'],
            'no_image':  state['no_image'],
            'no_stock':  state.get('no_stock', 0),
        })
    except Exception as e:
        logger.exception("run_now failed")
        return jsonify({'error': str(e)}), 500


@app.route('/results')
def get_results():
    return jsonify(load_results())


@app.route('/clear', methods=['POST'])
def clear_results():
    for f in [RESULTS_FILE]:
        if os.path.exists(f):
            os.remove(f)
    return jsonify({'success': True})


@app.route('/download')
def download():
    state = load_results()
    if not state.get('results'):
        return 'No results yet', 404

    mode    = state.get('mode', 'all')
    headers = state.get('headers', [])
    extra_h = (headers[1:5] if len(headers) > 1 else []) + ['', '', '', '']
    extra_h = [h or f'Extra {i+1}' for i, h in enumerate(extra_h[:4])]

    # Build column list based on mode
    cols = ['Product ID', 'Status', 'Brand', 'Product Name']
    if mode in ('all', 'stock'):
        cols += ['In Stock', 'Stock Qty', 'Button']
    if mode in ('all', 'prices'):
        cols += ['Original Price (Was)', 'Sale Price (Now)']
    if mode in ('all', 'images'):
        cols += ['Has Images', 'Image Count']
    if mode == 'all':
        cols += ['Category']
    cols += ['URL'] + extra_h

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(cols)

    for r in state['results']:
        extra = (r.get('extra') or []) + ['', '', '', '']
        row = [r.get('product_id', ''), r.get('status', ''),
               r.get('brand', ''), r.get('name', '')]
        if mode in ('all', 'stock'):
            in_stock = r.get('in_stock')
            row += [
                'Yes' if in_stock is True else ('No' if in_stock is False else ''),
                r.get('stock_qty', '') or '',
                r.get('button_label', ''),
            ]
        if mode in ('all', 'prices'):
            row += [r.get('original_price', ''), r.get('sale_price', '')]
        if mode in ('all', 'images'):
            row += ['Yes' if r.get('has_images') else 'No', r.get('image_count', 0)]
        if mode == 'all':
            row += [r.get('category', '')]
        row += [r.get('url', '')] + extra[:4]
        writer.writerow(row)

    output.seek(0)
    fname = f"pac_{mode}_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
    return send_file(
        io.BytesIO(output.getvalue().encode('utf-8-sig')),
        mimetype='text/csv',
        as_attachment=True,
        download_name=fname,
    )


@app.route('/save-config', methods=['POST'])
def save_config_route():
    data = request.json or {}
    cfg  = load_config()
    cfg.update(data)
    save_config(cfg)
    try:
        scheduler.reschedule_job(
            'daily_check',
            trigger='cron',
            hour=int(cfg.get('schedule_hour', 8)),
            minute=int(cfg.get('schedule_minute', 0)),
        )
    except Exception:
        pass
    return jsonify({'success': True})


@app.route('/trigger')
def external_trigger():
    cfg   = load_config()
    mode  = cfg.get('default_mode', 'all')
    state = run_checker(mode)
    if state:
        return jsonify({'success': True, 'total': state['total'], 'found': state['found']})
    return jsonify({'success': False, 'error': 'No file or no products'}), 400


# ── Scheduler ─────────────────────────────────────────────────────────────────

scheduler = BackgroundScheduler(timezone='Asia/Dubai')

def start_scheduler():
    cfg = load_config()
    h   = int(cfg.get('schedule_hour', 8))
    m   = int(cfg.get('schedule_minute', 0))
    scheduler.add_job(lambda: run_checker(cfg.get('default_mode', 'all')),
                      'cron', hour=h, minute=m,
                      id='daily_check', replace_existing=True)
    scheduler.start()
    logger.info(f"Scheduler started — daily at {h:02d}:{m:02d} GST")


start_scheduler()


# ── Debug route (remove after testing) ───────────────────────────────────────

@app.route('/debug/<pid>')
def debug_product(pid):
    from scraper import pad_product_id, make_session, BASE_URL
    from bs4 import BeautifulSoup
    padded = pad_product_id(pid)
    url    = BASE_URL.format(padded)
    session = make_session()
    resp   = session.get(url, timeout=25, verify=False, allow_redirects=True)
    soup   = BeautifulSoup(resp.text, 'lxml')
    checks = {
        'final_url':   resp.url,
        'status_code': resp.status_code,
        'page_title':  soup.title.text.strip() if soup.title else 'N/A',
        '.product-name':         str(soup.select_one('.product-name'))[:300],
        '.product-brand':        str(soup.select_one('.product-brand'))[:300],
        'h2.sales':              str(soup.select_one('h2.sales'))[:300],
        '.strike-through.list':  str(soup.select_one('.strike-through.list'))[:300],
        '.js-availability-container': str(soup.select_one('.js-availability-container'))[:200],
        '.js-add-to-cart text':  (soup.select_one('.js-add-to-cart') or object()).__class__.__name__,
        '.js-quantity-select options': len(soup.select('.js-quantity-select option')),
        '.row.js-image-row img count': len(soup.select('.row.js-image-row img')),
    }
    cart = soup.select_one('.js-add-to-cart')
    if cart:
        checks['.js-add-to-cart text'] = cart.get_text(strip=True)
    lines = [f'[{k}]\n{v}' for k, v in checks.items()]
    return '<pre style="font-size:13px;padding:24px;line-height:1.8">' + '\n\n'.join(lines) + '</pre>'


if __name__ == '__main__':
    app.run(debug=True, use_reloader=False, port=5011)
