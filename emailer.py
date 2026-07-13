import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime


def send_results_email(state, config):
    results = state.get('results', [])
    total = state.get('total', 0)
    found = state.get('found', 0)
    not_found = state.get('not_found', 0)
    errors = state.get('errors', 0)
    no_image = state.get('no_image', 0)

    msg = MIMEMultipart('alternative')
    msg['Subject'] = f"📦 Product Availability Report — {datetime.now().strftime('%d %b %Y')}"
    msg['From'] = config['email_from']
    msg['To'] = config['email_to']

    rows = ''
    for r in results:
        status = r.get('status', '')
        has_img = r.get('has_images', False)

        if status == 'Found':
            badge = '<span style="background:#10b981;color:#fff;padding:2px 10px;border-radius:20px;font-size:11px;font-weight:600">FOUND</span>'
            row_bg = '#ffffff'
        elif status == 'Not Found':
            badge = '<span style="background:#ef4444;color:#fff;padding:2px 10px;border-radius:20px;font-size:11px;font-weight:600">NOT FOUND</span>'
            row_bg = '#fff8f8'
        elif 'No Details' in status:
            badge = '<span style="background:#f59e0b;color:#fff;padding:2px 10px;border-radius:20px;font-size:11px;font-weight:600">NO DETAILS</span>'
            row_bg = '#fffbeb'
        else:
            badge = f'<span style="background:#94a3b8;color:#fff;padding:2px 10px;border-radius:20px;font-size:11px;font-weight:600">{status.upper()}</span>'
            row_bg = '#f8fafc'

        img_cell = (
            '<span style="color:#10b981;font-size:15px">✓</span>' if has_img
            else '<span style="color:#ef4444;font-size:15px">✗</span>'
        )
        img_count = f' <span style="color:#94a3b8;font-size:11px">({r.get("image_count",0)})</span>' if has_img else ''

        rows += f"""<tr style="background:{row_bg}">
          <td style="padding:10px 12px;border-bottom:1px solid #f1f5f9;font-family:monospace;font-size:12px;color:#475569">{r.get('product_id','')}</td>
          <td style="padding:10px 12px;border-bottom:1px solid #f1f5f9">{badge}</td>
          <td style="padding:10px 12px;border-bottom:1px solid #f1f5f9;font-size:13px;color:#1e293b">{(r.get('name','') or '—')[:55]}</td>
          <td style="padding:10px 12px;border-bottom:1px solid #f1f5f9;font-size:12px;color:#64748b">{r.get('category','') or '—'}</td>
          <td style="padding:10px 12px;border-bottom:1px solid #f1f5f9;font-size:12px;color:#94a3b8;text-decoration:line-through">{r.get('original_price','') or '—'}</td>
          <td style="padding:10px 12px;border-bottom:1px solid #f1f5f9;font-size:13px;font-weight:600;color:#0f172a">{r.get('sale_price','') or '—'}</td>
          <td style="padding:10px 12px;border-bottom:1px solid #f1f5f9;text-align:center">{img_cell}{img_count}</td>
          <td style="padding:10px 12px;border-bottom:1px solid #f1f5f9"><a href="{r.get('url','')}" style="color:#f97316;text-decoration:none;font-size:12px;font-weight:500">View ↗</a></td>
        </tr>"""

    html = f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#f1f5f9;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif">
<div style="max-width:960px;margin:32px auto;padding:0 16px">

  <!-- Header -->
  <div style="background:#0d1117;border-radius:12px 12px 0 0;padding:28px 32px;display:flex;align-items:center">
    <img src="https://www.thedealoutlet.com/on/demandware.static/Sites-TheDealOutlet_AE-Site/-/default/dwf652137f/images/logo.svg"
         height="36" alt="The Deal Outlet" style="display:block">
    <div style="margin-left:auto;text-align:right">
      <div style="color:#f97316;font-size:12px;font-weight:600;letter-spacing:0.08em;text-transform:uppercase">Product Availability Checker</div>
      <div style="color:#64748b;font-size:12px;margin-top:2px">{datetime.now().strftime('%A, %d %B %Y · %I:%M %p')}</div>
    </div>
  </div>

  <!-- Stats -->
  <div style="display:flex;background:#fff;border-left:1px solid #e2e8f0;border-right:1px solid #e2e8f0">
    <div style="flex:1;padding:20px 24px;border-right:1px solid #f1f5f9;text-align:center">
      <div style="font-size:32px;font-weight:700;color:#1e293b;line-height:1">{total}</div>
      <div style="font-size:11px;font-weight:600;letter-spacing:0.06em;text-transform:uppercase;color:#94a3b8;margin-top:4px">Total Checked</div>
    </div>
    <div style="flex:1;padding:20px 24px;border-right:1px solid #f1f5f9;text-align:center">
      <div style="font-size:32px;font-weight:700;color:#10b981;line-height:1">{found}</div>
      <div style="font-size:11px;font-weight:600;letter-spacing:0.06em;text-transform:uppercase;color:#94a3b8;margin-top:4px">Found</div>
    </div>
    <div style="flex:1;padding:20px 24px;border-right:1px solid #f1f5f9;text-align:center">
      <div style="font-size:32px;font-weight:700;color:#ef4444;line-height:1">{not_found}</div>
      <div style="font-size:11px;font-weight:600;letter-spacing:0.06em;text-transform:uppercase;color:#94a3b8;margin-top:4px">Not Found</div>
    </div>
    <div style="flex:1;padding:20px 24px;border-right:1px solid #f1f5f9;text-align:center">
      <div style="font-size:32px;font-weight:700;color:#f59e0b;line-height:1">{no_image}</div>
      <div style="font-size:11px;font-weight:600;letter-spacing:0.06em;text-transform:uppercase;color:#94a3b8;margin-top:4px">Missing Images</div>
    </div>
    <div style="flex:1;padding:20px 24px;text-align:center">
      <div style="font-size:32px;font-weight:700;color:#94a3b8;line-height:1">{errors}</div>
      <div style="font-size:11px;font-weight:600;letter-spacing:0.06em;text-transform:uppercase;color:#94a3b8;margin-top:4px">Errors</div>
    </div>
  </div>

  <!-- Table -->
  <div style="background:#fff;border:1px solid #e2e8f0;border-top:none;border-radius:0 0 12px 12px;overflow:hidden">
    <table style="width:100%;border-collapse:collapse;font-size:13px">
      <thead>
        <tr style="background:#f8fafc;border-bottom:2px solid #e2e8f0">
          <th style="padding:10px 12px;text-align:left;font-size:11px;font-weight:600;letter-spacing:0.05em;text-transform:uppercase;color:#94a3b8">Product ID</th>
          <th style="padding:10px 12px;text-align:left;font-size:11px;font-weight:600;letter-spacing:0.05em;text-transform:uppercase;color:#94a3b8">Status</th>
          <th style="padding:10px 12px;text-align:left;font-size:11px;font-weight:600;letter-spacing:0.05em;text-transform:uppercase;color:#94a3b8">Product Name</th>
          <th style="padding:10px 12px;text-align:left;font-size:11px;font-weight:600;letter-spacing:0.05em;text-transform:uppercase;color:#94a3b8">Category</th>
          <th style="padding:10px 12px;text-align:left;font-size:11px;font-weight:600;letter-spacing:0.05em;text-transform:uppercase;color:#94a3b8">Was</th>
          <th style="padding:10px 12px;text-align:left;font-size:11px;font-weight:600;letter-spacing:0.05em;text-transform:uppercase;color:#94a3b8">Now</th>
          <th style="padding:10px 12px;text-align:center;font-size:11px;font-weight:600;letter-spacing:0.05em;text-transform:uppercase;color:#94a3b8">Images</th>
          <th style="padding:10px 12px;text-align:left;font-size:11px;font-weight:600;letter-spacing:0.05em;text-transform:uppercase;color:#94a3b8">Link</th>
        </tr>
      </thead>
      <tbody>{rows}</tbody>
    </table>

    <div style="padding:16px 24px;background:#f8fafc;text-align:center;color:#94a3b8;font-size:11px;border-top:1px solid #f1f5f9">
      Generated by Product Availability Checker · The Deal Outlet Operations
    </div>
  </div>
</div>
</body>
</html>"""

    msg.attach(MIMEText(html, 'html'))

    with smtplib.SMTP(config.get('smtp_host', 'smtp.gmail.com'), int(config.get('smtp_port', 587))) as s:
        s.ehlo()
        s.starttls()
        s.login(config['email_from'], config['email_password'])
        s.sendmail(config['email_from'], config['email_to'], msg.as_string())
