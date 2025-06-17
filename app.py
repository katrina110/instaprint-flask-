import os
from flask import (
    Flask,
    jsonify,
    render_template,
    request,
    send_from_directory,
    redirect,
    url_for,
)
import cv2
import numpy as np
import fitz  # PyMuPDF
from io import BytesIO # Import BytesIO for in-memory image handling
import pythoncom
from win32com import client
from fpdf import FPDF
import serial
import time
from datetime import datetime, timezone, timedelta
from PyPDF2 import PdfReader # Keep for potential future PDF manipulation
import threading
from flask_socketio import SocketIO, emit
import win32print
from flask_sqlalchemy import SQLAlchemy
import csv
import requests
from io import StringIO
import pywintypes # Ensure this is imported if you catch pywintypes.com_error
import wmi
import multiprocessing # This was listed twice, ensure it's needed or remove redundancy
import json # Import json for parsing printOptions
import re # Import regex for more robust parsing
import math
import win32api
from sqlalchemy import extract, func
from pathlib import Path
import base64
import shutil
import pytz

import smtplib
from email.mime.text import MIMEText

PH_TZ = pytz.timezone('Asia/Manila')

def current_ph_time():
    return datetime.now(PH_TZ)

app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///instaprint.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
socketio = SocketIO(app, cors_allowed_origins="*")

# --- Database Models ---
class Transaction(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    method = db.Column(db.String(50), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

class UploadedFile(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    filename = db.Column(db.String(255), nullable=False)
    file_type = db.Column(db.String(50), nullable=False)
    pages = db.Column(db.Integer, nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f'<UploadedFile {self.filename}>'

class ErrorLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    source = db.Column(db.String(100), nullable=True) # e.g., 'payment.html', 'printer_status', 'app.py_upload'
    message = db.Column(db.Text, nullable=False)
    details = db.Column(db.Text, nullable=True) # Optional: for stack traces or more info

    def __repr__(self):
        return f'<ErrorLog {self.timestamp} - {self.source}: {self.message[:50]}>'

# NEW: User model for registration
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    registration_date = db.Column(db.DateTime, default=current_ph_time)

    def __repr__(self):
        return f'<User {self.username}>'

UPLOAD_FOLDER = os.path.join(os.getcwd(), "uploads")
STATIC_FOLDER = os.path.join(os.getcwd(), "static", "uploads")
ALLOWED_EXTENSIONS = {"pdf", "jpg", "jpeg", "png", "docx", "doc"}
printer_name = None
logged_errors = set()  # Set to track logged errors to avoid flooding for some types of errors
last_status = None

# Store uploaded file info for admin view
uploaded_files_info = []

app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
app.config["STATIC_FOLDER"] = STATIC_FOLDER
app.config["ALLOWED_EXTENSIONS"] = {"pdf", "docx", "jpg", "png"}

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(STATIC_FOLDER, exist_ok=True)

THUMBNAIL_DIR = Path(app.static_folder) / "uploads" / "thumbnails"
os.makedirs(THUMBNAIL_DIR, exist_ok=True)

# Global Variables
printed_pages_today = 0
files_uploaded_today = 0
last_updated_date = datetime.now().date()
images = []  # Store images globally (consider refactoring for very large files)
total_pages = 0  # Keep track of total pages.

SMTP_SERVER = 'smtp.gmail.com'  # e.g., 'smtp.gmail.com'
SMTP_PORT = 587  # or 465 for SSL (check your SMTP provider's settings)
SMTP_USERNAME = 'instaprint.kiosk2025@gmail.com'
SMTP_PASSWORD = 'cduu biru upqc mmqh'
SENDER_EMAIL = 'instaprint.kiosk2025@gmail.com' # The email address from which the alerts will be sent

last_error_email_sent_time = None
ERROR_EMAIL_COOLDOWN_SECONDS = 60

last_alert_attempt_time = 0
ALERT_COOLDOWN_SECONDS = 300 # 5 minutes (adjust as needed)

def send_error_email_notification(error_message):
    global last_error_email_sent_time
    current_time = datetime.now(PH_TZ)

    # Check cooldown period to avoid flooding emails
    if last_error_email_sent_time and (current_time - last_error_email_sent_time).total_seconds() < ERROR_EMAIL_COOLDOWN_SECONDS:
        print("Skipping error email: within cooldown period.")
        return

    with app.app_context():
        try:
            users = User.query.all()
            if not users:
                print("No registered users found to send error email to.")
                return

            # Collect all unique valid email addresses from registered users
            recipient_emails = [user.email for user in users if user.email]
            if not recipient_emails:
                print("No valid email addresses found for registered users.")
                return

            # Construct the email content
            subject = "InstaPrint Kiosk Error"
            body = f"""Dear Admin,

There has been an error with the InstaPrint Kiosk. Please attend to it immediately.

Error Details:
{error_message}

Timestamp (PH Time): {current_time.strftime('%Y-%m-%d %I:%M:%S %p %Z%z')}
"""

            msg = MIMEText(body)
            msg['Subject'] = subject
            msg['From'] = SENDER_EMAIL
            msg['To'] = ", ".join(recipient_emails) # Join all recipient emails with a comma

            # Send the email using SMTP
            with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
                server.starttls()  # Start TLS encryption for secure communication
                server.login(SMTP_USERNAME, SMTP_PASSWORD)
                server.send_message(msg)

            last_error_email_sent_time = current_time # Update the last sent time after successful sending
            print(f"Error email sent successfully to: {', '.join(recipient_emails)}")

        except Exception as e:
            print(f"CRITICAL ERROR: Failed to send error email: {e}")

TARGET_SUBFOLDER_NAME = "InstaPrintDL"
try:
    documents_path = Path.home() / "Documents"
    DOWNLOADS_DIR = documents_path / TARGET_SUBFOLDER_NAME
    if not DOWNLOADS_DIR.exists():
        print(f"WARNING: The target folder '{DOWNLOADS_DIR}' does not currently exist. Please create it inside your Documents folder.")
    elif not DOWNLOADS_DIR.is_dir():
        print(f"WARNING: The path '{DOWNLOADS_DIR}' exists but is not a folder. Please ensure it's a directory inside your Documents folder.")
    else:
        print(f"Target folder for downloads and clearing is set to: {DOWNLOADS_DIR}")

except Exception as e:
    print(f"CRITICAL ERROR: Could not determine the path for '{TARGET_SUBFOLDER_NAME}' in the Documents folder: {e}")
    print("Please ensure your environment allows access to the home directory and Documents folder.")
    DOWNLOADS_DIR = Path("./INSTAPRINT_DOCUMENTS_FOLDER_PATH_ERROR") # Emergency fallback
    print(f"Using an emergency fallback path due to error: {DOWNLOADS_DIR}")

# --- SVG Icon Definitions (from testest.py) ---
def create_svg_data_url(svg_xml_content):
    encoded_svg = base64.b64encode(svg_xml_content.encode('utf-8')).decode('utf-8')
    return f"data:image/svg+xml;base64,{encoded_svg}"

SVG_ICONS = {
    ".pdf": create_svg_data_url("""<svg width="48" height="48" viewBox="0 0 48 48" xmlns="http://www.w3.org/2000/svg"><rect x="6" y="2" width="36" height="44" rx="3" fill="#E53935"/><text x="24" y="30" font-family="sans-serif" font-weight="bold" font-size="12" fill="#FFFFFF" text-anchor="middle">PDF</text></svg>"""),
    ".docx": create_svg_data_url("""<svg width="48" height="48" viewBox="0 0 48 48" xmlns="http://www.w3.org/2000/svg"><rect x="6" y="2" width="36" height="44" rx="3" fill="#1E88E5"/><line x1="12" y1="14" x2="36" y2="14" stroke="#FFFFFF" stroke-width="2"/><line x1="12" y1="22" x2="36" y2="22" stroke="#FFFFFF" stroke-width="2"/><line x1="12" y1="30" x2="28" y2="30" stroke="#FFFFFF" stroke-width="2"/></svg>"""),
    ".doc": create_svg_data_url("""<svg width="48" height="48" viewBox="0 0 48 48" xmlns="http://www.w3.org/2000/svg"><rect x="6" y="2" width="36" height="44" rx="3" fill="#1E88E5"/><text x="24" y="30" font-family="sans-serif" font-weight="bold" font-size="10" fill="#FFFFFF" text-anchor="middle">DOC</text></svg>"""),
    ".jpg": create_svg_data_url("""<svg width="48" height="48" viewBox="0 0 48 48" xmlns="http://www.w3.org/2000/svg"><rect x="6" y="6" width="36" height="36" rx="3" fill="#FB8C00"/><circle cx="16" cy="16" r="4" fill="#FFFFFF"/><polygon points="14 38, 24 24, 34 30, 34 42, 6 42, 6 38" fill="#FDD835"/></svg>"""),
    ".png": create_svg_data_url("""<svg width="48" height="48" viewBox="0 0 48 48" xmlns="http://www.w3.org/2000/svg"><rect x="6" y="6" width="36" height="36" rx="3" fill="#43A047"/><path d="M16 16 L24 24 L32 16 L24 32 Z" fill="#FFFFFF" stroke="#FFFFFF" stroke-width="1"/></svg>"""),
    "default_file": create_svg_data_url("""<svg width="48" height="48" viewBox="0 0 48 48" xmlns="http://www.w3.org/2000/svg"><path d="M12 2 H28 L40 14 V42 C40 43.1 39.1 44 38 44 H12 C10.9 44 10 43.1 10 42 V6 C10 3.9 10.9 2 12 2 Z" fill="#B0BEC5"/><path d="M28 2 V14 H40" fill="#78909C"/></svg>""")
}
SVG_ICONS['.jpeg'] = SVG_ICONS['.jpg']
for ext in ALLOWED_EXTENSIONS:
    dot_ext = "." + ext
    if dot_ext not in SVG_ICONS:
        SVG_ICONS[dot_ext] = SVG_ICONS["default_file"]

def generate_thumbnail_for_file(item_path_obj: Path, file_ext_without_dot: str, unique_thumb_filename: str, app_config_upload_folder: str):
    thumbnail_save_path = THUMBNAIL_DIR / unique_thumb_filename
    temp_pdf_path_for_docx = None

    try:
        if file_ext_without_dot == "pdf":
            doc = None
            try:
                doc = fitz.open(str(item_path_obj))
                if len(doc) > 0:
                    page = doc.load_page(0)
                    pix = page.get_pixmap(alpha=False, dpi=50)
                    pix.save(str(thumbnail_save_path))
            finally:
                if doc: doc.close()

        elif file_ext_without_dot in ("jpg", "png"):
            img = cv2.imread(str(item_path_obj))
            if img is not None:
                target_width = 80
                height, width = img.shape[:2]
                if width == 0:
                    print(f"Warning: Image {item_path_obj.name} has zero width.")
                    return None
                target_height = int((target_width / width) * height)
                if target_height > 0 and target_width > 0 :
                    resized_img = cv2.resize(img, (target_width, target_height), interpolation=cv2.INTER_AREA)
                    cv2.imwrite(str(thumbnail_save_path), resized_img)
                else:
                    print(f"Warning: Could not calculate valid thumbnail dimensions for {item_path_obj.name}.")
                    return None
            else:
                print(f"Warning: cv2.imread failed for {item_path_obj.name}.")
                return None

        elif file_ext_without_dot == "docx":
            base_name_for_temp_pdf = item_path_obj.stem
            upload_folder_path = Path(app_config_upload_folder)
            temp_pdf_filename = f"temp_thumb_pdf_{base_name_for_temp_pdf}_{datetime.now().strftime('%Y%m%d%H%M%S%f')}.pdf"
            temp_pdf_path_for_docx = upload_folder_path / temp_pdf_filename
            
            abs_item_path = str(item_path_obj.resolve())
            abs_temp_pdf_path = str(temp_pdf_path_for_docx.resolve())

            pythoncom.CoInitialize()
            word_app = None
            word_doc = None
            pdf_doc_for_thumb = None
            try:
                word_app = client.Dispatch("Word.Application")
                word_app.visible = False
                word_doc = word_app.Documents.Open(abs_item_path)
                word_doc.SaveAs(abs_temp_pdf_path, FileFormat=17)
            finally:
                if word_doc: word_doc.Close(False)
                if word_app: word_app.Quit()
                pythoncom.CoUninitialize()

            if temp_pdf_path_for_docx.exists():
                try:
                    pdf_doc_for_thumb = fitz.open(str(temp_pdf_path_for_docx))
                    if len(pdf_doc_for_thumb) > 0:
                        page = pdf_doc_for_thumb.load_page(0)
                        pix = page.get_pixmap(alpha=False, dpi=50)
                        pix.save(str(thumbnail_save_path))
                finally:
                    if pdf_doc_for_thumb: pdf_doc_for_thumb.close()
        
        if thumbnail_save_path.exists() and thumbnail_save_path.stat().st_size > 0:
            return url_for('static', filename=f'uploads/thumbnails/{unique_thumb_filename}', _external=False)
        else: 
            if thumbnail_save_path.exists():
                try:
                    os.remove(str(thumbnail_save_path))
                except Exception as e_rem:
                    print(f"Warning: Could not remove empty/failed thumbnail {thumbnail_save_path}: {e_rem}")
            return None

    except Exception as e:
        print(f"ERROR generating thumbnail for {item_path_obj.name}: {e}")
        return None
    finally:
        if temp_pdf_path_for_docx and temp_pdf_path_for_docx.exists():
            try:
                os.remove(str(temp_pdf_path_for_docx))
            except Exception as e_clean:
                print(f"ERROR cleaning up temp PDF {temp_pdf_path_for_docx} for thumbnail: {e_clean}")

def log_error_to_db(message, source=None, details=None):
    with app.app_context():
        try:
            alert_timestamp_str = datetime.now().strftime("%m-%d-%Y %I:%M:%S %p")

            error_entry = ErrorLog(
                message=str(message),
                source=str(source) if source else "Unknown",
                details=str(details) if details else None,
                timestamp=datetime.utcnow()
            )
            db.session.add(error_entry)
            db.session.commit()
            print(f"DB_ERROR_LOGGED: [{source or 'GENERAL'}] {message}")

            email_detail_message = f"Source: {source or 'Unknown'}\nMessage: {message}\nDetails: {details or 'No additional details.'}"
            send_error_email_notification(email_detail_message)

        except Exception as e:
            try:
                db.session.rollback()
            except Exception as rb_e:
                print(f"CRITICAL_ERROR: Failed to rollback DB session during log_error_to_db failure: {rb_e}")

            alert_timestamp_db_fail_str = datetime.now().strftime("%Y-%m-%d %I:%M:%S %p")
            print(f"CRITICAL_ERROR: Failed to log error to DB: {e}. Original error: [{source}] {message}")
            email_detail_message_fallback = f"CRITICAL DB LOGGING FAILED. Original Error: Source: {source or 'Unknown'}, Message: {message}, DB Error: {e}"
            send_error_email_notification(email_detail_message_fallback)

def record_transaction(method, amount):
    try:
        transaction = Transaction(method=method, amount=amount, timestamp=datetime.utcnow()) # Use UTC now
        db.session.add(transaction)
        db.session.commit()
        # Emit to update admin dashboard or transaction logs in real-time
        socketio.emit('new_transaction', {
            'method': method,
            'amount': amount,
            'timestamp': transaction.timestamp.replace(tzinfo=timezone.utc).astimezone(timezone(timedelta(hours=8))).strftime('%Y-%m-%d %I:%M:%S %p') # Convert to PHT for display
        })
        print(f"Transaction Recorded: {method} - ₱{amount:.2f}")
    except Exception as e:
        db.session.rollback()
        error_msg = f"Failed to record transaction: {e}"
        print(error_msg)
        log_error_to_db(error_msg, source="record_transaction", details=str(e))
    
@app.route('/api/clear_downloads_folder', methods=['POST'])
def clear_downloads_folder_route():
    global DOWNLOADS_DIR
    print(f"[SERVER LOG /api/clear_downloads_folder] Route called.")
    print(f"[SERVER LOG /api/clear_downloads_folder] DOWNLOADS_DIR is currently: '{DOWNLOADS_DIR}' (Type: {type(DOWNLOADS_DIR)})")
    
    if not DOWNLOADS_DIR or not DOWNLOADS_DIR.exists() or not DOWNLOADS_DIR.is_dir():
        error_msg = f"Target directory '{DOWNLOADS_DIR}' for listing is invalid, does not exist, or is not a directory."
        print(f"[DEBUG /api/list_recent_downloads] ERROR: {error_msg}")
        return jsonify({"error": error_msg, "files": []}), 500

    if not DOWNLOADS_DIR.exists() or not DOWNLOADS_DIR.is_dir():
        error_msg = f"Downloads directory ('{DOWNLOADS_DIR}') not configured, does not exist, or is not a directory."
        print(f"[SERVER LOG /api/clear_downloads_folder] ERROR: {error_msg}")
        return jsonify({"success": False, "message": error_msg}), 500

    print(f"[SERVER LOG /api/clear_downloads_folder] Attempting to clear contents of: {DOWNLOADS_DIR}")
    deleted_count = 0
    error_count = 0
    errors_list = []

    try:
        dir_items = os.listdir(DOWNLOADS_DIR)
        print(f"[SERVER LOG /api/clear_downloads_folder] Items in directory: {dir_items}")
    except Exception as e_list:
        error_msg = f"Failed to list directory contents for '{DOWNLOADS_DIR}': {e_list}"
        print(f"[SERVER LOG /api/clear_downloads_folder] ERROR: {error_msg}")
        return jsonify({"success": False, "message": error_msg}), 500
        
    if not dir_items:
        print(f"[SERVER LOG /api/clear_downloads_folder] Directory '{DOWNLOADS_DIR}' is already empty.")
        return jsonify({
            "success": True, 
            "message": f"Downloads folder '{DOWNLOADS_DIR}' was already empty or contained no items to delete."
        }), 200

    for item_name in dir_items:
        item_path = DOWNLOADS_DIR / item_name
        print(f"[SERVER LOG /api/clear_downloads_folder] Processing item: {item_path}")
        try:
            if item_path.is_file() or item_path.is_link():
                os.remove(item_path)
                print(f"[SERVER LOG /api/clear_downloads_folder]   Deleted file/link: {item_path}")
                deleted_count += 1
            elif item_path.is_dir():
                shutil.rmtree(item_path)
                print(f"[SERVER LOG /api/clear_downloads_folder]   Deleted directory: {item_path}")
                deleted_count += 1
            else:
                print(f"[SERVER LOG /api/clear_downloads_folder]   Skipped (not a file, link, or dir): {item_path}")

        except Exception as e_delete:
            error_str = f"Failed to delete {item_path}: {e_delete}"
            print(f"[SERVER LOG /api/clear_downloads_folder]   ERROR: {error_str}")
            errors_list.append(error_str)
            error_count += 1

    if error_count > 0:
        print(f"[SERVER LOG /api/clear_downloads_folder] Finished with errors. Deleted: {deleted_count}, Failed: {error_count}")
        return jsonify({
            "success": False, 
            "message": f"Finished clearing downloads. Deleted {deleted_count} items. Encountered {error_count} errors.",
            "errors": errors_list
        }), 207 
    
    print(f"[SERVER LOG /api/clear_downloads_folder] Successfully cleared {deleted_count} items from {DOWNLOADS_DIR}.")
    return jsonify({
        "success": True, 
        "message": f"Successfully cleared {deleted_count} items from {DOWNLOADS_DIR}."
    }), 200

@app.route('/api/list_recent_downloads', methods=['GET'])
def list_recent_downloads():
    try:
        global DOWNLOADS_DIR
        if not DOWNLOADS_DIR.is_dir():
             raise FileNotFoundError("Downloads directory not found or is not a directory.")
    except Exception as e:
        print(f"CRITICAL: Could not define/access DOWNLOADS_DIR: {e}")
        log_error_to_db(f"CRITICAL: Could not define/access DOWNLOADS_DIR: {e}", "list_recent_downloads_setup")
        return jsonify({"error": "Server configuration error for downloads directory.", "files": []}), 500

    recent_files_data = []
    try:
        all_items_in_dir = os.listdir(DOWNLOADS_DIR)
        files_to_consider = []
        current_time = datetime.now()

        for item_name in all_items_in_dir:
            item_path = DOWNLOADS_DIR / item_name
            if item_path.is_file():
                file_extension_without_dot = item_name.rsplit('.', 1)[-1].lower() if '.' in item_name else ''
                if file_extension_without_dot in app.config["ALLOWED_EXTENSIONS"]:
                    try:
                        mtime_timestamp = os.path.getmtime(item_path)
                        mtime_datetime = datetime.fromtimestamp(mtime_timestamp)

                        if current_time - mtime_datetime <= timedelta(seconds=10):
                            files_to_consider.append({'path': item_path, 'name': item_name, 'mtime': mtime_timestamp})

                    except Exception as e_stat:
                        log_error_to_db(f"Could not get stat for file {item_path}: {e_stat}", source="list_recent_downloads_stat")
                        print(f"Warning: Could not get stat for file {item_path}: {e_stat}")
                        pass

        sorted_files = sorted(files_to_consider, key=lambda x: x['mtime'], reverse=True)

        for f_info in sorted_files:
            item_path_obj = f_info['path']
            original_filename = f_info['name']
            file_ext_with_dot_for_svg = ('.' + original_filename.rsplit('.', 1)[-1].lower()) if '.' in original_filename else '.default_file'
            file_ext_without_dot = file_ext_with_dot_for_svg.lstrip('.')

            thumb_base_name = Path(original_filename).stem
            thumb_mtime_str = datetime.fromtimestamp(f_info['mtime']).strftime('%Y%m%d%H%M%S')
            preview_image_filename = f"thumb_{thumb_base_name}_{thumb_mtime_str}.png"

            preview_url = generate_thumbnail_for_file(item_path_obj, file_ext_without_dot, preview_image_filename, app.config["UPLOAD_FOLDER"])

            if not preview_url:
                preview_url = SVG_ICONS.get(file_ext_with_dot_for_svg, SVG_ICONS.get("default_file", url_for('static', filename='images/default_icon.png')))


            recent_files_data.append({
                "filename": original_filename,
                "preview_url": preview_url,
                "mtime_readable": datetime.fromtimestamp(f_info['mtime']).strftime('%I:%M %p, %b %d')
            })

    except Exception as e:
        log_error_to_db(f"Error listing recent downloads: {e}", source="list_recent_downloads_general", details=str(e))
        print(f"ERROR listing recent downloads: {e}")
        return jsonify({"error": "An error occurred while listing files.", "files": []}), 500

    return jsonify({"files": recent_files_data})

@app.route("/process_downloaded_file", methods=["POST"])
def process_downloaded_file():
    global images, total_pages
    global printed_pages_today, files_uploaded_today, last_updated_date

    data = request.get_json()
    if not data or "selected_filename" not in data:
        log_error_to_db("No filename provided for processing downloaded file.", source="process_downloaded_file_payload")
        return jsonify({"error": "No filename provided."}), 400

    selected_filename = data["selected_filename"]
    
    try:
        filepath = (DOWNLOADS_DIR / selected_filename).resolve()
        if not filepath.is_file() or not filepath.is_relative_to(DOWNLOADS_DIR.resolve()):
            log_error_to_db(f"Forbidden access attempt for downloaded file: {selected_filename}", source="process_downloaded_file_security")
            return jsonify({"error": "Invalid file selection or access denied."}), 403
    except Exception as path_e:
        log_error_to_db(f"Path resolution error for {selected_filename}: {path_e}", source="process_downloaded_file_path_resolve")
        return jsonify({"error": "Error resolving file path."}), 500


    original_filename = selected_filename
    file_ext_without_dot = original_filename.rsplit(".", 1)[-1].lower() if '.' in original_filename else ''


    if datetime.now().date() != last_updated_date:
        printed_pages_today = 0
        files_uploaded_today = 0
        last_updated_date = datetime.now().date()

    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    
    final_filepath_for_processing = filepath
    final_filename_for_info = original_filename
    final_file_ext_for_info = file_ext_without_dot

    processed_images = []
    calculated_total_pages = 0
    
    images = []

    try:
        if file_ext_without_dot == "pdf":
            print(f"Processing selected PDF: {original_filename}")
            
            # NEW: Copy the PDF to UPLOAD_FOLDER with a unique name for printing consistency
            pdf_output_filename = f"print_ready_{timestamp}_{original_filename}"
            pdf_output_path = os.path.join(UPLOAD_FOLDER, pdf_output_filename)
            
            # Copy the file from DOWNLOADS_DIR to UPLOAD_FOLDER
            shutil.copy(str(final_filepath_for_processing), pdf_output_path)
            print(f"Copied original PDF to UPLOAD_FOLDER for printing: {pdf_output_path}")

            # Now process this copied PDF into images for preview/page counting
            processed_images = pdf_to_images(pdf_output_path)
            calculated_total_pages = len(processed_images)

            # Update final info to reflect the new file in UPLOAD_FOLDER
            final_filename_for_info = pdf_output_filename
            final_file_ext_for_info = "pdf"
            final_filepath_for_processing = pdf_output_path


        elif file_ext_without_dot in ("jpg", "jpeg", "png"):
            print(f"Processing selected Image: {original_filename}")
            
            pdf_output_filename = f"{timestamp}_{original_filename.rsplit('.', 1)[0]}.pdf"
            pdf_output_path = os.path.join(UPLOAD_FOLDER, pdf_output_filename)

            print(f"Converting selected image {original_filename} to PDF: {pdf_output_path}")
            image_to_pdf_fit_page(str(final_filepath_for_processing), pdf_output_path)

            processed_images = pdf_to_images(pdf_output_path)
            calculated_total_pages = len(processed_images)
            
            final_filename_for_info = pdf_output_filename
            final_file_ext_for_info = "pdf"
            final_filepath_for_processing = pdf_output_path

        elif file_ext_without_dot in ("doc", "docx"):
            print(f"Processing selected Word Document: {original_filename}")
            temp_doc_path_in_uploads = os.path.join(UPLOAD_FOLDER, f"{timestamp}_{original_filename}")
            shutil.copy(str(final_filepath_for_processing), temp_doc_path_in_uploads)
            processed_images, converted_pdf_path = docx_to_images(temp_doc_path_in_uploads)
            calculated_total_pages = len(processed_images)
            if converted_pdf_path and os.path.exists(converted_pdf_path):
                final_filename_for_info = os.path.basename(converted_pdf_path)
                final_file_ext_for_info = "pdf"
                final_filepath_for_processing = converted_pdf_path
                os.remove(temp_doc_path_in_uploads)
            else:
                if os.path.exists(temp_doc_path_in_uploads):
                    os.remove(temp_doc_path_in_uploads)
                raise Exception("Word to PDF conversion failed or PDF path not returned.")
        else:
            raise Exception(f"Unsupported file type selected: {file_ext_without_dot}")

        if not processed_images:
            raise Exception("File processing resulted in no images.")

        images = processed_images
        total_pages = calculated_total_pages;
        files_uploaded_today += 1

        try:
            new_uploaded_file_entry = UploadedFile(
                filename=final_filename_for_info,
                file_type=final_file_ext_for_info,
                pages=total_pages,
                timestamp=datetime.now()
            )
            db.session.add(new_uploaded_file_entry)
            db.session.commit()
            print(f"File info for {final_filename_for_info} (orig: {original_filename}) saved to database.")
        except Exception as e_db:
            db.session.rollback()
            log_error_to_db(f"Error saving file info to database for {final_filename_for_info}: {e_db}", source="process_downloaded_file_db")

        print(f"Selected downloaded file processed successfully: {final_filename_for_info} (from {original_filename})")
        return jsonify({
            "message": "File processed successfully!",
            "fileName": final_filename_for_info, # Ensure this is the name of the file in UPLOAD_FOLDER
            "totalPages": total_pages
        }), 200

    except Exception as e_proc:
        log_error_to_db(f"Error processing selected downloaded file {original_filename}: {e_proc}", source="process_downloaded_file_main_try", details=str(e_proc))
        if final_filename_for_info != original_filename and os.path.exists(final_filepath_for_processing):
            try:
                os.remove(final_filepath_for_processing)
                print(f"Cleaned up intermediate file on error: {final_filepath_for_processing}")
            except Exception as e_clean:
                log_error_to_db(f"Error cleaning up intermediate file {final_filepath_for_processing}: {e_clean}", source="process_downloaded_file_cleanup")
        return jsonify({"error": f"Failed to process selected file: {str(e_proc)}"}), 500

@app.route("/set_price", methods=["POST"])
def set_price():
    data = request.get_json() or {}
    price = int(data.get("price", 0))
        
# Helper Functions
def allowed_file(filename):
    return (
        "." in filename
        and filename.rsplit(".", 1)[1].lower() in app.config["ALLOWED_EXTENSIONS"]
    )

def image_to_pdf_fit_page(image_path, output_pdf_path, page_size="Short"):
    page_sizes = {
        "Short": (216, 279),  # US Letter approx (8.5 x 11 in)
    }

    if page_size not in page_sizes:
        page_size = "Short"  # Default fallback

    page_w_mm, page_h_mm = page_sizes[page_size]

    img = cv2.imread(image_path)
    if img is None:
        raise ValueError(f"Failed to load image for PDF conversion: {image_path}")

    img_h_px, img_w_px = img.shape[:2]

    dpi = 96
    px_to_mm = 25.4 / dpi

    img_w_mm = img_w_px * px_to_mm
    img_h_mm = img_h_px * px_to_mm

    scale = min(page_w_mm / img_w_mm, page_h_mm / img_h_mm)

    pdf_img_w = img_w_mm * scale
    pdf_img_h = img_h_mm * scale

    x_pos = (page_w_mm - pdf_img_w) / 2
    y_pos = (page_h_mm - pdf_img_h) / 2

    pdf = FPDF(unit="mm", format=(page_w_mm, page_h_mm))
    pdf.add_page()
    pdf.image(image_path, x=x_pos, y=y_pos, w=pdf_img_w, h=pdf_img_h)
    pdf.output(output_pdf_path)


def pdf_to_images(pdf_path):
    document = None
    try:
        document = fitz.open(pdf_path)
        pdf_images = []
        for page_num in range(len(document)):
            page = document.load_page(page_num)
            pix = page.get_pixmap(alpha=False)
            image = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
                (pix.height, pix.width, pix.n)
            )
            if pix.n == 3:
                 image = cv2.cvtColor(image, cv2.COLOR_RGB2BGR)
            elif pix.n == 4:
                 image = cv2.cvtColor(image, cv2.COLOR_RGBA2BGR)

            pdf_images.append(image)
        return pdf_images
    except Exception as e:
        print(f"Error processing PDF: {e}")
        return []
    finally:
        if document:
            document.close()

def docx_to_images(file_path):
    if not file_path.lower().endswith((".doc", ".docx")):
        raise ValueError("Unsupported file type for Word documents.")

    pdf_path = None
    try:
        pythoncom.CoInitialize()
        word = client.Dispatch("Word.Application")
        word.visible = False
        doc = word.Documents.Open(file_path)

        pdf_path = file_path.rsplit(".", 1)[0] + ".pdf"
        doc.SaveAs(pdf_path, FileFormat=17)
        doc.Close()
        word.Quit()

        images = pdf_to_images(pdf_path)
        return images, pdf_path

    except Exception as e:
        print(f"Error processing Word document: {e}")
        return [], None

    finally:
        pythoncom.CoUninitialize()

def parse_page_selection(selection, total_pages):
    pages = set()
    if not selection:
        return list(range(total_pages))

    parts = re.split(r",\s*", selection)
    for part in parts:
        if "-" in part:
            try:
                start, end = map(int, part.split("-"))
                start = max(1, start)
                end = min(total_pages, end)
                pages.update(range(start - 1, end))
            except ValueError:
                continue
        elif part.isdigit():
            page_num = int(part)
            if 1 <= page_num <= total_pages:
                pages.add(page_num - 1)
        else:
            continue
    return sorted(p for p in pages if 0 <= p < total_pages)

def highlight_bright_pixels(image, dark_threshold=50):
    if image is None or len(image.shape) < 3 or image.shape[2] != 3:
         print("Error: Invalid image format for highlighting.")
         return None

    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    dark_mask = gray < dark_threshold
    result = np.ones_like(image, dtype=np.uint8) * 255
    result[~dark_mask] = image[~dark_mask]
    return result

def process_image(image, page_size="Short", color_option="Color"):
    if image is None:
        print("Error: Unable to load image for processing.")
        return None, 0.0

    paper_cost_per_page = {
        "Short": 2.0,
    }
    default_paper_cost = 2.0 # Fallback to Short's cost
    paper_cost = paper_cost_per_page.get(page_size, default_paper_cost)

    max_price_cap_per_page = {
        "Short": 15.0,
    }
    default_max_cap = 15.0 # Fallback to Short's cap
    max_cap = max_price_cap_per_page.get(page_size, default_max_cap)

    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    total_pixels = gray.size

    white_ratio = np.sum(gray > 220) / total_pixels
    dark_text_ratio = np.sum((gray > 10) & (gray < 180)) / total_pixels

    if white_ratio > 0.85 and dark_text_ratio < 0.15:
        text_only_prices = {"Short": 3.0}
        return image, text_only_prices.get(page_size, 3.0)

    if np.mean(gray) > 240:
         return image, 0.0

    if np.mean(gray) < 15:
        base_rate = 0.000006
        base_cost = total_pixels * base_rate + paper_cost
        return image, round(min(base_cost * 1.5, max_cap), 2)

    if color_option.lower() == "grayscale":
        edges = cv2.Canny(gray, 50, 150)
        edge_density = np.sum(edges > 0) / total_pixels
        dark_ratio = np.sum(gray < 100) / total_pixels
        hist = cv2.calcHist([gray], [0], None, [256], [0, 256])
        hist_norm = hist.ravel() / (hist.sum() + 1e-7)
        entropy = -np.sum(hist_norm * np.log2(hist_norm + 1e-7))

        if white_ratio > 0.85 and dark_text_ratio < 0.15:
            base_price = 1.0
        elif edge_density < 0.004 and entropy < 5.0:
            base_price = 2.0
        elif edge_density < 0.01 and entropy < 5.5:
            base_price = 3.0
        elif edge_density < 0.02 and entropy < 6.0:
            base_price = 4.0
        else:
            base_price = 6.0 if dark_ratio > 0.2 else 5.0

        final_price = base_price + paper_cost
        return image, round(min(final_price, max_cap), 2)

    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    color_mask = cv2.inRange(hsv, np.array([0, 50, 50]), np.array([180, 255, 255]))
    color_pixel_count = cv2.countNonZero(color_mask)
    color_coverage = color_pixel_count / total_pixels

    if color_coverage == 0:
        base_price = 1.0
    elif color_coverage < 0.015:
        base_price = 2.0
    elif color_coverage < 0.25:
        base_price = 3.0
    else:
        base_price = 4.0

    final_price = (base_price + paper_cost) * 1.5
    return image, round(min(final_price, max_cap), 2)


def get_printer_status_wmi():
    global printer_name, logged_errors
    c = wmi.WMI()
    logs_for_immediate_return = []
    now = datetime.now()

    status_map = {
        1: "Other", 2: "Unknown", 3: "Idle", 4: "Printing",
        5: "Warming Up", 6: "Stopped Printing", 7: "Offline"
    }
    error_state_map = {
        0: "Unknown", 1: "Other", 2: "No Paper", 3: "No Toner",
        4: "Door Open", 5: "Jammed", 6: "Service Requested", 7: "Output Bin Full"
    }

    try:
        printers_found = c.Win32_Printer(Name=printer_name)
        if not printers_found:
            msg = f"Printer '{printer_name}' not found by WMI."
            if "printer_not_found" not in logged_errors:
                print(msg)
                log_error_to_db(msg, source="get_printer_status_wmi_not_found")
                logs_for_immediate_return.append({"date": now.strftime("%m/%d/%Y"), "time": now.strftime("%I:%M %p"), "event": msg})
                logged_errors.add("printer_not_found")
            return logs_for_immediate_return

        for printer in printers_found:
            status_msg_from_map = status_map.get(printer.PrinterStatus, "Unknown")
            error_code = printer.DetectedErrorState
            error_msg_from_map = error_state_map.get(error_code, None)
            current_error_key = f"printer_{printer_name}_error_{error_code}"
            current_status_key = f"printer_{printer_name}_status_{printer.PrinterStatus}"

            if error_code != 0 and error_msg_from_map and error_msg_from_map != "Unknown":
                full_error_message = f"Printer '{printer_name}': {error_msg_from_map} (Device Status: {status_msg_from_map})"
                if current_error_key not in logged_errors:
                    print(f"LOGGING NEW PRINTER ERROR: {full_error_message}")
                    log_error_to_db(message=full_error_message, source="get_printer_status_wmi_error")
                    logs_for_immediate_return.append({"date": now.strftime("%m/%d/%Y"), "time": now.strftime("%I:%M %p"), "event": full_error_message})
                    logged_errors.add(current_error_key)
            elif error_code == 0 and current_error_key in logged_errors:
                resolved_msg = f"Printer '{printer_name}' error (was code {error_code}) appears resolved. Current status: {status_msg_from_map}."
                print(resolved_msg)
                logged_errors.remove(current_error_key)
            
            general_status_event = f"Printer '{printer_name}': {status_msg_from_map}"
            if status_msg_from_map not in ["Idle", "Unknown"] and not (error_code != 0 and error_msg_from_map and error_msg_from_map != "Unknown"):
                if current_status_key not in logged_errors:
                    logs_for_immediate_return.append({"date": now.strftime("%m/%d/%Y"), "time": now.strftime("%I:%M %p"), "event": general_status_event})
                    logged_errors.add(current_status_key)
            elif status_msg_from_map == "Idle" and current_status_key in logged_errors:
                 logged_errors.remove(current_status_key)

    except pywintypes.com_error as com_e:
        error_message = f"WMI COM Error accessing printer '{printer_name}': {com_e}"
        if "wmi_com_error" not in logged_errors:
            print(error_message)
            log_error_to_db(message=error_message, source="get_printer_status_wmi_com_error", details=str(com_e))
            logs_for_immediate_return.append({"date": now.strftime("%m/%d/%Y"), "time": now.strftime("%I:%M %p"), "event": error_message})
            logged_errors.add("wmi_com_error")
    except Exception as e:
        error_message = f"WMI general error for printer '{printer_name}': {e}"
        if "wmi_general_error" not in logged_errors:
            print(error_message)
            log_error_to_db(message=error_message, source="get_printer_status_wmi_general_error", details=str(e))
            logs_for_immediate_return.append({"date": now.strftime("%m/%d/%Y"), "time": now.strftime("%I:%M %p"), "event": error_message})
            logged_errors.add("wmi_general_error")
            
    return logs_for_immediate_return

@app.route("/")
def admin_user():
    # This line was changed to open file-upload.html as the default page
    return render_template("file-upload.html")

@app.route("/admin-dashboard")
def admin_dashboard():
    # Fetch all users from the database to display
    users = User.query.order_by(User.registration_date.desc()).all()
    # Prepare data for rendering
    user_data = []
    for user in users:
        user_data.append({
            'id': user.id,
            'username': user.username,
            'email': user.email,
            'registration_date': user.registration_date.strftime('%Y-%m-%d %H:%M:%S')
        })
    return render_template("admin-dashboard.html", users=user_data)

from flask import request, jsonify
from datetime import datetime

@app.route("/register_user", methods=["POST"])
def register_user():
    data = request.get_json()
    username = data.get("username")
    email = data.get("email")

    if not username or not email:
        return jsonify({"success": False, "message": "Username and email are required."}), 400

    existing_user_username = User.query.filter_by(username=username).first()
    existing_user_email = User.query.filter_by(email=email).first()

    if existing_user_username:
        return jsonify({"success": False, "message": "Username already exists."}), 409
    if existing_user_email:
        return jsonify({"success": False, "message": "Email already exists."}), 409

    try:
        new_user = User(username=username, email=email)
        db.session.add(new_user)
        db.session.commit()

        # Convert datetime to local time if needed
        local_time = new_user.registration_date
        formatted_time = local_time.strftime('%m-%d-%Y %I:%M:%S %p')  # 12-hour with AM/PM

        user_data = {
            "id": new_user.id,
            "username": new_user.username,
            "email": new_user.email,
            "registration_date": formatted_time
        }

        socketio.emit('new_user', user_data)

        return jsonify({
            "success": True,
            "message": "User registered successfully!",
            "user": user_data
        }), 201

    except Exception as e:
        db.session.rollback()
        log_error_to_db(
            f"Error registering user: {e}",
            source="register_user_route",
            details=str(e)
        )
        return jsonify({
            "success": False,
            "message": "An error occurred during registration."
        }), 500

@app.route("/delete_user", methods=["POST"])
def delete_user():
    data = request.get_json()
    user_id = data.get("id")

    if not user_id:
        return jsonify({"success": False, "message": "User ID is required."}), 400

    try:
        user_to_delete = User.query.get(user_id)
        if user_to_delete:
            db.session.delete(user_to_delete)
            db.session.commit()
            return jsonify({"success": True, "message": "User deleted successfully."}), 200
        else:
            return jsonify({"success": False, "message": "User not found."}), 404
    except Exception as e:
        db.session.rollback()
        log_error_to_db(f"Error deleting user with ID {user_id}: {e}", source="delete_user_route", details=str(e))
        return jsonify({"success": False, "message": "An error occurred during deletion."}), 500

@app.route("/admin-files-upload")
def admin_printed_pages():
    all_uploaded_files = UploadedFile.query.order_by(UploadedFile.timestamp.desc()).all() #
    
    formatted_files = []
    for uploaded_file_entry in all_uploaded_files:
        formatted_files.append({
            'file': uploaded_file_entry.filename, #
            'type': uploaded_file_entry.file_type, #
            'pages': uploaded_file_entry.pages, #
            'timestamp_full': uploaded_file_entry.timestamp.strftime('%b %d, %Y, %I:%M %p')  #
        })
        
    return render_template("admin-files-upload.html", uploaded=formatted_files) #

@app.route("/admin-activity-log")
def admin_activity_log():
    # This route refers to a global `activity_log` which was removed.
    # It should now query `ErrorLog` from the database.
    # For simplicity, I'm keeping the old reference, but this needs an update
    # to use `get_error_logs_data_for_admin` or similar DB query.
    # return render_template('admin-activity-log.html', printed=activity_log)
    return render_template('admin-activity-log.html', printed=[]) # Pass empty for now


activity_logs = [] # This global is unused if errors are logged to DB.

@app.route('/log_activity', methods=['POST'])
def log_activity_from_frontend_route():
    data = request.get_json()
    message = data.get("message")
    source = data.get("source", "frontend_general")
    details = data.get("details")

    if not message:
        print(f"Received log_activity request with no message from {source}")
        return jsonify(success=False, error="No message provided"), 400

    log_error_to_db(message=message, source=source, details=details)
    return jsonify(success=True, message="Error logged to database.")

@app.route('/admin_activity_data')
def get_error_logs_data_for_admin():
    try:
        error_entries = ErrorLog.query.order_by(ErrorLog.timestamp.desc()).limit(200).all() #
        
        logs_data = []
        # Define the PHT timezone (UTC+8)
        pht = timezone(timedelta(hours=8))

        for entry in error_entries:
            # entry.timestamp is a naive datetime object representing UTC
            # Make it an aware datetime object (aware of its UTC timezone)
            aware_utc_timestamp = entry.timestamp.replace(tzinfo=timezone.utc)
            # Convert the UTC timestamp to PHT
            pht_timestamp = aware_utc_timestamp.astimezone(pht)
            
            logs_data.append({
                'id': entry.id, #
                'timestamp': pht_timestamp.strftime('%b %d, %Y, %I:%M %p'), # Now formatted PHT
                'source': entry.source or 'N/A', #
                'message': entry.message, #
                'details': entry.details or '' #
            })
        return jsonify(logs_data)
    except Exception as e:
        print(f"CRITICAL_ADMIN_ERROR: Failed to fetch error logs for admin display: {e}")
        log_error_to_db(f"Failed to fetch error logs for admin display: {e}", "get_error_logs_data_for_admin", str(e))
        return jsonify({"error": "Failed to retrieve error logs from database.", "details": str(e)}), 500

@app.route('/api/printer-status')
def printer_status_api():
    global printer_name
    if printer_name is None:
        try:
            printer_name = win32print.GetDefaultPrinter()
        except Exception as e:
            error_msg = f"Could not get default printer for API: {e}"
            print(error_msg)
            return jsonify({"error": error_msg}), 500
    
    status_logs = get_printer_status_wmi()
    return jsonify(status_logs)

@app.route("/admin-feedbacks")
def admin_feedbacks():
    url = "https://docs.google.com/spreadsheets/d/e/2PACX-1vQlpo4U0Z7HHUmeM3XIV9GW6xZ31WGILRjNm8hGCEuEahnxTzyFg5PLOXpFctb_69PZEDH8UAAoOEtk/pub?output=csv"
    feedbacks = []
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        f = StringIO(response.text)
        reader = csv.DictReader(f)
        for row in reader:
            rating_str = row.get("How would you rate your experience?", "0")
            try:
                rating = int(float(rating_str))
            except (ValueError, TypeError):
                rating = 0
            feedbacks.append({
                "user": row.get("Nickname"), "message": row.get("Comment"),
                "time": row.get("Timestamp"), "rating": rating
            })
    except requests.exceptions.RequestException as e:
        error_msg = f"Failed to fetch feedbacks from Google Sheets: {e}"
        print(error_msg)
        log_error_to_db(error_msg, source="admin_feedbacks_fetch", details=str(e))
        return render_template("admin-feedbacks.html", feedbacks=[], error=error_msg)
    return render_template("admin-feedbacks.html", feedbacks=feedbacks)

@app.route("/file-upload")
def file_upload_page():
    return render_template("file-upload.html")

@app.route("/index")
def index():
    return render_template("index.html")

@app.route("/manual-upload")
def manual_upload():
    try:
        files = os.listdir(UPLOAD_FOLDER)
    except FileNotFoundError:
        error_msg = f"Upload folder not found at {UPLOAD_FOLDER}"
        print(error_msg)
        log_error_to_db(error_msg, source="manual_upload_listdir")
        files = []
    return render_template("manual-display-upload.html", files=files)

@app.route("/online-upload", methods=["GET", "POST"])
def online_upload():
    if request.method == "POST":
        file = request.files.get("file")
        if file and file.filename:
            filename = file.filename
            try:
                file.save(os.path.join(UPLOAD_FOLDER, filename))
                return f'File "{filename}" uploaded successfully!'
            except Exception as e:
                error_msg = f"Failed to save uploaded file '{filename}': {e}"
                print(error_msg)
                log_error_to_db(error_msg, source="online_upload_save", details=str(e))
                return "Error saving file on server.", 500
        else:
            log_error_to_db("No file selected for online upload.", source="online_upload_post_no_file")
            return "No file selected!", 400
    else:
        try:
            files = os.listdir(UPLOAD_FOLDER)
        except FileNotFoundError:
            error_msg = f"Upload folder not found at {UPLOAD_FOLDER} for online_upload GET"
            print(error_msg)
            log_error_to_db(error_msg, source="online_upload_listdir_get")
            files = []
        toffee_share_link = "https://toffeeshare.com/nearby"
        return render_template("upload_page.html", files=files, toffee_share_link=toffee_share_link)

@app.route("/upload", methods=["POST"])
def upload_file():
    global images, total_pages
    global printed_pages_today, files_uploaded_today, last_updated_date
    global uploaded_files_info

    if datetime.now().date() != last_updated_date:
        printed_pages_today = 0
        files_uploaded_today = 0
        last_updated_date = datetime.now().date()
        uploaded_files_info = []

    if "file" not in request.files:
        print("Upload Error: No file part in request.")
        return jsonify({"error": "No file part"}), 400

    file = request.files["file"]

    if file.filename == "":
        print("Upload Error: No selected file (empty filename).")
        return jsonify({"error": "No selected file"}), 400

    original_filename = file.filename
    file_ext = original_filename.rsplit(".", 1)[-1].lower()

    if not allowed_file(original_filename):
        print(f"Upload Error: Invalid file format: {original_filename}")
        return jsonify({"error": f"Unsupported file format: .{file_ext}"}), 400

    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    safe_saved_filename = f"{timestamp}_{original_filename}"
    filepath = os.path.join(UPLOAD_FOLDER, safe_saved_filename)

    try:
        file.save(filepath)
        print(f"File saved temporarily at: {filepath}")
    except Exception as e:
        print(f"Upload Error: Failed to save file {safe_saved_filename}: {e}")
        return jsonify({"error": "Failed to save file on server."}), 500

    final_filename_for_info = safe_saved_filename
    final_file_ext_for_info = file_ext
    final_filepath_for_processing = filepath
    processed_images = []
    calculated_total_pages = 0

    global images
    images = []


    if file_ext == "pdf":
        print(f"Processing PDF: {original_filename}")
        processed_images = pdf_to_images(final_filepath_for_processing)
        calculated_total_pages = len(processed_images)

    elif file_ext in ("jpg", "jpeg", "png"):
        print(f"Processing Image: {original_filename}")
        img = cv2.imread(final_filepath_for_processing)
        if img is not None:
            processed_images = [img]
            calculated_total_pages = 1

            pdf_filename = safe_saved_filename.rsplit(".", 1)[0] + ".pdf"
            pdf_path = os.path.join(UPLOAD_FOLDER, pdf_filename)

            try:
                print(f"Converting image to PDF: {pdf_path}")
                image_to_pdf_fit_page(final_filepath_for_processing, pdf_path)
                final_filename_for_info = pdf_filename
                final_file_ext_for_info = "pdf"
                final_filepath_for_processing = pdf_path
                os.remove(filepath)
                print(f"Deleted original image file: {filepath}")

            except Exception as e:
                print(f"Upload Error: Failed to convert image to PDF: {e}")
                if os.path.exists(filepath): os.remove(filepath)
                return jsonify({"error": "Image conversion to PDF failed"}), 500
        else:
            print(f"Upload Error: Failed to read image file: {final_filepath_for_processing}")
            if os.path.exists(final_filepath_for_processing): os.remove(final_filepath_for_processing)
            return jsonify({"error": "Failed to read image file"}), 500

    elif file_ext in ("doc", "docx"):
        print(f"Processing Word Document: {original_filename}")
        processed_images, converted_pdf_path = docx_to_images(final_filepath_for_processing)
        calculated_total_pages = len(processed_images)

        if converted_pdf_path and os.path.exists(converted_pdf_path):
            final_filename_for_info = os.path.basename(converted_pdf_path)
            final_file_ext_for_info = "pdf"
            final_filepath_for_processing = converted_pdf_path
            os.remove(filepath)
            print(f"Deleted original Word document file: {filepath}")
        else:
             print(f"Upload Error: Failed to convert Word document to PDF: {original_filename}")
             if os.path.exists(filepath): os.remove(filepath)
             return jsonify({"error": "Failed to convert Word document to PDF"}), 500

    else:
        print(f"Upload Error: Reached unsupported file format block for {original_filename}")
        if os.path.exists(filepath): os.remove(filepath)
        return jsonify({"error": "Internal server error: Unsupported file format after check."}), 500

    if not processed_images:
        print(f"Upload Error: File processing resulted in no images for {final_filename_for_info}")
        if os.path.exists(final_filepath_for_processing): os.remove(final_filepath_for_processing)
        return jsonify({"error": "Failed to process file content (empty or invalid?)"}), 500

    images = processed_images
    total_pages = calculated_total_pages

    files_uploaded_today += 1
    printed_pages_today += total_pages

    try:
        new_uploaded_file_entry = UploadedFile(
            filename=final_filename_for_info,
            file_type=final_file_ext_for_info,
            pages=total_pages,
            timestamp=datetime.now()
        )
        db.session.add(new_uploaded_file_entry)
        db.session.commit()
        print(f"File info for {final_filename_for_info} saved to database.")
    except Exception as e:
        db.session.rollback()
        print(f"Error saving file info to database: {e}")

    print(f"File uploaded and processed successfully: {final_filename_for_info}")

    return jsonify({
        "message": "File uploaded successfully!",
        "fileName": final_filename_for_info,
        "totalPages": total_pages
    }), 200


@app.route("/generate_preview", methods=["POST"])
def generate_preview():
    global images
    if not images:
        return jsonify({"error": "No images available. Please upload a file first."}), 400

    data = request.json
    if not data:
        return jsonify({"error": "Invalid JSON payload."}), 400

    page_selection = data.get('pageSelection', '')
    num_copies = int(data.get('numCopies', 1))
    page_size = data.get('pageSize', 'Short') # Default to Short
    color_option = data.get('colorOption', 'Color')
    orientation = data.get('orientationOption', 'portrait')

    try:
        selected_indexes = parse_page_selection(page_selection, len(images))
        previews = []

        for idx in selected_indexes:
            if idx < 0 or idx >= len(images):
                 print(f"Warning: Skipping invalid page index {idx} in preview generation.")
                 continue

            img = images[idx].copy()

            current_orientation = data.get("orientationOption") or "auto"
            current_orientation = determine_orientation(img, current_orientation)

            canvas_size = calculate_canvas_size(page_size, current_orientation)
            processed_img = fit_image_to_canvas(img, canvas_size)

            if color_option.lower() == 'grayscale':
                processed_img = cv2.cvtColor(processed_img, cv2.COLOR_BGR2GRAY)
                processed_img = cv2.cvtColor(processed_img, cv2.COLOR_GRAY2BGR)


            for copy_idx in range(num_copies):
                filename = f"print_preview_{idx+1}_{page_size}_{color_option}_{current_orientation}_{copy_idx}_{int(time.time())}.jpg"
                path = os.path.join(app.config['STATIC_FOLDER'], filename)
                cv2.imwrite(path, processed_img, [int(cv2.IMWRITE_JPEG_QUALITY), 90])
                previews.append(f"/uploads/{filename}")

        if not previews:
            return jsonify({"error": "No previews generated. Check your page selection or file."}), 400

        return jsonify({"previews": previews}), 200

    except Exception as e:
        print(f"Error generating preview: {e}")
        return jsonify({"error": f"Failed to generate preview: {str(e)}"}), 500

@app.route("/check_images", methods=["GET"])
def check_images():
    global images
    return jsonify({"imagesAvailable": bool(images)})

@app.route("/preview_with_price", methods=["POST"])
def preview_with_price():
    global images
    if not images:
        return jsonify({"error": "No images available. Please upload a file first."}), 400

    data = request.json
    if not data:
        return jsonify({"error": "Invalid JSON payload."}), 400

    selection = data.get("pageSelection", "")
    num_copies = int(data.get("numCopies", 1))
    page_size = data.get("pageSize", "Short") # Default to Short
    color_option = data.get("colorOption", "Color").lower()

    selected_indexes = parse_page_selection(selection, len(images))
    if not selected_indexes:
        return jsonify({"error": "No valid pages selected."}), 400

    page_prices = []
    total_price = 0
    generated_previews = []

    for idx in selected_indexes:
        if idx < 0 or idx >= len(images):
             print(f"Warning: Skipping invalid page index {idx} in pricing.")
             continue

        original_img = images[idx].copy()

        orientation = data.get("orientationOption") or "auto"
        orientation = determine_orientation(original_img, orientation)

        canvas_size = calculate_canvas_size(page_size, orientation)
        processed_img_for_pricing = fit_image_to_canvas(original_img, canvas_size)

        processed_img_result, page_price = process_image(processed_img_for_pricing, page_size=page_size, color_option=color_option)
        page_price *= num_copies
        total_price += page_price

        preview_filename = f"preview_{idx+1}_{int(time.time())}.jpg"
        preview_path = os.path.join(app.config['STATIC_FOLDER'], preview_filename)
        cv2.imwrite(preview_path, original_img, [int(cv2.IMWRITE_JPEG_QUALITY), 90])

        highlighted_img = highlight_bright_pixels(original_img)
        highlighted_filename = f"bright_pixels_{idx+1}_{int(time.time())}.jpg"
        highlighted_path = os.path.join(app.config['STATIC_FOLDER'], highlighted_filename)
        if highlighted_img is not None:
             cv2.imwrite(highlighted_path, highlighted_img, [int(cv2.IMWRITE_JPEG_QUALITY), 90])
        else:
             cv2.imwrite(highlighted_path, original_img, [int(cv2.IMWRITE_JPEG_QUALITY), 90])


        if color_option == "grayscale":
            processed_filename = f"grayscale_preview_{idx+1}_{int(time.time())}.jpg"
            gray_img = cv2.cvtColor(original_img, cv2.COLOR_BGR2GRAY)
            processed_path = os.path.join(app.config['STATIC_FOLDER'], processed_filename)
            cv2.imwrite(processed_path, gray_img, [int(cv2.IMWRITE_JPEG_QUALITY), 90])
        else:
            processed_filename = f"color_preview_{idx+1}_{int(time.time())}.jpg"
            processed_path = os.path.join(app.config['STATIC_FOLDER'], processed_filename)
            cv2.imwrite(processed_path, original_img, [int(cv2.IMWRITE_JPEG_QUALITY), 90])


        page_prices.append({
            "page": idx + 1,
            "price": round(page_price, 2),
            "original": f"/uploads/{preview_filename}",
            "processed": f"/uploads/{processed_filename}",
            "highlighted": f"/uploads/{highlighted_filename}"
        })

        generated_previews.append({"page": idx + 1, "path": f"/uploads/{preview_filename}"})


    total_price = math.ceil(total_price)

    return jsonify({
        "totalPrice": total_price,
        "pagePrices": page_prices,
        "previews": generated_previews,
        "totalPages": len(images)
    })

def calculate_canvas_size(page_size, orientation):
    sizes = {
        'Short': (2480, 3200), 
        }
    w, h = sizes.get(page_size, sizes['Short']) # Default to Short
    return (max(w, h), min(w, h)) if orientation == 'landscape' else (min(w, h), max(w, h))

def fit_image_to_canvas(image, canvas_size):
    if image is None:
        print("Error: Cannot fit None image to canvas.")
        return None

    canvas_w, canvas_h = canvas_size
    img_h, img_w = image.shape[:2]

    scale = min(canvas_w / img_w, canvas_h / img_h)
    new_w, new_h = int(img_w * scale), int(img_h * scale)
    resized_image = cv2.resize(image, (new_w, new_h), interpolation=cv2.INTER_AREA)
    canvas = np.ones((canvas_h, canvas_w, 3), dtype=np.uint8) * 255
    x_offset = (canvas_w - new_w) // 2
    y_offset = (canvas_h - new_h) // 2
    canvas[y_offset:y_offset+new_h, x_offset:x_offset+new_w] = resized_image
    return canvas

def determine_orientation(image, user_orientation):
    if user_orientation.lower() != 'auto':
        return user_orientation.lower()

    h, w = image.shape[:2]
    return 'landscape' if w > h else 'portrait'

def print_document_logic(print_options):
    try:
        filename_to_print = print_options.get("fileName")
        if not filename_to_print:
            err_msg = "Print Error: No filename provided in print options."
            log_error_to_db(err_msg, "print_document_logic", f"Options: {print_options}")
            return False, err_msg
        
        print(f"Executing placeholder print_document_logic for {filename_to_print}")
        if filename_to_print == "error.pdf":
            log_error_to_db("Simulated print error in print_document_logic", "print_document_logic_simulation")
            return False, "Simulated print error"
        
        upload_dir = app.config['UPLOAD_FOLDER']
        file_path = os.path.join(upload_dir, filename_to_print)
        if not os.path.exists(file_path):
            candidates = [f for f in os.listdir(upload_dir) if filename_to_print in f]
            if candidates:
                file_path = os.path.join(upload_dir, candidates[0])
                print(f"Found candidate file for printing: {file_path}")
            else:
                err_msg = f"Print file '{filename_to_print}' not found in {upload_dir}."
                log_error_to_db(err_msg, "print_document_logic_file_search", f"Original: {filename_to_print}")
                return False, err_msg

        num_copies = int(print_options.get("numCopies", 1))
        default_printer = win32print.GetDefaultPrinter()
        print(f"Attempting to print {file_path} to {default_printer}, {num_copies} copies.")
        
        try:
            for i in range(num_copies):
                win32api.ShellExecute(0, "print", file_path, None, os.path.dirname(file_path), 0)
                time.sleep(0.2)
            print(f"Print commands issued for {filename_to_print}.")
            return True, "Document sent to printer."
        except Exception as e:
            err_msg = f"ShellExecute failed for printing {file_path}: {e}"
            log_error_to_db(err_msg, "print_document_logic_shellexec_final", str(e))
            return False, err_msg

    except Exception as e:
        err_msg = f"Unexpected error in print_document_logic: {e}"
        log_error_to_db(err_msg, "print_document_logic_unexpected", str(e))
        return False, err_msg

def convert_pdf_to_grayscale(input_pdf_path, output_pdf_path):
    doc = None
    gray_doc = None
    try:
        doc = fitz.open(input_pdf_path)
        gray_doc = fitz.open()
        for page in doc:
            try:
                pix = page.get_pixmap(colorspace=fitz.csGRAY)
            except Exception as pixmap_e:
                print(f"Grayscale Conversion Warning: Could not get pixmap for page {page.number} in {input_pdf_path}: {pixmap_e}")
                continue

            rect = page.rect
            new_page = gray_doc.new_page(width=rect.width, height=rect.height)
            try:
                new_page.insert_image(rect, pixmap=pix)
            except Exception as insert_e:
                 print(f"Grayscale Conversion Warning: Could not insert pixmap for page {page.number} in {input_pdf_path}: {insert_e}")
                 continue

        try:
            gray_doc.save(output_pdf_path)
            print(f"Successfully converted {input_pdf_path} to grayscale PDF at {output_pdf_path}.")
        except Exception as save_e:
            error_msg = f"Grayscale Conversion Error: Failed to save grayscale PDF: {save_e}"
            print(error_msg)
            raise Exception(error_msg)

    except fitz.FileNotFoundError:
         error_msg = f"Grayscale Conversion Error: Input PDF not found at {input_pdf_path}"
         print(error_msg)
         raise Exception(error_msg)
    except Exception as e:
        error_msg = f"Grayscale Conversion Error: An unexpected error occurred: {e}"
        print(error_msg)
        if 'gray_doc' in locals() and gray_doc and os.path.exists(output_pdf_path):
             try:
                  gray_doc.close()
                  os.remove(output_pdf_path)
                  print(f"Cleaned up partial grayscale output file: {output_pdf_path}")
             except Exception as cleanup_e:
                  print(f"Error during grayscale cleanup: {cleanup_e}")
        raise Exception(error_msg)
    finally:
        if doc:
            doc.close()
        if gray_doc:
            gray_doc.close()

@app.route('/print_document', methods=['POST'])
def print_document_route():
    data = request.json
    if not data:
        log_error_to_db("Invalid JSON payload for /print_document.", source="print_document_route_no_json")
        return jsonify({"error": "Invalid JSON payload."}), 400

    print("Received print request via /print_document route with data:", data)
    success, message = print_document_logic(data)

    if success:
        return jsonify({"success": True, "message": message}), 200
    else:
        return jsonify({"error": message}), 500

@app.route('/result', methods=['GET'])
def result():
    return render_template('result.html')

@app.route('/uploads/<filename>')
def uploaded_file(filename):
    file_path = os.path.join(app.config['STATIC_FOLDER'], filename)
    if os.path.exists(file_path):
        return send_from_directory(app.config['STATIC_FOLDER'], filename)
    else:
        file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        if os.path.exists(file_path):
             return send_from_directory(app.config['UPLOAD_FOLDER'], filename)
        else:
            return "File not found.", 404

def check_printer_status(printer_name):
    try:
        if not printer_name:
             return "Error: Printer name not set."

        try:
            hPrinter = win32print.OpenPrinter(printer_name)
        except Exception as open_e:
            return f"Error opening printer handle: {open_e}"

        try:
            jobs = win32print.EnumJobs(hPrinter, 0, -1, 2)
            printer_info = win32print.GetPrinter(hPrinter, 2)
            printer_status = printer_info['Status']

            PRINTER_STATUS_PRINTING = 0x00000002
            PRINTER_STATUS_ERROR = 0x00000008
            PRINTER_STATUS_OFFLINE = 0x00000080
            PRINTER_STATUS_PAPER_JAM = 0x00000008 
            PRINTER_STATUS_NO_PAPER = 0x00000010
            PRINTER_STATUS_TONER_LOW = 0x00000040
            PRINTER_STATUS_OUT_OF_PAPER = 0x00000010 

            status_messages = []

            if printer_status & PRINTER_STATUS_ERROR:
                 status_messages.append("Error")
            if printer_status & PRINTER_STATUS_OFFLINE:
                 status_messages.append("Offline")
            if printer_status & PRINTER_STATUS_PAPER_JAM:
                 status_messages.append("Paper Jam")
            if printer_status & PRINTER_STATUS_NO_PAPER or printer_status & PRINTER_STATUS_OUT_OF_PAPER:
                 status_messages.append("No Paper")
            if printer_status & PRINTER_STATUS_TONER_LOW:
                 status_messages.append("Toner Low")


            if jobs:
                if not status_messages:
                    return "Printing"
                else:
                    return ", ".join(status_messages)
            else:
                if status_messages:
                    return ", ".join(status_messages)
                else:
                    return "Idle"

        finally:
            win32print.ClosePrinter(hPrinter)

    except Exception as e:
        return f"Error checking printer status: {e}"

def monitor_printer_status(printer_name, upload_folder):
    last_status = None
    printing_was_active = False

    while True:
        current_status = check_printer_status(printer_name)

        if current_status != last_status:
            print(f"Printer status changed to: {current_status}")
            socketio.emit("printer_status_update", {"status": current_status})

        elif current_status == "Idle" and last_status == "Printing" and printing_was_active:
            time.sleep(10)
            for filename in os.listdir(upload_folder):
                if filename.startswith("print_ready_") or filename.startswith("gs_"):
                    path = os.path.join(upload_folder, filename)
                    if os.path.isfile(path):
                        try:
                            os.remove(path)
                            print(f"Cleaned up temporary print file: {path}")
                        except OSError as e:
                            print(f"Cleanup Warning: Could not delete temporary print file {path} (in use?): {e}")
                        except Exception as e:
                            print(f"Cleanup Error: Could not delete temporary print file {path}: {e}")

        last_status = current_status
        time.sleep(2)

def log_activity(event_type, message):
    pass

if __name__ == "__main__":
    try:
        printer_name = win32print.GetDefaultPrinter()
        print(f"Default printer set to: {printer_name}")
    except Exception as e:
        print(f"CRITICAL: Error getting default printer: {e}. Printer status monitoring WILL NOT WORK.")
        log_error_to_db(f"Failed to get default printer: {e}", "main_get_printer", critical=True)
        printer_name = None

    upload_folder = app.config['UPLOAD_FOLDER']

    if printer_name:
        socketio.start_background_task(monitor_printer_status, printer_name, upload_folder)
    else:
        print("Printer name not available. Printer status monitoring disabled.")

    socketio.run(app, debug=False, allow_unsafe_werkzeug=True)
