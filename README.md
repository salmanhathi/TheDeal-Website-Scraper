# Product Availability Checker
**The Deal Outlet — Internal Operations Tool**

Reads product IDs from an Excel file, checks each product on thedealoutlet.com, and emails a daily availability report.

---

## Setup on Render

### 1. Push to GitHub
```bash
git init
git add .
git commit -m "Initial commit"
git remote add origin https://github.com/YOUR_USERNAME/pac-tdo.git
git push -u origin main
```

### 2. Create Web Service on Render
- Connect your GitHub repo
- Render will auto-detect `render.yaml`
- OR manually set: Build command `pip install -r requirements.txt`, Start command `gunicorn app:app --workers 1 --timeout 120`

### 3. Set Environment Variables in Render Dashboard
| Variable | Value |
|---|---|
| `EMAIL_FROM` | your Gmail address |
| `EMAIL_PASSWORD` | Gmail App Password (not your Gmail password) |
| `EMAIL_TO` | recipient email |
| `SCHEDULE_HOUR` | hour to run (0–23, GST) |
| `SCHEDULE_MINUTE` | minute to run |

### 4. Get a Gmail App Password
1. Enable 2FA on your Google account
2. Go to myaccount.google.com → Security → App Passwords
3. Create an app password for "Mail"
4. Use that 16-character password as `EMAIL_PASSWORD`

---

## Excel File Format
| Column A | Column B | Column C | Column D | Column E |
|---|---|---|---|---|
| Product ID | VPN / Style | Brand | Color | Size |

- Column A is required (Product ID — padded to 12 digits automatically)
- Columns B–E can be anything you want (they appear in the output)
- First row can optionally be a header row

---

## Features
- ✅ Drag & drop Excel upload
- ✅ Auto-pads product IDs to 12 digits
- ✅ Scrapes: name, category, original price, sale price, image availability
- ✅ Daily scheduled check (APScheduler, Asia/Dubai timezone)
- ✅ HTML email report with summary stats
- ✅ Filter: All / Found / Not Found / No Images / Errors
- ✅ Search by any text
- ✅ Sortable columns
- ✅ Download results as CSV

## External Cron (Recommended for Render Free Tier)
Render free services sleep after 15 mins of inactivity — the scheduler won't fire.

**Solution:** Use [cron-job.org](https://cron-job.org) (free) to call:
```
https://YOUR-APP.onrender.com/trigger
```
Set it to your desired daily time. This also wakes the service.

---

## Adding Google Sheets Support (Future)
Replace the Excel upload with a Google Sheets reader using `gspread`:
1. Create a Google Service Account
2. Share the sheet with the service account email
3. Replace `read_excel()` in `app.py` with `gspread` API calls

