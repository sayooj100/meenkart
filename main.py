from flask import Flask, jsonify, request, send_from_directory
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

# ── Paths ───────────────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env.local"))

# ── Config ──────────────────────────────────────────────────────────────────
SPREADSHEET_ID   = "1wjtZBYMI9Czg7x1j4kpG-QieOVoP3V3tUEzHl6O52fI"
CREDENTIALS_FILE = os.path.join(BASE_DIR, "credentials.json")
SHEET_FISH       = "daily_fish"
SHEET_ORDERS     = "orders"

R2_ACCOUNT_ID        = os.getenv("R2_ACCOUNT_ID")
R2_ACCESS_KEY_ID     = os.getenv("R2_ACCESS_KEY_ID")
R2_SECRET_ACCESS_KEY = os.getenv("R2_SECRET_ACCESS_KEY")
R2_BUCKET_NAME       = os.getenv("R2_BUCKET_NAME", "meen-kada-images")
R2_PUBLIC_URL        = os.getenv("R2_PUBLIC_URL", "")

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# ── Flask ────────────────────────────────────────────────────────────────────
app = Flask(__name__, static_folder=BASE_DIR)
CORS(app)

# ── Google Sheets helper ─────────────────────────────────────────────────────
def get_sheet(sheet_name: str):
    google_creds_json = os.getenv("GOOGLE_CREDENTIALS_JSON")
    if google_creds_json:
        try:
            info  = json.loads(google_creds_json)
            creds = Credentials.from_service_account_info(info, scopes=SCOPES)
        except Exception as e:
            print("Error parsing GOOGLE_CREDENTIALS_JSON: ", e)
            creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=SCOPES)
    else:
        creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=SCOPES)
    gc = gspread.authorize(creds)
    return gc.open_by_key(SPREADSHEET_ID).worksheet(sheet_name)


# ── R2 (Cloudflare) helper ───────────────────────────────────────────────────
def get_r2_client():
    return boto3.client(
        "s3",
        endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
        aws_access_key_id=R2_ACCESS_KEY_ID,
        aws_secret_access_key=R2_SECRET_ACCESS_KEY,
        region_name="auto",
    )


# ── Sheet column indices (0-based) ───────────────────────────────────────────
# Columns: row_id(0) | name_ml(1) | name_en(2) | price(3) | original_price(4)
#          note(5) | available(6) | image_url(7) | type(8)
COL_ROW_ID     = 0
COL_NAME_ML    = 1
COL_NAME_EN    = 2
COL_PRICE      = 3
COL_ORIG_PRICE = 4
COL_NOTE       = 5
COL_AVAILABLE  = 6
COL_IMAGE_URL  = 7
COL_TYPE       = 8


def parse_items(rows, type_filter=None):
    """Parse sheet rows into product dicts. Optionally filter by type."""
    items = []
    for row in rows:
        if not row or str(row[COL_ROW_ID]).strip() == "row_id":
            continue
        # Pad short rows so we don't get index errors
        while len(row) <= COL_TYPE:
            row.append("")
        available = str(row[COL_AVAILABLE]).strip().upper()
        if available not in ("TRUE", "1", "YES"):
            continue
        item_type = str(row[COL_TYPE]).strip().lower() or "fish"
        if type_filter and item_type != type_filter:
            continue
        try:
            price = float(row[COL_PRICE])
        except (ValueError, TypeError):
            continue
        orig = None
        try:
            orig_val = float(row[COL_ORIG_PRICE])
            if orig_val > price:
                orig = orig_val
        except (ValueError, TypeError):
            pass
        items.append({
            "row_id":        str(row[COL_ROW_ID]),
            "nameMl":        row[COL_NAME_ML],
            "nameEn":        row[COL_NAME_EN],
            "price":         price,
            "originalPrice": orig,
            "note":          row[COL_NOTE],
            "imageUrl":      row[COL_IMAGE_URL],
            "type":          item_type,
        })
    return items


# ── Static File Routes ───────────────────────────────────────────────────────
@app.route("/", methods=["GET"])
def index():
    return send_from_directory(BASE_DIR, "index.html")

@app.route("/admin.html", methods=["GET"])
def admin():
    return send_from_directory(BASE_DIR, "admin.html")

@app.route("/assets/<path:path>", methods=["GET"])
def assets(path):
    return send_from_directory(os.path.join(BASE_DIR, "assets"), path)

@app.route("/favicon.ico", methods=["GET"])
def favicon():
    return send_from_directory(BASE_DIR, "favicon.ico")


# ── Public API ───────────────────────────────────────────────────────────────
@app.route("/api/fish", methods=["GET"])
def api_fish():
    try:
        sh   = get_sheet(SHEET_FISH)
        rows = sh.get_all_values()
        return jsonify(parse_items(rows, type_filter="fish"))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/chicken", methods=["GET"])
def api_chicken():
    try:
        sh   = get_sheet(SHEET_FISH)
        rows = sh.get_all_values()
        return jsonify(parse_items(rows, type_filter="chicken"))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/beef", methods=["GET"])
def api_beef():
    try:
        sh   = get_sheet(SHEET_FISH)
        rows = sh.get_all_values()
        return jsonify(parse_items(rows, type_filter="beef"))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/order", methods=["POST"])
def api_order():
    try:
        data          = request.get_json(force=True)
        customer_name = data.get("customer_name", "").strip()
        phone         = re.sub(r"\D", "", data.get("phone", ""))
        fish_name     = data.get("fish_name", "").strip()
        kg            = float(data.get("kg", 0))
        unit_price    = float(data.get("unit_price", 0))
        map_link      = data.get("map_link", "").strip()
        total         = round(kg * unit_price)

        if not customer_name or len(phone) != 10 or not fish_name or kg <= 0:
            return jsonify({"error": "Invalid order data"}), 400

        now      = datetime.now()
        order_id = f"ORD-{now.strftime('%Y%m%d%H%M%S')}"

        sh = get_sheet(SHEET_ORDERS)
        sh.append_row([
            order_id,
            str(date.today()),
            now.strftime("%H:%M:%S"),
            customer_name,
            phone,
            fish_name,
            kg,
            unit_price,
            total,
            map_link,
        ])

        return jsonify({
            "ok":         True,
            "order_id":   order_id,
            "fish_name":  fish_name,
            "kg":         kg,
            "unit_price": unit_price,
            "total":      total,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Admin API ────────────────────────────────────────────────────────────────
@app.route("/api/admin/fish", methods=["GET"])
def admin_fish():
    """Return all currently AVAILABLE items (all types) for admin view."""
    try:
        sh   = get_sheet(SHEET_FISH)
        rows = sh.get_all_values()
        items = []
        for row in rows:
            if not row or str(row[COL_ROW_ID]).strip() == "row_id":
                continue
            while len(row) <= COL_TYPE:
                row.append("")
            available = str(row[COL_AVAILABLE]).strip().upper()
            if available not in ("TRUE", "1", "YES"):
                continue
            try:
                price = float(row[COL_PRICE])
            except (ValueError, TypeError):
                continue
            items.append({
                "row_id":    str(row[COL_ROW_ID]),
                "name_ml":   row[COL_NAME_ML],
                "name_en":   row[COL_NAME_EN],
                "price":     price,
                "image_url": row[COL_IMAGE_URL],
                "type":      str(row[COL_TYPE]).strip().lower() or "fish",
            })
        return jsonify(items)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/admin/add-fish", methods=["POST"])
def admin_add_fish():
    try:
        file      = request.files.get("image")
        name_ml   = request.form.get("name_ml", "").strip()
        name_en   = request.form.get("name_en", "").strip()
        price     = float(request.form.get("price", 0))
        note      = request.form.get("note", "").strip()
        item_type = request.form.get("type", "fish").strip().lower()

        if not file or not name_ml or not name_en or price <= 0:
            return jsonify({"error": "Missing required fields"}), 400

        if item_type not in ("fish", "chicken", "beef"):
            return jsonify({"error": "Invalid type. Must be fish, chicken, or beef"}), 400

        # Upload image to Cloudflare R2
        img = Image.open(file.stream).convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="WEBP", quality=82)
        buf.seek(0)

        fname = secure_filename(
            f"{item_type}_{name_en.lower().replace(' ', '_')}_{datetime.now().strftime('%Y%m%d%H%M%S')}.webp"
        )
        r2 = get_r2_client()
        r2.upload_fileobj(buf, R2_BUCKET_NAME, fname, ExtraArgs={"ContentType": "image/webp"})
        image_url = f"{R2_PUBLIC_URL}/{fname}"

        # Compute original price: +25%, rounded up to nearest ₹5
        original_price = int((price * 1.25 + 4) // 5 * 5)

        # Append row to sheet
        sh     = get_sheet(SHEET_FISH)
        rows   = sh.get_all_values()
        row_id = len(rows)  # 1-based; header is row 1

        sh.append_row([
            str(row_id),
            name_ml,
            name_en,
            price,
            original_price,
            note,
            "TRUE",
            image_url,
            item_type,
        ])

        return jsonify({"ok": True, "row_id": str(row_id), "image_url": image_url})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/admin/toggle-fish", methods=["POST"])
def admin_toggle_fish():
    try:
        data      = request.get_json(force=True)
        row_id    = str(data.get("row_id", "")).strip()
        available = data.get("available", False)

        sh   = get_sheet(SHEET_FISH)
        rows = sh.get_all_values()

        for i, row in enumerate(rows):
            if row and str(row[COL_ROW_ID]).strip() == row_id:
                # Sheets rows are 1-indexed; i is 0-indexed
                sh.update_cell(i + 1, COL_AVAILABLE + 1, "TRUE" if available else "FALSE")
                return jsonify({"ok": True})

        return jsonify({"error": "Row not found"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/admin/deactivate-all", methods=["POST"])
def admin_deactivate_all():
    try:
        sh   = get_sheet(SHEET_FISH)
        rows = sh.get_all_values()
        updates = []
        for i, row in enumerate(rows):
            if not row or str(row[COL_ROW_ID]).strip() == "row_id":
                continue
            while len(row) <= COL_AVAILABLE:
                row.append("")
            available = str(row[COL_AVAILABLE]).strip().upper()
            if available in ("TRUE", "1", "YES"):
                updates.append({
                    "range":  f"G{i + 1}",
                    "values": [["FALSE"]],
                })
        if updates:
            sh.spreadsheet.values_batch_update({
                "valueInputOption": "RAW",
                "data": updates,
            })
        return jsonify({"ok": True, "deactivated": len(updates)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
