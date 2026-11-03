from flask import Flask, jsonify, request
from flask_cors import CORS
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime, date
import re
import os
import io
import json
import boto3
from dotenv import load_dotenv
from PIL import Image
from werkzeug.utils import secure_filename

# ============ BASE DIR ============
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ============ CONFIG ============
load_dotenv(dotenv_path=os.path.join(BASE_DIR, ".env.local"))
load_dotenv()

SPREADSHEET_ID = os.getenv("SPREADSHEET_ID", "1wjtZBYMI9Czg7x1j4kpG-QieOVoP3V3tUEzHl6O52fI")
CREDENTIALS_FILE = os.getenv("CREDENTIALS_FILE", "credentials.json")
FISH_TAB = "daily_fish"
ORDERS_TAB = "orders"

# Cloudflare R2 Config
R2_ACCOUNT_ID = os.getenv("R2_ACCOUNT_ID")
R2_ACCESS_KEY = os.getenv("R2_ACCESS_KEY_ID")
R2_SECRET_KEY = os.getenv("R2_SECRET_ACCESS_KEY")
R2_BUCKET = os.getenv("R2_BUCKET_NAME", "meen-kada-images")
R2_PUBLIC_URL = os.getenv("R2_PUBLIC_URL")

# Google Sheets Setup
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

google_creds_raw = os.getenv("GOOGLE_CREDENTIALS_JSON")
creds = None
if google_creds_raw:
    try:
        creds_dict = json.loads(google_creds_raw)
        creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    except Exception as e:
        print(f"Error parsing GOOGLE_CREDENTIALS_JSON: {e}")

if not creds:
    local_path = os.path.join(BASE_DIR, CREDENTIALS_FILE)
    if os.path.exists(local_path):
        creds = Credentials.from_service_account_file(local_path, scopes=SCOPES)
    elif os.path.exists(CREDENTIALS_FILE):
        creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=SCOPES)

gs_client = gspread.authorize(creds) if creds else None

# Cloudflare R2 Setup
s3 = None
if R2_ACCOUNT_ID and R2_ACCESS_KEY and R2_SECRET_KEY:
    s3 = boto3.client(
        service_name="s3",
        endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
        aws_access_key_id=R2_ACCESS_KEY,
        aws_secret_access_key=R2_SECRET_KEY,
        region_name="auto",
    )

app = Flask(__name__)
CORS(app)

# ---------- HEALTH CHECK ----------
@app.route("/health", methods=["GET"])
def health():
    import sys
    return jsonify({
        "ok": True,
        "python": sys.version,
        "base_dir": BASE_DIR,
        "gs_client": gs_client is not None,
        "s3": s3 is not None,
    })

# ---------- HELPERS ----------
def get_sheet(tab_name):
    if not gs_client:
        raise ValueError("Google Sheets client is not configured. Check GOOGLE_CREDENTIALS_JSON.")
    return gs_client.open_by_key(SPREADSHEET_ID).worksheet(tab_name)

def next_order_id(sheet):
    today_compact = datetime.now().strftime("%Y%m%d")
    prefix = f"ORD-{today_compact}-"
    all_ids = sheet.col_values(1)
    todays = [i for i in all_ids[1:] if i.startswith(prefix)]
    return f"{prefix}{len(todays) + 1:03d}"

def convert_to_webp(image_file, max_size=1000, quality=85):
    img = Image.open(image_file.stream)
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGB")
    img.thumbnail((max_size, max_size))
    out = io.BytesIO()
    img.save(out, format="WEBP", quality=quality)
    out.seek(0)
    return out.read()

def upload_to_r2(webp_bytes, filename):
    if not s3:
        raise ValueError("Cloudflare R2 is not configured. Check R2 credentials.")
    key = f"fish/{filename}"
    s3.put_object(
        Bucket=R2_BUCKET,
        Key=key,
        Body=webp_bytes,
        ContentType="image/webp",
    )
    return f"{R2_PUBLIC_URL}/{key}"

def calculate_original_price(selling_price):
    raw = selling_price * 1.25
    return int((raw + 4) // 5 * 5)

# ---------- ROUTES ----------
@app.route("/api/fish", methods=["GET"])
def get_fish():
    try:
        sheet = get_sheet(FISH_TAB)
        rows = sheet.get_all_records()
        fish = []
        for r in rows:
            if not r.get("fish_name"): continue
            if str(r.get("available", "")).strip().lower() != "yes": continue
            fish.append({
                "nameMl": r.get("fish_name", ""),
                "nameEn": r.get("name_en", ""),
                "price": int(r.get("price") or 0),
                "originalPrice": int(r.get("original_price") or 0),
                "imageUrl": r.get("image_url", ""),
                "available": True,
                "note": r.get("note", ""),
            })
        return jsonify(fish)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/order", methods=["POST"])
def create_order():
    try:
        data = request.get_json(force=True) or {}
        name = str(data.get("customer_name", "")).strip()
        phone = str(data.get("phone", "")).strip()
        fish_name = str(data.get("fish_name", "")).strip()
        unit_price = int(data.get("unit_price") or 0)
        try: kg = float(data.get("kg") or 0)
        except: kg = 0
        map_link = str(data.get("map_link", "")).strip()

        if not name: return jsonify({"error": "Name is required"}), 400
        if not re.fullmatch(r"[6-9]\d{9}", phone): return jsonify({"error": "Valid 10-digit Indian phone required"}), 400
        if not fish_name: return jsonify({"error": "Fish is required"}), 400
        if kg <= 0: return jsonify({"error": "Quantity must be greater than 0"}), 400
        if unit_price <= 0: return jsonify({"error": "Invalid price"}), 400

        total = round(kg * unit_price, 2)
        sheet = get_sheet(ORDERS_TAB)
        order_id = next_order_id(sheet)
        now = datetime.now()

        row = [order_id, now.strftime("%Y-%m-%d"), now.strftime("%H:%M:%S"), name, phone, fish_name, kg, unit_price, total, map_link, "new"]
        sheet.append_row(row, value_input_option="USER_ENTERED")

        return jsonify({"ok": True, "order_id": order_id, "total": total, "fish_name": fish_name, "kg": kg, "unit_price": unit_price}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ---------- ADMIN ROUTES ----------
@app.route("/api/admin/add-fish", methods=["POST"])
def admin_add_fish():
    try:
        name_ml = request.form.get("name_ml", "").strip()
        name_en = request.form.get("name_en", "").strip()
        price_str = request.form.get("price", "").strip()
        note = request.form.get("note", "").strip()

        if not name_ml or not name_en or not price_str:
            return jsonify({"error": "Malayalam name, English name, and price are required"}), 400
        try:
            selling_price = int(price_str)
            if selling_price <= 0: raise ValueError
        except ValueError:
            return jsonify({"error": "Price must be a positive number"}), 400

        if "image" not in request.files: return jsonify({"error": "Image is required"}), 400
        file = request.files["image"]
        if file.filename == "": return jsonify({"error": "No image selected"}), 400

        webp_bytes = convert_to_webp(file)
        safe_name = secure_filename(name_en) or "fish"
        filename = f"{safe_name}_{selling_price}.webp"
        image_url = upload_to_r2(webp_bytes, filename)
        original_price = calculate_original_price(selling_price)

        sheet = get_sheet(FISH_TAB)
        row = [date.today().isoformat(), name_ml, name_en, selling_price, original_price, image_url, "yes", note]
        sheet.append_row(row, value_input_option="USER_ENTERED")

        return jsonify({
            "ok": True,
            "name_ml": name_ml,
            "name_en": name_en,
            "selling_price": selling_price,
            "original_price": original_price,
            "discount_percent": round((1 - selling_price / original_price) * 100),
            "image_url": image_url,
        }), 201
    except Exception as e:
        print(f"[admin_add_fish] error: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/api/admin/fish", methods=["GET"])
def admin_get_fish():
    try:
        sheet = get_sheet(FISH_TAB)
        all_values = sheet.get_all_values()
        active_fish = []
        
        for i, row in enumerate(all_values[1:], start=2):
            if not row or len(row) < 7: continue
            if str(row[6]).strip().lower() == "yes":
                active_fish.append({
                    "row_id": str(i), 
                    "name_ml": row[1],
                    "name_en": row[2],
                    "price": int(row[3]) if row[3].isdigit() else 0,
                    "image_url": row[5] if len(row) > 5 else "",
                    "available": True
                })
        return jsonify(active_fish)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/admin/toggle-fish", methods=["POST"])
def admin_toggle_fish():
    try:
        data = request.get_json()
        row_id = int(data.get("row_id"))
        new_status = "yes" if data.get("available") else "no"
        
        sheet = get_sheet(FISH_TAB)
        sheet.update_cell(row_id, 7, new_status)
        
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/admin/deactivate-all", methods=["POST"])
def admin_deactivate_all():
    try:
        sheet = get_sheet(FISH_TAB)
        all_values = sheet.get_all_values()
        
        cells_to_update = []
        for i, row in enumerate(all_values[1:], start=2):
            if not row or len(row) < 7: continue
            if str(row[6]).strip().lower() == "yes":
                cells_to_update.append(gspread.Cell(row=i, col=7, value="no"))
                
        if cells_to_update:
            sheet.update_cells(cells_to_update)
            
        return jsonify({"ok": True, "count": len(cells_to_update)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
