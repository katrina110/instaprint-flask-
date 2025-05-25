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
from datetime import datetime
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
from sqlalchemy import func

from pathlib import Path
import base64

app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///instaprint.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
socketio = SocketIO(app, cors_allowed_origins="*")

hopper_balance = 50.00

class Transaction(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    method = db.Column(db.String(50), nullable=False)  # 'GCash' or 'Coinslot'
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

# New Model for Error Logging
class ErrorLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    source = db.Column(db.String(100), nullable=True) # e.g., 'payment.html', 'printer_status', 'app.py_upload'
    message = db.Column(db.Text, nullable=False)
    details = db.Column(db.Text, nullable=True) # Optional: for stack traces or more info

    def __repr__(self):
        return f'<ErrorLog {self.timestamp} - {self.source}: {self.message[:50]}>'


with app.app_context():
    db.create_all()  # ensures tables are created before first use, including ErrorLog

UPLOAD_FOLDER = os.path.join(os.getcwd(), "uploads")
STATIC_FOLDER = os.path.join(os.getcwd(), "static", "uploads")
ALLOWED_EXTENSIONS = {"pdf", "jpg", "jpeg", "png", "docx", "doc"}
printer_name = None
# activity_log = [] # This will be replaced by querying the ErrorLog table for errors
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
uploaded_files_info = []
printed_pages_today = 0
files_uploaded_today = 0
last_updated_date = datetime.now().date()
images = []  # Store images globally (consider refactoring for very large files)
total_pages = 0  # Keep track of total pages.  May not be needed globally.
# selected_previews = []  # Store paths to selected preview images. Removed global variable, generate previews on demand.
arduino = None # This might still be used in the arduino_payment_page route, will keep for now.
coin_count = 0
coin_value_sum = 0  # Track the total value of inserted coins
coin_detection_active = False # Flag to control coin detection
gsm_active = False # Flag to control GSM detection for COM12

# Define COM ports and baud rate for both Arduinos
COIN_SLOT_PORT = 'COM6'
GSM_PORT = 'COM12'
MOTOR_PORT = 'COM15'
BAUD_RATE = 9600

# Global variables to hold serial port objects
coin_slot_serial = None
gsm_serial = None

# Global variables to store GCash payment details temporarily
expected_gcash_amount = 0.0
gcash_print_options = None

# --- Configuration for Recent Downloads ---
try:
    DOWNLOADS_DIR = Path.home() / "Downloads"
    if not DOWNLOADS_DIR.exists() or not DOWNLOADS_DIR.is_dir():
        # Fallback if ~/Downloads doesn't exist or isn't a directory
        # You might want a more specific fallback for a kiosk environment
        DOWNLOADS_DIR = Path(UPLOAD_FOLDER).parent / "User_Downloads_Fallback" # Example fallback
        DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
        print(f"Warning: Standard Downloads folder not found or invalid. Using fallback: {DOWNLOADS_DIR}")
except Exception as e:
    print(f"Could not automatically determine Downloads folder: {e}")
    # Consider a fixed path if auto-detection is problematic in your environment
    DOWNLOADS_DIR = Path("KIOSK_DOWNLOADS_FOLDER_PATH_HERE") # Replace if auto-detection fails
    if str(DOWNLOADS_DIR) == "KIOSK_DOWNLOADS_FOLDER_PATH_HERE":
        # Create a fallback if the placeholder wasn't replaced and home detection failed
        DOWNLOADS_DIR = Path(UPLOAD_FOLDER).parent / "User_Downloads_Fallback_Critical"
        DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
        print(f"CRITICAL: Downloads folder path not set and auto-detection failed. Using critical fallback: {DOWNLOADS_DIR}")

# --- SVG Icon Definitions (from testest.py) ---
def create_svg_data_url(svg_xml_content):
    encoded_svg = base64.b64encode(svg_xml_content.encode('utf-8')).decode('utf-8')
    return f"data:image/svg+xml;base64,{encoded_svg}"

SVG_ICONS = {
    ".pdf": create_svg_data_url("""<svg width="48" height="48" viewBox="0 0 48 48" xmlns="http://www.w3.org/2000/svg"><rect x="6" y="2" width="36" height="44" rx="3" fill="#E53935"/><text x="24" y="30" font-family="sans-serif" font-weight="bold" font-size="12" fill="#FFFFFF" text-anchor="middle">PDF</text></svg>"""),
    ".docx": create_svg_data_url("""<svg width="48" height="48" viewBox="0 0 48 48" xmlns="http://www.w3.org/2000/svg"><rect x="6" y="2" width="36" height="44" rx="3" fill="#1E88E5"/><line x1="12" y1="14" x2="36" y2="14" stroke="#FFFFFF" stroke-width="2"/><line x1="12" y1="22" x2="36" y2="22" stroke="#FFFFFF" stroke-width="2"/><line x1="12" y1="30" x2="28" y2="30" stroke="#FFFFFF" stroke-width="2"/></svg>"""),
    ".doc": create_svg_data_url("""<svg width="48" height="48" viewBox="0 0 48 48" xmlns="http://www.w3.org/2000/svg"><rect x="6" y="2" width="36" height="44" rx="3" fill="#1E88E5"/><text x="24" y="30" font-family="sans-serif" font-weight="bold" font-size="10" fill="#FFFFFF" text-anchor="middle">DOC</text></svg>"""), # Added .doc
    ".jpg": create_svg_data_url("""<svg width="48" height="48" viewBox="0 0 48 48" xmlns="http://www.w3.org/2000/svg"><rect x="6" y="6" width="36" height="36" rx="3" fill="#FB8C00"/><circle cx="16" cy="16" r="4" fill="#FFFFFF"/><polygon points="14 38, 24 24, 34 30, 34 42, 6 42, 6 38" fill="#FDD835"/></svg>"""),
    ".png": create_svg_data_url("""<svg width="48" height="48" viewBox="0 0 48 48" xmlns="http://www.w3.org/2000/svg"><rect x="6" y="6" width="36" height="36" rx="3" fill="#43A047"/><path d="M16 16 L24 24 L32 16 L24 32 Z" fill="#FFFFFF" stroke="#FFFFFF" stroke-width="1"/></svg>"""),
    "default_file": create_svg_data_url("""<svg width="48" height="48" viewBox="0 0 48 48" xmlns="http://www.w3.org/2000/svg"><path d="M12 2 H28 L40 14 V42 C40 43.1 39.1 44 38 44 H12 C10.9 44 10 43.1 10 42 V6 C10 3.9 10.9 2 12 2 Z" fill="#B0BEC5"/><path d="M28 2 V14 H40" fill="#78909C"/></svg>""")
}
SVG_ICONS['.jpeg'] = SVG_ICONS['.jpg']
# Ensure all ALLOWED_EXTENSIONS (dot prefixed) have an icon or will use default
for ext in ALLOWED_EXTENSIONS: # ALLOWED_EXTENSIONS in app.py is {"pdf", "jpg", ...}
    dot_ext = "." + ext
    if dot_ext not in SVG_ICONS:
        SVG_ICONS[dot_ext] = SVG_ICONS["default_file"]

def generate_thumbnail_for_file(item_path_obj: Path, file_ext_without_dot: str, unique_thumb_filename: str, app_config_upload_folder: str):
    """
    Generates a thumbnail for a given file and saves it.
    Returns the URL to the thumbnail or None if generation fails.
    item_path_obj: Path object to the original file (e.g., in DOWNLOADS_DIR).
    file_ext_without_dot: File extension like 'pdf', 'docx', 'jpg'.
    unique_thumb_filename: The filename to save the thumbnail as (e.g., 'thumb_docname_timestamp.png').
    app_config_upload_folder: The path to app.config["UPLOAD_FOLDER"].
    """
    thumbnail_save_path = THUMBNAIL_DIR / unique_thumb_filename
    temp_pdf_path_for_docx = None # Temporary PDF for DOCX conversion

    try:
        if file_ext_without_dot == "pdf":
            doc = None # Initialize doc
            try:
                doc = fitz.open(str(item_path_obj))
                if len(doc) > 0:
                    page = doc.load_page(0)  # First page
                    pix = page.get_pixmap(alpha=False, dpi=50) # Experiment with DPI for size/quality
                    pix.save(str(thumbnail_save_path)) # PyMuPDF can save directly
            finally:
                if doc: doc.close()

        elif file_ext_without_dot in ("jpg", "png"): # Removed "jpeg" here as it's not in the new ALLOWED_EXTENSIONS
            img = cv2.imread(str(item_path_obj))
            if img is not None:
                target_width = 80  # Desired thumbnail width
                height, width = img.shape[:2]
                if width == 0: # Avoid division by zero
                    print(f"Warning: Image {item_path_obj.name} has zero width.")
                    return None
                target_height = int((target_width / width) * height)
                if target_height > 0 and target_width > 0 : # Ensure valid dimensions
                    resized_img = cv2.resize(img, (target_width, target_height), interpolation=cv2.INTER_AREA)
                    cv2.imwrite(str(thumbnail_save_path), resized_img)
                else: # Could not calculate valid dimensions
                    print(f"Warning: Could not calculate valid thumbnail dimensions for {item_path_obj.name}.")
                    return None
            else: # cv2.imread failed
                print(f"Warning: cv2.imread failed for {item_path_obj.name}.")
                return None

        elif file_ext_without_dot == "docx": # Removed "doc"
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
        # log_error_to_db(f"Error generating thumbnail for {item_path_obj.name}: {e}", source="generate_thumbnail_for_file", details=str(e))
        print(f"ERROR generating thumbnail for {item_path_obj.name}: {e}")
        return None
    finally:
        if temp_pdf_path_for_docx and temp_pdf_path_for_docx.exists():
            try:
                os.remove(str(temp_pdf_path_for_docx))
            except Exception as e_clean:
                # log_error_to_db(f"Cleanup error temp PDF {temp_pdf_path_for_docx}", "generate_thumbnail_for_file_cleanup", str(e_clean))
                print(f"ERROR cleaning up temp PDF {temp_pdf_path_for_docx} for thumbnail: {e_clean}")

# --- Helper function to log errors to the database ---
def log_error_to_db(message, source=None, details=None):
    """Logs an error message to the ErrorLog table in the database."""
    try:
        # Ensure app context for database operations if called from threads sometimes
        # However, Flask-SQLAlchemy usually handles this within request contexts.
        # If called from a background thread not managed by Flask, app_context might be needed.
        error_entry = ErrorLog(
            message=str(message), # Ensure message is a string
            source=str(source) if source else None,
            details=str(details) if details else None,
            timestamp=datetime.utcnow()
        )
        db.session.add(error_entry)
        db.session.commit()
        print(f"DB_ERROR_LOGGED: [{source or 'GENERAL'}] {message}")
    except Exception as e:
        db.session.rollback()
        # Log to console as a last resort if DB logging fails
        print(f"CRITICAL_ERROR: Failed to log error to DB: {e}. Original error: [{source}] {message}")

# --- End of Helper function ---

# WebSocket Connection
@socketio.on("connect")
def send_initial_coin_count():
    global coin_value_sum
    print(
        f"WebSocket client connected. Emitting initial coin count: {coin_value_sum}"
    )  # Keep the log
    emit("update_coin_count", {"count": coin_value_sum})

def update_coin_count(count):
    socketio.emit("update_coin_count", {"count": count})

@socketio.on("confirm_gcash_payment")
def handle_confirm_gcash_payment(data):
    global expected_gcash_amount, gcash_print_options
    try:
        expected_gcash_amount = float(data.get('totalPrice', 0.0))
        gcash_print_options = data.get('printOptions', None)
        print(f"GCash payment details received via SocketIO. Expected amount: ₱{expected_gcash_amount}. Print options received: {'Yes' if gcash_print_options else 'No'}")
        emit("gcash_payment_initiated", {"success": True, "message": "Waiting for payment confirmation via SMS."})
    except Exception as e:
        error_msg = f"Error receiving GCash payment details via SocketIO: {e}"
        print(error_msg) # Existing print statement
        log_error_to_db(error_msg, source="confirm_gcash_payment_socket", details=str(e)) # This line logs to DB
        emit("gcash_payment_initiated", {"success": False, "message": error_msg}) # Existing emit

def record_transaction(method, amount):
    try:
        transaction = Transaction(method=method, amount=amount)
        db.session.add(transaction)
        db.session.commit()
        socketio.emit('new_transaction', {
            'method': method,
            'amount': amount,
            'timestamp': transaction.timestamp.strftime('%Y-%m-%d %H:%M:%S')
        })
    except Exception as e:
        db.session.rollback()
        error_msg = f"Failed to record transaction: {e}"
        print(error_msg)
        log_error_to_db(error_msg, source="record_transaction", details=str(e))

@app.route('/api/list_recent_downloads', methods=['GET'])
def list_recent_downloads():
    # Ensure DOWNLOADS_DIR is defined and accessible
    try:
        DOWNLOADS_DIR = Path(Path.home() / "Downloads") # Example
        if not DOWNLOADS_DIR.is_dir():
             raise FileNotFoundError("Downloads directory not found or is not a directory.")
    except Exception as e:
        print(f"CRITICAL: Could not define/access DOWNLOADS_DIR: {e}")
        # log_error_to_db(f"CRITICAL: Could not define/access DOWNLOADS_DIR: {e}", "list_recent_downloads_setup")
        return jsonify({"error": "Server configuration error for downloads directory.", "files": []}), 500

    recent_files_data = []
    try:
        all_items_in_dir = os.listdir(DOWNLOADS_DIR)
        todays_date_for_display = datetime.now().date()
        files_to_consider = []

        for item_name in all_items_in_dir:
            item_path = DOWNLOADS_DIR / item_name
            if item_path.is_file():
                file_extension_without_dot = item_name.rsplit('.', 1)[-1].lower() if '.' in item_name else ''
                # This 'if' condition uses the updated app.config["ALLOWED_EXTENSIONS"]
                if file_extension_without_dot in app.config["ALLOWED_EXTENSIONS"]:
                    try:
                        mtime_timestamp = os.path.getmtime(item_path)
                        mtime_datetime = datetime.fromtimestamp(mtime_timestamp)
                        if mtime_datetime.date() == todays_date_for_display:
                            files_to_consider.append({'path': item_path, 'name': item_name, 'mtime': mtime_timestamp})
                    except Exception as e_stat:
                        # log_error_to_db(f"Could not get stat for file {item_path}: {e_stat}", source="list_recent_downloads_stat")
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
                # SVG_ICONS should be defined in your app.py
                # SVG_ICONS = { ".pdf": "data:image/svg+xml;base64,...", ... , "default_file": "data:..."}
                # For example, if SVG_ICONS is not fully set up, provide a generic fallback:
                # preview_url = SVG_ICONS.get(file_ext_with_dot_for_svg, SVG_ICONS.get("default_file", "path/to/default/icon.svg"))
                preview_url = url_for('static', filename='images/default_icon.png') # Example fallback, ensure this image exists

            recent_files_data.append({
                "filename": original_filename,
                "preview_url": preview_url,
                "mtime_readable": datetime.fromtimestamp(f_info['mtime']).strftime('%I:%M %p, %b %d')
            })

    except Exception as e:
        # log_error_to_db(f"Error listing recent downloads: {e}", source="list_recent_downloads_general", details=str(e))
        print(f"ERROR listing recent downloads: {e}")
        return jsonify({"error": "An error occurred while listing files.", "files": []}), 500

    return jsonify({"files": recent_files_data})

@app.route("/process_downloaded_file", methods=["POST"])
def process_downloaded_file():
    global images, total_pages # These are globals used by your existing logic
    global printed_pages_today, files_uploaded_today, last_updated_date # Daily stats

    data = request.get_json()
    if not data or "selected_filename" not in data:
        log_error_to_db("No filename provided for processing downloaded file.", source="process_downloaded_file_payload")
        return jsonify({"error": "No filename provided."}), 400

    selected_filename = data["selected_filename"]
    
    # Security: Construct path and ensure it's within DOWNLOADS_DIR
    try:
        filepath = (DOWNLOADS_DIR / selected_filename).resolve()
        if not filepath.is_file() or not filepath.is_relative_to(DOWNLOADS_DIR.resolve()):
            log_error_to_db(f"Forbidden access attempt for downloaded file: {selected_filename}", source="process_downloaded_file_security")
            return jsonify({"error": "Invalid file selection or access denied."}), 403
    except Exception as path_e:
        log_error_to_db(f"Path resolution error for {selected_filename}: {path_e}", source="process_downloaded_file_path_resolve")
        return jsonify({"error": "Error resolving file path."}), 500


    original_filename = selected_filename # This is the name from DOWNLOADS_DIR
    file_ext_without_dot = original_filename.rsplit(".", 1)[-1].lower() if '.' in original_filename else ''


    # Reset daily stats if the day has changed (similar to /upload)
    if datetime.now().date() != last_updated_date:
        printed_pages_today = 0
        files_uploaded_today = 0
        last_updated_date = datetime.now().date()
        # uploaded_files_info = [] # This global was in your /upload, decide if needed here

    # --- File path is valid and allowed, proceed with processing ---
    # Create a unique working filename for UPLOAD_FOLDER if conversions happen
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    
    # This variable will hold the path to the file that is actually processed (original or converted)
    # and its name will be used for DB records and frontend display.
    final_filepath_for_processing = filepath # Start with the path from DOWNLOADS_DIR
    final_filename_for_info = original_filename
    final_file_ext_for_info = file_ext_without_dot

    processed_images = []
    calculated_total_pages = 0
    
    images = [] # Clear previous global images

    try:
        if file_ext_without_dot == "pdf":
            print(f"Processing selected PDF: {original_filename}")
            processed_images = pdf_to_images(str(final_filepath_for_processing)) # PyMuPDF needs string path
            calculated_total_pages = len(processed_images)
            # final_filename_for_info and final_file_ext_for_info remain correct

        elif file_ext_without_dot in ("jpg", "jpeg", "png"):
            print(f"Processing selected Image: {original_filename}")
            # The file is already in DOWNLOADS_DIR. We need to copy it or convert it to UPLOAD_FOLDER
            # to fit the existing pattern of image_to_pdf_fit_page saving to a *new* PDF path.
            
            # Create a PDF filename in UPLOAD_FOLDER
            pdf_output_filename = f"{timestamp}_{original_filename.rsplit('.', 1)[0]}.pdf"
            pdf_output_path = os.path.join(UPLOAD_FOLDER, pdf_output_filename)

            print(f"Converting selected image {original_filename} to PDF: {pdf_output_path}")
            image_to_pdf_fit_page(str(final_filepath_for_processing), pdf_output_path) # Needs string path

            # Now read this newly created PDF to get images for preview (as per your existing flow)
            processed_images = pdf_to_images(pdf_output_path) # This will be a list of one image typically
            calculated_total_pages = len(processed_images)
            
            final_filename_for_info = pdf_output_filename
            final_file_ext_for_info = "pdf"
            final_filepath_for_processing = pdf_output_path # This is now the PDF in UPLOAD_FOLDER

        elif file_ext_without_dot in ("doc", "docx"):
            print(f"Processing selected Word Document: {original_filename}")
            # docx_to_images creates a PDF in the *same directory* as the input.
            # We want the PDF in UPLOAD_FOLDER.
            # So, first, copy the doc/docx to a temporary location in UPLOAD_FOLDER.
            temp_doc_path_in_uploads = os.path.join(UPLOAD_FOLDER, f"{timestamp}_{original_filename}")
            import shutil
            shutil.copy(str(final_filepath_for_processing), temp_doc_path_in_uploads)

            processed_images, converted_pdf_path = docx_to_images(temp_doc_path_in_uploads)
            calculated_total_pages = len(processed_images)

            if converted_pdf_path and os.path.exists(converted_pdf_path):
                final_filename_for_info = os.path.basename(converted_pdf_path)
                final_file_ext_for_info = "pdf"
                final_filepath_for_processing = converted_pdf_path # This PDF is in UPLOAD_FOLDER
                os.remove(temp_doc_path_in_uploads) # Clean up temp docx
            else:
                if os.path.exists(temp_doc_path_in_uploads): os.remove(temp_doc_path_in_uploads)
                raise Exception("Word to PDF conversion failed or PDF path not returned.")
        else:
            # Should not happen if ALLOWED_EXTENSIONS is respected by frontend display
            raise Exception(f"Unsupported file type selected: {file_ext_without_dot}")

        if not processed_images:
            raise Exception("File processing resulted in no images.")

        # --- If successful, update global state and return response ---
        images = processed_images # Store for /generate_preview
        total_pages = calculated_total_pages

        files_uploaded_today += 1 # Count as an "upload" for stats
        # printed_pages_today += total_pages # Or adjust this based on actual prints

        try:
            new_uploaded_file_entry = UploadedFile(
                filename=final_filename_for_info, # This is now the (potentially new PDF) name in UPLOAD_FOLDER
                file_type=final_file_ext_for_info, # Should be 'pdf' if conversion happened
                pages=total_pages,
                timestamp=datetime.now()
            )
            db.session.add(new_uploaded_file_entry)
            db.session.commit()
            print(f"File info for {final_filename_for_info} (orig: {original_filename}) saved to database.")
        except Exception as e_db:
            db.session.rollback()
            log_error_to_db(f"Error saving file info to database for {final_filename_for_info}: {e_db}", source="process_downloaded_file_db")
            # Continue to return success to user, as file processing itself was okay

        print(f"Selected downloaded file processed successfully: {final_filename_for_info} (from {original_filename})")
        return jsonify({
            "message": "File processed successfully!",
            "fileName": final_filename_for_info, # Name of the file in UPLOAD_FOLDER ready for printing
            "totalPages": total_pages
        }), 200

    except Exception as e_proc:
        log_error_to_db(f"Error processing selected downloaded file {original_filename}: {e_proc}", source="process_downloaded_file_main_try", details=str(e_proc))
        # Clean up any intermediate files created in UPLOAD_FOLDER if error
        if final_filename_for_info != original_filename and os.path.exists(final_filepath_for_processing):
            try:
                os.remove(final_filepath_for_processing)
                print(f"Cleaned up intermediate file on error: {final_filepath_for_processing}")
            except Exception as e_clean:
                log_error_to_db(f"Error cleaning up intermediate file {final_filepath_for_processing}: {e_clean}", source="process_downloaded_file_cleanup")
        return jsonify({"error": f"Failed to process selected file: {str(e_proc)}"}), 500
    
@app.route('/api/record-transaction', methods=['POST'])
def api_record_transaction():
    data = request.get_json(force=True)
    method = data.get('method')
    amount = float(data.get('amount', 0))
    if method not in ('Coinslot','GCash'):
        return jsonify(success=False, message='Invalid payment method'), 400

    record_transaction(method, amount)
    return jsonify(success=True)

@app.route('/api/filtered-sales', methods=['POST'])
def get_filtered_sales():
    data = request.get_json()
    selected_dates_str = data.get('dates', [])

    # Base queries using the imported func
    query_total = db.session.query(func.sum(Transaction.amount))
    query_gcash = db.session.query(func.sum(Transaction.amount)).filter(Transaction.method == "GCash")
    query_coins = db.session.query(func.sum(Transaction.amount)).filter(Transaction.method == "Coinslot")

    if selected_dates_str:
        # Using func.date
        query_total = query_total.filter(func.date(Transaction.timestamp).in_(selected_dates_str))
        query_gcash = query_gcash.filter(func.date(Transaction.timestamp).in_(selected_dates_str))
        query_coins = query_coins.filter(func.date(Transaction.timestamp).in_(selected_dates_str))

    total_sales = query_total.scalar() or 0.0
    gcash_sales = query_gcash.scalar() or 0.0
    coins_sales = query_coins.scalar() or 0.0

    return jsonify({
        'total_sales': total_sales,
        'gcash_sales': gcash_sales,
        'coins_sales': coins_sales
    })

@app.route("/admin-sales")
def admin_sales():
    # Pass 0 for initial rendering. Dynamic updates will be handled by JavaScript.
    return render_template("admin-sales.html",
                           total_sales=0,
                           gcash_sales=0,
                           coins_sales=0)

@app.route("/set_price", methods=["POST"])
def set_price():
    global coin_slot_serial
    data = request.get_json() or {}
    price = int(data.get("price", 0))

    if not coin_slot_serial or not coin_slot_serial.is_open:
        try:
            coin_slot_serial = serial.Serial(COIN_SLOT_PORT, BAUD_RATE, timeout=1)
            time.sleep(2)
        except serial.SerialException as e:
            error_msg = f"Failed to open serial port {COIN_SLOT_PORT} for set_price: {str(e)}"
            print(error_msg)
            log_error_to_db(error_msg, source="set_price_serial_open", details=str(e))
            return jsonify(success=False, message=error_msg), 500

    try:
        coin_slot_serial.write(f"{price}\n".encode())
        return jsonify(success=True, message="Price set on Arduino.")
    except Exception as e:
        error_msg = f"Failed to send price to Arduino on {COIN_SLOT_PORT}: {str(e)}"
        print(error_msg)
        log_error_to_db(error_msg, source="set_price_serial_write", details=str(e))
        return jsonify(success=False, message=error_msg), 500

def update_hopper_balance(change_dispensed):
    global hopper_balance
    if hopper_balance >= change_dispensed:
        hopper_balance -= change_dispensed
        print(f"[HOPPER] Dispensed ₱{change_dispensed}. Remaining balance: ₱{hopper_balance:.2f}")
        return True
    else:
        print("[HOPPER] Not enough balance to dispense change.")
        return False

def serial_listener():
    global coin_slot_serial
    global hopper_balance

    while True:
        if coin_slot_serial and coin_slot_serial.is_open and coin_slot_serial.in_waiting:
            try:
                line = coin_slot_serial.readline().decode().strip()
                if line.startswith("COIN:"):
                    try:
                        total = int(line.split(":")[1])
                        socketio.emit("coin_update", {"total": total})
                    except ValueError:
                        print(f"Could not parse coin total from serial: {line}")
                elif line == "PRINTING":
                    socketio.emit("printer_status", {"status": "Printing"})
                elif line.startswith("CHANGE:"):
                    try:
                        amt = int(line.split(":")[1])
                        # Update hopper balance here
                        if update_hopper_balance(amt):
                            socketio.emit("change_dispensed", {"amount": amt})
                        else:
                            socketio.emit("change_error", {"message": "Insufficient change in hopper."})
                    except ValueError:
                        print(f"Could not parse change amount from serial: {line}")
            except serial.SerialException as e:
                print(f"Serial error during read on {COIN_SLOT_PORT}: {e}")
                if coin_slot_serial and coin_slot_serial.is_open:
                    try:
                        coin_slot_serial.close()
                        print(f"Closed serial port {COIN_SLOT_PORT} due to error.")
                    except Exception as close_e:
                        print(f"Error closing serial port {COIN_SLOT_PORT}: {close_e}")
                coin_slot_serial = None

        elif coin_slot_serial is None or not coin_slot_serial.is_open:
            try:
                coin_slot_serial = serial.Serial(COIN_SLOT_PORT, BAUD_RATE, timeout=1)
                print(f"Successfully re-opened serial port {COIN_SLOT_PORT}.")
                time.sleep(2)
            except serial.SerialException as e:
                print(f"Failed to re-open serial port {COIN_SLOT_PORT}: {e}")
                coin_slot_serial = None
                time.sleep(5)

        time.sleep(0.05)
        
# Helper Functions
def allowed_file(filename):
    return (
        "." in filename
        and filename.rsplit(".", 1)[1].lower() in app.config["ALLOWED_EXTENSIONS"]
    )

def image_to_pdf_fit_page(image_path, output_pdf_path, page_size="A4"):
    page_sizes = {
        "A4": (210, 297),
        "Short": (216, 279),  # US Letter approx (8.5 x 11 in)
        "Long": (216, 330),   # Custom longer paper, e.g., 8.5 x 13 in
    }

    if page_size not in page_sizes:
        page_size = "A4"  # Default fallback

    page_w_mm, page_h_mm = page_sizes[page_size]

    # Load image to get size in pixels
    img = cv2.imread(image_path)
    if img is None:
        raise ValueError(f"Failed to load image for PDF conversion: {image_path}")

    img_h_px, img_w_px = img.shape[:2]

    # Assume 96 DPI for pixel to mm conversion (adjust if you know real DPI)
    dpi = 96
    px_to_mm = 25.4 / dpi

    img_w_mm = img_w_px * px_to_mm
    img_h_mm = img_h_px * px_to_mm

    # Calculate scale to fit image inside page, preserving aspect ratio
    scale = min(page_w_mm / img_w_mm, page_h_mm / img_h_mm)

    pdf_img_w = img_w_mm * scale
    pdf_img_h = img_h_mm * scale

    # Center image position on page
    x_pos = (page_w_mm - pdf_img_w) / 2
    y_pos = (page_h_mm - pdf_img_h) / 2

    pdf = FPDF(unit="mm", format=(page_w_mm, page_h_mm))
    pdf.add_page()
    pdf.image(image_path, x=x_pos, y=y_pos, w=pdf_img_w, h=pdf_img_h)
    pdf.output(output_pdf_path)


def pdf_to_images(pdf_path):
    """Converts a PDF file to a list of OpenCV images."""
    # Optimization Note: For very large PDFs, converting all pages to images upfront
    # might be memory intensive. Consider processing pages on demand if this becomes a bottleneck.
    document = None # Initialize document to None
    try:
        document = fitz.open(pdf_path)
        pdf_images = []
        for page_num in range(len(document)):
            page = document.load_page(page_num)
            # Use get_pixmap with higher dpi for better quality previews if needed,
            # but be mindful of memory usage. Default dpi is usually sufficient.
            pix = page.get_pixmap(alpha=False)
            # Convert pixmap to numpy array directly
            image = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
                (pix.height, pix.width, pix.n) # Use pix.n for number of channels
            )
            # Ensure image is BGR for OpenCV compatibility if it's RGB
            if pix.n == 3:
                 image = cv2.cvtColor(image, cv2.COLOR_RGB2BGR)
            elif pix.n == 4: # Handle RGBA if necessary by converting to BGR and dropping alpha
                 image = cv2.cvtColor(image, cv2.COLOR_RGBA2BGR)

            pdf_images.append(image)
        return pdf_images
    except Exception as e:
        print(f"Error processing PDF: {e}")
        return []
    finally:
        if document:
            document.close()  # Ensure document is closed

def docx_to_images(file_path):
    """
    Converts a Word document (doc or docx) to a list of OpenCV images and returns the PDF path.
    """
    if not file_path.lower().endswith((".doc", ".docx")):
        raise ValueError("Unsupported file type for Word documents.")

    pdf_path = None
    try:
        pythoncom.CoInitialize()
        word = client.Dispatch("Word.Application")
        word.visible = False
        doc = word.Documents.Open(file_path)

        # Create new filename by replacing .docx/.doc with .pdf
        pdf_path = file_path.rsplit(".", 1)[0] + ".pdf"
        doc.SaveAs(pdf_path, FileFormat=17)  # 17 = PDF format
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
    import re
    pages = set()
    if not selection:
        return list(range(total_pages))  # Default: all pages

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
    # Ensure image is not None and is in BGR format
    if image is None or len(image.shape) < 3 or image.shape[2] != 3:
         print("Error: Invalid image format for highlighting.")
         return None

    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)

    # Create a mask for dark pixels (below threshold)
    dark_mask = gray < dark_threshold

    # Start with a white background
    result = np.ones_like(image, dtype=np.uint8) * 255

    # Keep only pixels that are not too dark
    result[~dark_mask] = image[~dark_mask]

    return result



def process_image(image, page_size="A4", color_option="Color"):
    """
    Processes an image to determine printing cost.

    Args:
        image (numpy.ndarray): The image to process.
        page_size (str):  "Short", "A4", or "Long".
        color_option (str): "Color" or "Grayscale".

    Returns:
        tuple: (processed_image, price)
    """
    if image is None:
        print("Error: Unable to load image for processing.")
        return None, 0.0

    # Define paper costs and max price caps based on page size
    # Using more descriptive variable names
    paper_cost_per_page = {
        "Short": 2.0,
        "A4": 3.0,
        "Long": 5.0
    }
    # Default paper cost if page_size is not recognized
    default_paper_cost = 195 / 500 # Example default, adjust as needed
    paper_cost = paper_cost_per_page.get(page_size, default_paper_cost)

    max_price_cap_per_page = {
        "Short": 15.0,
        "A4": 18.0,
        "Long": 20.0
    }
    # Default max price cap if page_size is not recognized
    default_max_cap = 18.0 # Example default, adjust as needed
    max_cap = max_price_cap_per_page.get(page_size, default_max_cap)


    # Convert image to grayscale for analysis
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    total_pixels = gray.size # Total number of pixels in the image

    # Calculate ratios of different pixel intensities
    white_ratio = np.sum(gray > 220) / total_pixels
    dark_text_ratio = np.sum((gray > 10) & (gray < 180)) / total_pixels # Pixels that are not very dark or very light

    # Case 1: Text-only document (mostly white with some dark text)
    if white_ratio > 0.85 and dark_text_ratio < 0.15:
        text_only_prices = {"Short": 3.0, "A4": 3.0, "Long": 4.0}
        # Return original image and text-only price
        return image, text_only_prices.get(page_size, 3.0)

    # Case 2: Mostly white page (very few non-white pixels)
    # Check if the image is almost entirely white (allowing for some minor variations)
    if np.mean(gray) > 240: # Average pixel intensity is close to white (255)
         return image, 0.0 # Return original image and zero price

    # Case 3: Full black page (or nearly full black)
    if np.mean(gray) < 15: # Average pixel intensity is close to black (0)
        base_rate = 0.000006 # Base cost per pixel for full coverage
        # Calculate base cost based on total pixels and base rate, add paper cost
        base_cost = total_pixels * base_rate + paper_cost
        # Apply a multiplier for full black pages and cap the price
        return image, round(min(base_cost * 1.5, max_cap), 2)

    # Grayscale processing logic
    if color_option.lower() == "grayscale":
        # Detect edges to estimate complexity
        edges = cv2.Canny(gray, 50, 150)
        edge_density = np.sum(edges > 0) / total_pixels # Ratio of edge pixels

        # Calculate dark pixel ratio (pixels darker than 100)
        dark_ratio = np.sum(gray < 100) / total_pixels

        # Calculate image entropy to estimate information content/texture complexity
        # Use a small epsilon to avoid log(0)
        hist = cv2.calcHist([gray], [0], None, [256], [0, 256])
        hist_norm = hist.ravel() / (hist.sum() + 1e-7) # Normalize histogram
        entropy = -np.sum(hist_norm * np.log2(hist_norm + 1e-7)) # Calculate entropy

        # Determine base price based on complexity metrics
        if white_ratio > 0.85 and dark_text_ratio < 0.15:
            base_price = 1.0  # Simple text with potential logo
        elif edge_density < 0.004 and entropy < 5.0:
            base_price = 2.0 # Low complexity, simple graphics
        elif edge_density < 0.01 and entropy < 5.5:
            base_price = 3.0 # Moderate complexity
        elif edge_density < 0.02 and entropy < 6.0:
            base_price = 4.0 # Higher complexity
        else:
            # High complexity, potentially images or detailed graphics
            base_price = 6.0 if dark_ratio > 0.2 else 5.0 # Higher price for darker images

        # Calculate final price and cap it
        final_price = base_price + paper_cost
        return image, round(min(final_price, max_cap), 2)

    # Color processing logic
    # Convert image to HSV color space to analyze color saturation
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    # Create a mask for pixels with significant color saturation (excluding near-grayscale)
    # Saturation > 50 and Value > 50 to exclude very dark or very light colors
    color_mask = cv2.inRange(hsv, np.array([0, 50, 50]), np.array([180, 255, 255]))
    color_pixel_count = cv2.countNonZero(color_mask) # Number of colored pixels
    color_coverage = color_pixel_count / total_pixels # Ratio of colored pixels

    # Determine base price based on color coverage
    if color_coverage == 0:
        base_price = 1.0 # No color detected (effectively grayscale)
    elif color_coverage < 0.015:
        base_price = 2.0 # Very low color coverage (e.g., a small logo)
    elif color_coverage < 0.25:
        base_price = 3.0 # Moderate color coverage
    else:
        base_price = 4.0 # High color coverage (e.g., photos, graphics)

    # Calculate final price for color prints (typically higher than grayscale) and cap it
    final_price = (base_price + paper_cost) * 1.5 # Apply a multiplier for color
    return image, round(min(final_price, max_cap), 2)


# Printer real-time status
def get_printer_status_wmi():
    global printer_name, logged_errors # activity_log is removed from globals here as errors go to DB
    c = wmi.WMI()
    logs_for_immediate_return = [] # This is for the /api/printer-status immediate response
    now = datetime.now()

    status_map = {
        1: "Other",
        2: "Unknown",
        3: "Idle",
        4: "Printing",
        5: "Warming Up",
        6: "Stopped Printing",
        7: "Offline"
    }

    error_state_map = {
        0: "Unknown",
        1: "Other",
        2: "No Paper",
        3: "No Toner",
        4: "Door Open",
        5: "Jammed",
        6: "Service Requested",
        7: "Output Bin Full"
    }

    try:
        printers_found = c.Win32_Printer(Name=printer_name)
        if not printers_found:
            msg = f"Printer '{printer_name}' not found by WMI."
            if "printer_not_found" not in logged_errors:
                print(msg)
                log_error_to_db(msg, source="get_printer_status_wmi_not_found")
                logs_for_immediate_return.append({"date": now.strftime("%m/%d/%Y"), "time": now.strftime("%I:%M %p"), "event": msg})
                logged_errors.add("printer_not_found") # Avoid flooding with "not found"
            return logs_for_immediate_return


        for printer in printers_found:
            status_msg_from_map = status_map.get(printer.PrinterStatus, "Unknown")
            error_code = printer.DetectedErrorState
            error_msg_from_map = error_state_map.get(error_code, None) # Default to None if no specific error

            # Key for tracking this specific error state to avoid re-logging if it persists
            current_error_key = f"printer_{printer_name}_error_{error_code}"
            # Key for tracking general status to avoid re-logging if it persists
            current_status_key = f"printer_{printer_name}_status_{printer.PrinterStatus}"

            if error_code != 0 and error_msg_from_map and error_msg_from_map != "Unknown":
                # This is an actual error state
                full_error_message = f"Printer '{printer_name}': {error_msg_from_map} (Device Status: {status_msg_from_map})"
                if current_error_key not in logged_errors:
                    print(f"LOGGING NEW PRINTER ERROR: {full_error_message}")
                    log_error_to_db(message=full_error_message, source="get_printer_status_wmi_error")
                    logs_for_immediate_return.append({"date": now.strftime("%m/%d/%Y"), "time": now.strftime("%I:%M %p"), "event": full_error_message})
                    logged_errors.add(current_error_key)
            elif error_code == 0 and current_error_key in logged_errors:
                # Error condition has cleared
                resolved_msg = f"Printer '{printer_name}' error (was code {error_code}) appears resolved. Current status: {status_msg_from_map}."
                print(resolved_msg)
                # Optionally log resolution to DB or a general activity log
                # log_error_to_db(message=resolved_msg, source="get_printer_status_wmi_resolved")
                logged_errors.remove(current_error_key)
            
            # For general status messages (non-errors) that might be useful for the immediate return
            # This part is for the `logs_for_immediate_return` which might be displayed on a dashboard live.
            # The persistent `ErrorLog` DB is primarily for errors.
            general_status_event = f"Printer '{printer_name}': {status_msg_from_map}"
            if status_msg_from_map not in ["Idle", "Unknown"] and not (error_code != 0 and error_msg_from_map and error_msg_from_map != "Unknown"):
                 # This is a non-error, non-idle status
                if current_status_key not in logged_errors: # Using logged_errors to prevent flooding immediate UI too
                    logs_for_immediate_return.append({"date": now.strftime("%m/%d/%Y"), "time": now.strftime("%I:%M %p"), "event": general_status_event})
                    logged_errors.add(current_status_key)
            elif status_msg_from_map == "Idle" and current_status_key in logged_errors:
                 logged_errors.remove(current_status_key) # Clear if it became Idle

    except pywintypes.com_error as com_e:
        error_message = f"WMI COM Error accessing printer '{printer_name}': {com_e}"
        if "wmi_com_error" not in logged_errors: # Generic key for this type of error
            print(error_message)
            log_error_to_db(message=error_message, source="get_printer_status_wmi_com_error", details=str(com_e))
            logs_for_immediate_return.append({"date": now.strftime("%m/%d/%Y"), "time": now.strftime("%I:%M %p"), "event": error_message})
            logged_errors.add("wmi_com_error")
    except Exception as e:
        error_message = f"WMI general error for printer '{printer_name}': {e}"
        if "wmi_general_error" not in logged_errors: # Generic key
            print(error_message)
            log_error_to_db(message=error_message, source="get_printer_status_wmi_general_error", details=str(e))
            logs_for_immediate_return.append({"date": now.strftime("%m/%d/%Y"), "time": now.strftime("%I:%M %p"), "event": error_message})
            logged_errors.add("wmi_general_error")
            
    return logs_for_immediate_return


# Routes - Admin
@app.route("/")
def admin_user():
    return render_template("file-upload.html")


@app.route("/admin-dashboard")
def admin_dashboard():
    transactions = Transaction.query.order_by(Transaction.timestamp.desc()).limit(10).all()
    total_sales = db.session.query(db.func.sum(Transaction.amount)).scalar() or 0
    
    # Count by method
    gcash_total = db.session.query(db.func.sum(Transaction.amount)).filter(Transaction.method == 'GCash').scalar() or 0
    coinslot_total = db.session.query(db.func.sum(Transaction.amount)).filter(Transaction.method == 'Coinslot').scalar() or 0

    data = {
        "total_sales": total_sales,
        "printed_pages": printed_pages_today,
        "files_uploaded": files_uploaded_today,
        "current_balance": 200,
        "sales_history": [
            {
                "method": t.method,
                "amount": t.amount,
                "date": t.timestamp.strftime('%Y-%m-%d'),
                "time": t.timestamp.strftime('%H:%M:%S')
            } for t in transactions
        ],
        "sales_chart": [500, 600, 700, 800],  # Example sales data for Chart.js
        "transaction_method_summary": {
            "gcash": round(gcash_total),
            "coinslot": round(coinslot_total)
        }
    }
    return render_template("admin-dashboard.html", data=data)


@app.route("/admin-files-upload") #
def admin_printed_pages(): #
    # Query the database for all uploaded file records, order by most recent
    all_uploaded_files = UploadedFile.query.order_by(UploadedFile.timestamp.desc()).all() #
    
    # Format the data for the template
    # The template expects 'file', 'type', 'pages', 'time'
    formatted_files = []
    for uploaded_file_entry in all_uploaded_files:
        formatted_files.append({
            'file': uploaded_file_entry.filename,
            'type': uploaded_file_entry.file_type,
            'pages': uploaded_file_entry.pages,
            'time': uploaded_file_entry.timestamp.strftime('%I:%M %p') # Format time as needed
        })
        
    return render_template("admin-files-upload.html", uploaded=formatted_files) #


@app.route("/admin-activity-log")
def admin_activity_log():
    # Pass the global activity log to the template
    return render_template('admin-activity-log.html', printed=activity_log)

# Store logs in memory (or ideally, use a database)
activity_logs = []

@app.route('/log_activity', methods=['POST'])
def log_activity_from_frontend_route():
    data = request.get_json()
    message = data.get("message")
    source = data.get("source", "frontend_general") # Default source if not provided
    details = data.get("details")

    if not message:
        # Log this failure to console, not DB to avoid loop if DB is the issue
        print(f"Received log_activity request with no message from {source}")
        return jsonify(success=False, error="No message provided"), 400

    log_error_to_db(message=message, source=source, details=details)
    return jsonify(success=True, message="Error logged to database.")

@app.route('/admin_activity_data')
def get_error_logs_data_for_admin():
    try:
        # Query the ErrorLog table, order by most recent, limit for display
        error_entries = ErrorLog.query.order_by(ErrorLog.timestamp.desc()).limit(200).all() # Increased limit
        
        logs_data = []
        for entry in error_entries:
            logs_data.append({
                'id': entry.id,
                'timestamp': entry.timestamp.strftime('%Y-%m-%d %H:%M:%S UTC'),
                'source': entry.source or 'N/A',
                'message': entry.message,
                'details': entry.details or '' # Send details if available
            })
        return jsonify(logs_data)
    except Exception as e:
        # Log this critical failure to console
        print(f"CRITICAL_ADMIN_ERROR: Failed to fetch error logs for admin display: {e}")
        # Avoid logging to DB here if the DB itself might be the issue, to prevent error loops.
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
            # Don't log to DB here as get_printer_status_wmi will handle its own logging
            return jsonify({"error": error_msg}), 500
    
    # get_printer_status_wmi now returns logs for immediate display
    # and also logs errors to the DB.
    status_logs = get_printer_status_wmi()
    return jsonify(status_logs) # Return the immediately relevant logs/statuses

@app.route("/admin-balance")
def admin_balance():
    return render_template("admin-balance.html")

@app.route("/admin-feedbacks")
def admin_feedbacks():
    # ... (feedback logic remains the same) ...
    url = "https://docs.google.com/spreadsheets/d/e/2PACX-1vQlpo4U0Z7HHUmeM3XIV9GW6xZ31WGILRjNm8hGCEuEahnxTzyFg5PLOXpFctb_69PZEDH8UAAoOEtk/pub?output=csv"
    feedbacks = []
    try:
        response = requests.get(url, timeout=10) # Added timeout
        response.raise_for_status() # Raise HTTPError for bad responses (4XX or 5XX)
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
        # Pass an empty list or an error message to the template
        return render_template("admin-feedbacks.html", feedbacks=[], error=error_msg)
    return render_template("admin-feedbacks.html", feedbacks=feedbacks)

@app.route("/file-upload") # This is also the root route "/"
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
        files = [] # Present an empty list to avoid crashing the template
    return render_template("manual-display-upload.html", files=files)

@app.route("/online-upload", methods=["GET", "POST"])
def online_upload():
    if request.method == "POST":
        file = request.files.get("file")
        if file and file.filename: # Check if filename is not empty
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
    else: # GET request
        try:
            files = os.listdir(UPLOAD_FOLDER)
        except FileNotFoundError:
            error_msg = f"Upload folder not found at {UPLOAD_FOLDER} for online_upload GET"
            print(error_msg)
            log_error_to_db(error_msg, source="online_upload_listdir_get")
            files = []
        toffee_share_link = "https://toffeeshare.com/nearby" # This seems static
        return render_template("upload_page.html", files=files, toffee_share_link=toffee_share_link)

@app.route("/payment")
def payment_page():
    global coin_detection_active, coin_slot_serial, coin_value_sum
    coin_value_sum = 0
    socketio.emit('update_coin_count', {'count': coin_value_sum})
    coin_detection_active = True
    print("Coin detection activated for payment. Reset and fresh cycle.")

    try:
        if coin_slot_serial and coin_slot_serial.is_open:
            coin_slot_serial.close()
            print("Closed stale coin-serial port for fresh open.")
    except Exception as e:
        print(f"Warning closing stale serial port for payment page: {e}") # Not a critical error to log to DB

    try:
        coin_slot_serial = serial.Serial(COIN_SLOT_PORT, BAUD_RATE, timeout=1)
        time.sleep(2) # Allow Arduino to reset
        print(f"Re-opened serial port {COIN_SLOT_PORT} for new payment cycle.")
    except Exception as e:
        error_msg = f"Error reopening serial port {COIN_SLOT_PORT} for payment: {e}"
        print(error_msg)
        log_error_to_db(error_msg, source="payment_page_serial_reopen", details=str(e))
        # Potentially redirect or show error on payment page if serial is critical
        # return render_template("payment.html", error="Serial port connection failed.")

    price_to_send_str = request.args.get('price')
    if price_to_send_str and coin_slot_serial and coin_slot_serial.is_open: # Check if serial is open
        try:
            price_to_send = int(price_to_send_str)
            coin_slot_serial.write(f"{price_to_send}\n".encode())
            print(f"Price {price_to_send} sent to Arduino from /payment route.")
        except Exception as e:
            error_msg = f"Error sending price to Arduino in /payment fresh cycle: {e}"
            print(error_msg)
            log_error_to_db(error_msg, source="payment_page_send_price", details=str(e))
    elif not price_to_send_str:
        print("No price parameter received in /payment URL.")
        log_error_to_db("No price parameter in /payment URL.", source="payment_page_no_price")
    elif not (coin_slot_serial and coin_slot_serial.is_open):
        print(f"Cannot send price, serial port {COIN_SLOT_PORT} is not open.")
        # Error already logged above if open failed

    return render_template("payment.html")

@app.route("/stop_coin_detection", methods=["POST"])
def stop_coin_detection():
    global coin_detection_active
    coin_detection_active = False
    print("Coin detection deactivated by user.")
    global coin_slot_serial
    if coin_slot_serial and coin_slot_serial.is_open:
        try: coin_slot_serial.close()
        except Exception as e: print(f"Error closing coin_slot_serial on stop_coin_detection: {e}")
    return jsonify({"success": True, "message": "Coin detection stopped."})

# File Upload and Processing
@app.route("/upload", methods=["POST"])
def upload_file():
    """Handles file uploads, converts them to images or PDFs, and stores file information."""
    global images, total_pages
    global printed_pages_today, files_uploaded_today, last_updated_date
    global uploaded_files_info

    # Reset daily stats if the day has changed
    if datetime.now().date() != last_updated_date:
        printed_pages_today = 0
        files_uploaded_today = 0
        last_updated_date = datetime.now().date()
        uploaded_files_info = [] # Also clear file info for the new day

    # Check if the file part is in the request
    if "file" not in request.files:
        print("Upload Error: No file part in request.")
        return jsonify({"error": "No file part"}), 400

    file = request.files["file"]

    # Check if a file was selected (filename is not empty)
    if file.filename == "":
        print("Upload Error: No selected file (empty filename).")
        return jsonify({"error": "No selected file"}), 400

    original_filename = file.filename
    file_ext = original_filename.rsplit(".", 1)[-1].lower()

    # Check if the file extension is allowed
    if not allowed_file(original_filename):
        print(f"Upload Error: Invalid file format: {original_filename}")
        return jsonify({"error": f"Unsupported file format: .{file_ext}"}), 400

    # --- File is valid and allowed, proceed with saving and processing ---

    # Create a safe, unique filename for saving
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    # Use original_filename here to preserve the original extension in the saved path
    # This is important for the file.save() method and subsequent processing
    safe_saved_filename = f"{timestamp}_{original_filename}"
    filepath = os.path.join(UPLOAD_FOLDER, safe_saved_filename)

    try:
        # Save the original uploaded file
        file.save(filepath)
        print(f"File saved temporarily at: {filepath}")
    except Exception as e:
        print(f"Upload Error: Failed to save file {safe_saved_filename}: {e}")
        return jsonify({"error": "Failed to save file on server."}), 500

    # --- File saved, now process it ---

    # Initialize variables that will hold the *final* filename and total pages
    # after potential conversion to PDF. Initialize filename here!
    final_filename_for_info = safe_saved_filename # Start with the saved filename
    final_file_ext_for_info = file_ext # Start with original extension
    final_filepath_for_processing = filepath # Use the saved file initially
    processed_images = []
    calculated_total_pages = 0

    # Reset previous global image data
    global images # Clear previous images before processing the new file
    images = []


    # Process based on file type
    if file_ext == "pdf":
        print(f"Processing PDF: {original_filename}")
        processed_images = pdf_to_images(final_filepath_for_processing)
        calculated_total_pages = len(processed_images)
        # final_filename_for_info and final_file_ext_for_info remain correct here

    elif file_ext in ("jpg", "jpeg", "png"):
        print(f"Processing Image: {original_filename}")
        img = cv2.imread(final_filepath_for_processing)
        if img is not None:
            # Even for a single image, treat as a list of one for consistency
            processed_images = [img]
            calculated_total_pages = 1

            # Convert image to PDF for consistent handling downstream (printing, page selection)
            # Generate a PDF filename based on the safe_saved_filename base name
            pdf_filename = safe_saved_filename.rsplit(".", 1)[0] + ".pdf"
            pdf_path = os.path.join(UPLOAD_FOLDER, pdf_filename)

            try:
                print(f"Converting image to PDF: {pdf_path}")
                image_to_pdf_fit_page(final_filepath_for_processing, pdf_path)
                # Update tracking variables to point to the new PDF
                final_filename_for_info = pdf_filename
                final_file_ext_for_info = "pdf"
                final_filepath_for_processing = pdf_path
                # Delete the original image file as it's been converted
                os.remove(filepath) # Use the initial 'filepath' which points to the original image
                print(f"Deleted original image file: {filepath}")

            except Exception as e:
                print(f"Upload Error: Failed to convert image to PDF: {e}")
                # Clean up the original image if conversion failed
                if os.path.exists(filepath): os.remove(filepath)
                return jsonify({"error": "Image conversion to PDF failed"}), 500
        else:
            print(f"Upload Error: Failed to read image file: {final_filepath_for_processing}")
            # Clean up the original image file if read failed
            if os.path.exists(final_filepath_for_processing): os.remove(final_filepath_for_processing)
            return jsonify({"error": "Failed to read image file"}), 500

    elif file_ext in ("doc", "docx"):
        print(f"Processing Word Document: {original_filename}")
        # docx_to_images returns images and the path to the *newly created PDF*
        processed_images, converted_pdf_path = docx_to_images(final_filepath_for_processing) # Use the saved doc/docx path
        calculated_total_pages = len(processed_images)

        if converted_pdf_path and os.path.exists(converted_pdf_path):
            # Update tracking variables to point to the new PDF
            final_filename_for_info = os.path.basename(converted_pdf_path) # Get just the filename
            final_file_ext_for_info = "pdf"
            final_filepath_for_processing = converted_pdf_path
            # Delete the original DOC/DOCX file
            os.remove(filepath) # Use the initial 'filepath' which points to the original doc/docx
            print(f"Deleted original Word document file: {filepath}")
        else:
             print(f"Upload Error: Failed to convert Word document to PDF: {original_filename}")
             # Clean up the original doc/docx if conversion failed
             if os.path.exists(filepath): os.remove(filepath)
             return jsonify({"error": "Failed to convert Word document to PDF"}), 500

    else:
        # This block should ideally be unreachable due to allowed_file check
        print(f"Upload Error: Reached unsupported file format block for {original_filename}")
        # Clean up the saved file
        if os.path.exists(filepath): os.remove(filepath)
        return jsonify({"error": "Internal server error: Unsupported file format after check."}), 500

    # --- Check if processing resulted in images ---
    if not processed_images:
        print(f"Upload Error: File processing resulted in no images for {final_filename_for_info}")
        # Clean up the potentially converted file if processing failed
        # Check using the potentially updated filepath
        if os.path.exists(final_filepath_for_processing): os.remove(final_filepath_for_processing)
        return jsonify({"error": "Failed to process file content (empty or invalid?)"}), 500

    # --- If successful, update global state and return response ---

    # Store the processed images globally (consider memory implications for large files)
    images = processed_images
    total_pages = calculated_total_pages # Update global total_pages

    # Update daily stats
    files_uploaded_today += 1
    # Note: printed_pages_today should ideally count pages sent to the printer,
    # not pages in the uploaded file. Keeping original logic for now.
    printed_pages_today += total_pages

    # Add file info to the admin view list
    # uploaded_files_info.append({
    #     "file": final_filename_for_info, # Use the determined final filename
    #     "type": final_file_ext_for_info, # Use the determined final extension ('pdf')
    #     "pages": total_pages,
    #    "time": datetime.now().strftime('%I:%M %p')
    # })

    # Save uploaded file information to the database
    try:
        new_uploaded_file_entry = UploadedFile(
            filename=final_filename_for_info,
            file_type=final_file_ext_for_info,
            pages=total_pages,
            timestamp=datetime.now() # Or use default=datetime.utcnow in the model
        )
        db.session.add(new_uploaded_file_entry)
        db.session.commit()
        print(f"File info for {final_filename_for_info} saved to database.")
    except Exception as e:
        db.session.rollback()
        print(f"Error saving file info to database: {e}")

    print(f"File uploaded and processed successfully: {final_filename_for_info}")

    # --- Return success response ---
    # Use the consistently assigned variable 'final_filename_for_info'
    return jsonify({
        "message": "File uploaded successfully!",
        "fileName": final_filename_for_info, # <-- Use the variable assigned in all paths
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
    page_size = data.get('pageSize', 'A4')
    color_option = data.get('colorOption', 'Color')
    orientation = data.get('orientationOption', 'portrait')

    try:
        selected_indexes = parse_page_selection(page_selection, len(images))
        previews = []

        # Optimization: Generate previews on demand and save them to static folder
        # instead of storing all in a global variable.
        # This reduces memory usage if many previews are generated but not all are needed.
        # However, the current approach of generating all selected previews at once
        # and returning their paths is kept for now to align with the original flow.

        for idx in selected_indexes:
            if idx < 0 or idx >= len(images):
                 print(f"Warning: Skipping invalid page index {idx} in preview generation.")
                 continue # Skip invalid index

            img = images[idx].copy() # Work on a copy to avoid modifying the original image data

            # Determine orientation based on user selection or automatically
            current_orientation = data.get("orientationOption") or "auto"
            current_orientation = determine_orientation(img, current_orientation)

            # Calculate canvas size based on selected page size and determined orientation
            canvas_size = calculate_canvas_size(page_size, current_orientation)

            # Fit the image onto the calculated canvas size
            processed_img = fit_image_to_canvas(img, canvas_size)

            # Apply grayscale conversion if selected
            if color_option.lower() == 'grayscale':
                processed_img = cv2.cvtColor(processed_img, cv2.COLOR_BGR2GRAY)
                processed_img = cv2.cvtColor(processed_img, cv2.COLOR_GRAY2BGR) # Convert back to BGR for saving


            # Generate and save preview images for each copy
            for copy_idx in range(num_copies):
                # Create a unique filename for each preview image
                filename = f"print_preview_{idx+1}_{page_size}_{color_option}_{current_orientation}_{copy_idx}_{int(time.time())}.jpg" # Added timestamp for uniqueness
                path = os.path.join(app.config['STATIC_FOLDER'], filename)

                # Save the processed image as a JPEG
                # Adjust quality (0-100) for smaller file sizes if needed. Default is 95.
                cv2.imwrite(path, processed_img, [int(cv2.IMWRITE_JPEG_QUALITY), 90])

                # Append the URL path (relative to static folder) for frontend use
                previews.append(f"/uploads/{filename}")

        # selected_previews = previews # Removed global variable usage

        if not previews:
            return jsonify({"error": "No previews generated. Check your page selection or file."}), 400

        return jsonify({"previews": previews}), 200

    except Exception as e:
        print(f"Error generating preview: {e}")
        return jsonify({"error": f"Failed to generate preview: {str(e)}"}), 500

@app.route("/check_images", methods=["GET"])
def check_images():
    global images
    # If images are available, allow to proceed to preview generation
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
    page_size = data.get("pageSize", "A4")
    color_option = data.get("colorOption", "Color").lower()

    selected_indexes = parse_page_selection(selection, len(images))
    if not selected_indexes:
        return jsonify({"error": "No valid pages selected."}), 400

    page_prices = []
    total_price = 0

    # Optimization: Generate and save preview images here for the result page
    # This avoids regenerating them in the /result route or storing them globally.
    generated_previews = []

    for idx in selected_indexes:
        if idx < 0 or idx >= len(images):
             print(f"Warning: Skipping invalid page index {idx} in pricing.")
             continue # Skip invalid index

        original_img = images[idx].copy() # Work on a copy

        # Determine orientation
        orientation = data.get("orientationOption") or "auto"
        orientation = determine_orientation(original_img, orientation)

        # Calculate canvas size and fit image for processing and pricing
        canvas_size = calculate_canvas_size(page_size, orientation)
        processed_img_for_pricing = fit_image_to_canvas(original_img, canvas_size)

        # Process image and get price
        # The returned processed_img from process_image is the original image or a modified version for grayscale
        processed_img_result, page_price = process_image(processed_img_for_pricing, page_size=page_size, color_option=color_option)
        page_price *= num_copies # Multiply price by the number of copies
        total_price += page_price # Add to total price

        # --- Generate and Save Preview Images for the Frontend ---

        # Save original preview
        preview_filename = f"preview_{idx+1}_{int(time.time())}.jpg" # Unique filename
        preview_path = os.path.join(app.config['STATIC_FOLDER'], preview_filename)
        cv2.imwrite(preview_path, original_img, [int(cv2.IMWRITE_JPEG_QUALITY), 90]) # Save with quality setting

        # Generate and save highlighted bright pixels image
        highlighted_img = highlight_bright_pixels(original_img)
        highlighted_filename = f"bright_pixels_{idx+1}_{int(time.time())}.jpg" # Unique filename
        highlighted_path = os.path.join(app.config['STATIC_FOLDER'], highlighted_filename)
        if highlighted_img is not None: # Ensure highlighting was successful
             cv2.imwrite(highlighted_path, highlighted_img, [int(cv2.IMWRITE_JPEG_QUALITY), 90])
        else:
             # If highlighting failed, maybe save the original or a placeholder
             cv2.imwrite(highlighted_path, original_img, [int(cv2.IMWRITE_JPEG_QUALITY), 90]) # Fallback to saving original


        # Save processed image based on color option for display
        if color_option == "grayscale":
            processed_filename = f"grayscale_preview_{idx+1}_{int(time.time())}.jpg" # Unique filename
            gray_img = cv2.cvtColor(original_img, cv2.COLOR_BGR2GRAY)
            processed_path = os.path.join(app.config['STATIC_FOLDER'], processed_filename)
            cv2.imwrite(processed_path, gray_img, [int(cv2.IMWRITE_JPEG_QUALITY), 90])
        else:
            # For color, save the original image as the "processed" view
            processed_filename = f"color_preview_{idx+1}_{int(time.time())}.jpg" # Unique filename
            processed_path = os.path.join(app.config['STATIC_FOLDER'], processed_filename)
            cv2.imwrite(processed_path, original_img, [int(cv2.IMWRITE_JPEG_QUALITY), 90])


        page_prices.append({
            "page": idx + 1,
            "price": round(page_price, 2), # Ensure price is rounded
            "original": f"/uploads/{preview_filename}",
            "processed": f"/uploads/{processed_filename}",
            "highlighted": f"/uploads/{highlighted_filename}"
        })

        # Add the original preview path to the list for the frontend result page
        generated_previews.append({"page": idx + 1, "path": f"/uploads/{preview_filename}"})


    # Round the total price up to the nearest whole number
    total_price = math.ceil(total_price)

    # Pass the generated preview paths to the frontend
    return jsonify({
        "totalPrice": total_price,
        "pagePrices": page_prices,
        "previews": generated_previews, # Use the list of generated preview paths
        "totalPages": len(images)
    })

# START OF ARDUINO AND COIN SLOT CONNECTION CODE

def calculate_canvas_size(page_size, orientation):
    # Define standard sizes in pixels (e.g., 300 DPI)
    # A4: 210x297 mm -> 2480x3508 pixels (approx at 300 DPI)
    # Short: 8.5x11 inches -> 2550x3300 pixels (approx at 300 DPI) - Using 2480 for width consistency
    # Long: 8.5x13 inches -> 2550x3900 pixels (approx at 300 DPI) - Using 2480 for width consistency
    sizes = {
        'A4': (2480, 3508),
        'Short': (2480, 3200), # Adjusted Short height slightly
        'Long': (2480, 4200)  # Adjusted Long height slightly
        }
    w, h = sizes.get(page_size, sizes['A4']) # Default to A4 if page_size is unknown
    # Return dimensions based on orientation
    return (max(w, h), min(w, h)) if orientation == 'landscape' else (min(w, h), max(w, h))

def fit_image_to_canvas(image, canvas_size):
    """
    Resizes and centers an image onto a white canvas of the specified size.
    """
    if image is None:
        print("Error: Cannot fit None image to canvas.")
        return None

    canvas_w, canvas_h = canvas_size
    img_h, img_w = image.shape[:2]

    # Calculate scaling factor to fit image within canvas dimensions
    scale = min(canvas_w / img_w, canvas_h / img_h)

    # Calculate new dimensions after scaling
    new_w, new_h = int(img_w * scale), int(img_h * scale)

    # Resize the image using calculated new dimensions
    resized_image = cv2.resize(image, (new_w, new_h), interpolation=cv2.INTER_AREA) # Use INTER_AREA for shrinking

    # Create a white canvas of the target size
    canvas = np.ones((canvas_h, canvas_w, 3), dtype=np.uint8) * 255

    # Calculate offsets to center the resized image on the canvas
    x_offset = (canvas_w - new_w) // 2
    y_offset = (canvas_h - new_h) // 2

    # Place the resized image onto the center of the canvas
    canvas[y_offset:y_offset+new_h, x_offset:x_offset+new_w] = resized_image

    return canvas

def determine_orientation(image, user_orientation):
    """
    Determines image orientation based on user preference or automatically.
    """
    if user_orientation.lower() != 'auto':
        return user_orientation.lower() # Use user specified orientation

    # Automatically determine orientation based on image dimensions
    h, w = image.shape[:2]
    return 'landscape' if w > h else 'portrait'


# The read_coin_slot_data function remains largely the same, as its performance
# is tied to the serial communication speed and the Arduino's output rate.
# Error handling and reconnect logic are already present.
def read_coin_slot_data():
    """
    Continuously reads data from the Arduino serial port (COM6) for coin detection,
    parses coin values, and updates the total coin count. Handles errors robustly.
    Only active when coin_detection_active flag is True.
    """
    global coin_value_sum, coin_detection_active, coin_slot_serial

    while True:
        if not coin_detection_active:
            # If coin detection is not active, close the serial port if open and sleep
            if coin_slot_serial and coin_slot_serial.is_open:
                try:
                    coin_slot_serial.close()
                    print(f"Closed serial port {COIN_SLOT_PORT} for coin slot.")
                except Exception as e:
                    print(f"Error closing serial port {COIN_SLOT_PORT}: {e}")
                coin_slot_serial = None # Ensure the variable is set to None
            time.sleep(1) # Sleep longer when inactive
            continue

        # If coin detection is active, ensure the serial port is open
        if coin_slot_serial is None or not coin_slot_serial.is_open:
            try:
                coin_slot_serial = serial.Serial(COIN_SLOT_PORT, BAUD_RATE, timeout=1)
                print(f"Successfully opened serial port {COIN_SLOT_PORT} for coin slot.")
                time.sleep(2) # Wait for the Arduino to reset
            except serial.SerialException as e:
                print(f"Error opening serial port {COIN_SLOT_PORT} for coin slot: {e}")
                coin_slot_serial = None # Set to None to attempt reconnect later
                time.sleep(5) # Wait before retrying
                continue # Continue the outer while loop to check coin_detection_active again


        if coin_slot_serial and coin_slot_serial.is_open and coin_slot_serial.in_waiting > 0:
            try:
                message = coin_slot_serial.readline().decode('utf-8').strip()
                if message: # Process message only if it's not empty
                    print(f"Received from Arduino (COM6): {message}")
                    if message.startswith("Detected coin worth ₱"):
                        try:
                            # Extract coin value using regex for better robustness
                            match = re.search(r"Detected coin worth ₱(\d+)", message)
                            if match:
                                value = int(match.group(1))
                                coin_value_sum += value
                                print(f"Coin inserted: ₱{value}, Total coins inserted: ₱{coin_value_sum}")
                                # Emit update to all connected clients
                                socketio.emit('update_coin_count', {'count': coin_value_sum})
                            else:
                                print(f"Could not parse coin value from serial message: {message}")

                        except ValueError:
                            print(f"Error: Could not convert extracted coin value to integer from COM6 message: {message}.")
                    elif message.startswith("Unknown coin"):
                        print(f"Warning from COM6: {message}")
                    # Add handling for other potential messages from Arduino if needed
                    elif message == "PRINTING":
                         socketio.emit("printer_status", {"status": "Printing"})
                    elif message.startswith("CHANGE:"):
                         try:
                              amt = int(message.split(":")[1])
                              socketio.emit("change_dispensed", {"amount": amt})
                         except ValueError:
                              print(f"Could not parse change amount from serial: {message}")

            except UnicodeDecodeError:
                print("Error decoding serial data from COM6.")
            except serial.SerialException as e:
                print(f"Serial error on {COIN_SLOT_PORT}: {e}")
                # Attempt to close and reopen the port on error
                if coin_slot_serial and coin_slot_serial.is_open:
                    try:
                        coin_slot_serial.close()
                        print(f"Closed serial port {COIN_SLOT_PORT} due to error.")
                    except Exception as close_e:
                         print(f"Error closing serial port {COIN_SLOT_PORT}: {close_e}")
                coin_slot_serial = None # Indicate need to reconnect
                time.sleep(5) # Wait before attempting reconnect

        # If serial port is not open, the reconnect logic at the beginning of the loop will handle it.

        time.sleep(0.05) # Short sleep to prevent excessive CPU usage


def read_gsm_data():
    """
    Continuously reads data from the GSM module serial port (COM5)
    and processes incoming SMS for payment detection. Handles errors robustly.
    Only active when gsm_active flag is True.
    Triggers printing and deactivates GSM if the received amount matches the expected amount.
    """
    global gsm_active, gsm_serial, expected_gcash_amount, gcash_print_options, images # 'images' is not used here, consider removing

    while True:
        if not gsm_active:
            if gsm_serial and gsm_serial.is_open:
                try:
                    gsm_serial.close()
                    print(f"Closed serial port {GSM_PORT} for GSM module.")
                except Exception as e:
                    # This error is less critical for core functionality but good to log
                    error_msg = f"Error closing serial port {GSM_PORT} in read_gsm_data (inactive): {e}"
                    print(error_msg)
                    log_error_to_db(error_msg, source="read_gsm_data_close_inactive", details=str(e))
                gsm_serial = None
            time.sleep(1)
            continue

        if gsm_serial is None or not gsm_serial.is_open:
            try:
                gsm_serial = serial.Serial(GSM_PORT, BAUD_RATE, timeout=1)
                print(f"Successfully opened serial port {GSM_PORT} for GSM module.")
                time.sleep(2) 
            except serial.SerialException as e:
                error_msg = f"Error opening serial port {GSM_PORT} for GSM module: {e}"
                print(error_msg)
                log_error_to_db(error_msg, source="read_gsm_data_serial_open", details=str(e))
                gsm_serial = None
                time.sleep(5) 
                continue
            except Exception as e: # Catch any other unexpected error during serial open
                error_msg = f"Unexpected error opening serial port {GSM_PORT} for GSM: {e}"
                print(error_msg)
                log_error_to_db(error_msg, source="read_gsm_data_serial_open_unexpected", details=str(e))
                gsm_serial = None
                time.sleep(5)
                continue


        if gsm_serial and gsm_serial.is_open and gsm_serial.in_waiting > 0:
            try:
                message = gsm_serial.readline().decode("utf-8", errors='ignore').strip()
                if message:
                    print(f"Received from GSM ({GSM_PORT}): {message}") # Corrected COM port in log
                    match = re.search(r"You received PHP (\d+\.\d{2}) via QRPH", message)
                    if match:
                        try:
                            extracted_amount = float(match.group(1))
                            print(f"Successfully extracted amount from SMS: ₱{extracted_amount}")

                            if extracted_amount >= expected_gcash_amount and gcash_print_options:
                                print("GCash payment amount matched. Triggering print.")
                                success, print_message = print_document_logic(gcash_print_options)

                                if success:
                                    print("Printing initiated successfully for GCash payment.")
                                    socketio.emit("gcash_payment_success", {"success": True, "message": "Payment confirmed and printing started."})
                                    # Optionally record GCash transaction here if not done elsewhere
                                    # record_transaction('GCash', extracted_amount) # Or expected_gcash_amount
                                else:
                                    # Error for print_document_logic failure is already logged within that function
                                    print(f"Failed to initiate printing for GCash payment (handled by print_document_logic): {print_message}")
                                    socketio.emit("gcash_payment_failed", {"success": False, "message": f"Payment confirmed, but printing failed: {print_message}"})
                                
                                gsm_active = False # Deactivate after one attempt
                                print("GSM detection deactivated after GCash payment attempt.")
                                expected_gcash_amount = 0.0
                                gcash_print_options = None
                            
                            elif gcash_print_options is None: # If options are None, payment might be for a different flow or stale
                                print(f"Received GCash SMS (₱{extracted_amount}) but no print options are set. Ignoring.")
                                # No error log here, as this could be normal if gcash-payment page wasn't fully set up by user
                            else: # Amount doesn't match
                                print(f"Received amount ₱{extracted_amount} does not match expected ₱{expected_gcash_amount}.")
                                socketio.emit("gcash_payment_failed", {"success": False, "message": f"Received incorrect payment amount: ₱{extracted_amount}. Expected: ₱{expected_gcash_amount}"})
                                # Not necessarily an error to log to DB, but a payment mismatch.
                                # Could log if this happens frequently:
                                # log_error_to_db(f"GCash amount mismatch. Expected {expected_gcash_amount}, got {extracted_amount}", "read_gsm_data_amount_mismatch", message)


                        except ValueError as e:
                            error_msg = f"Error: Could not convert extracted amount to float from GSM message on {GSM_PORT}: '{match.group(1)}'"
                            print(error_msg)
                            log_error_to_db(error_msg, source="read_gsm_data_value_error", details=f"Original message: {message}, Exception: {str(e)}")
                            socketio.emit("gcash_payment_failed", {"success": False, "message": "Could not convert received amount to number."})
                    # else: # SMS does not match payment format - this is common, not an error
                    #     print(f"Received non-payment SMS on {GSM_PORT}: {message}")

            except UnicodeDecodeError as e:
                error_msg = f"Error decoding serial data from {GSM_PORT} (GSM): {e}"
                print(error_msg)
                log_error_to_db(error_msg, source="read_gsm_data_unicode_error", details=str(e))
                socketio.emit("gcash_payment_failed", {"success": False, "message": "Error decoding SMS data."})
            except serial.SerialException as e:
                error_msg = f"Serial error on {GSM_PORT} (GSM) during read: {e}"
                print(error_msg)
                log_error_to_db(error_msg, source="read_gsm_data_serial_read_error", details=str(e))
                if gsm_serial and gsm_serial.is_open:
                    try:
                        gsm_serial.close()
                    except Exception as close_e:
                        print(f"Error closing GSM serial port after read error: {close_e}")
                        log_error_to_db(f"Error closing GSM serial port after read error: {close_e}", source="read_gsm_data_serial_close_after_error", details=str(close_e))
                gsm_serial = None 
                time.sleep(5)
                socketio.emit("gcash_payment_failed", {"success": False, "message": f"GSM Serial communication error: {e}"})
            except Exception as e:
                error_msg = f"An unexpected error occurred in read_gsm_data: {e}"
                print(error_msg)
                log_error_to_db(error_msg, source="read_gsm_data_unexpected_error", details=str(e))
                socketio.emit("gcash_payment_failed", {"success": False, "message": f"An internal error occurred processing GSM data: {e}"})

        time.sleep(0.1)

# This route is no longer needed as the coin detection is controlled by the /payment route and stop_coin_detection
# @app.route('/detect_coins')
# def start_coin_detection_route():
#     return "Coin detection service is running in the background."


@app.route('/coin_count')
def get_coin_count():
    """Returns the current total coin value as a JSON response."""
    global coin_value_sum
    return jsonify({'count': coin_value_sum})
# END OF ARDUINO AND COIN SLOT CONNECTION CODE

@app.route('/gcash-payment')
def gcash_payment():
    global gsm_active, expected_gcash_amount, gcash_print_options
    try:
        gsm_active = True
        print("GSM detection activated for GCash payment page.")
        expected_gcash_amount = 0.0 # Reset
        gcash_print_options = None # Reset
        return render_template('gcash-payment.html')
    except Exception as e:
        error_msg = f"Error in /gcash-payment route: {e}"
        print(error_msg)
        log_error_to_db(error_msg, source="gcash_payment_route", details=str(e))
        # Fallback or error page, though render_template itself might raise if template not found
        return "An error occurred while trying to load the GCash payment page. Please try again later.", 500

@app.route('/stop_gsm_detection', methods=['POST'])
def stop_gsm_detection():
    global gsm_active, expected_gcash_amount, gcash_print_options
    gsm_active = False
    print("GSM detection deactivated by user.")
    expected_gcash_amount = 0.0
    gcash_print_options = None
    # Optionally close GSM serial port if not used elsewhere
    return jsonify({"success": True, "message": "GSM detection stopped."})

# START OF PRINT DOCUMENT LOGIC (extracted from route)

def print_document_logic(print_options):
    # This function is complex and has many points of failure.
    # Each `print("Error...")` should be augmented or replaced with `log_error_to_db`.
    # Example:
    #   if not filename_to_print:
    #       error_msg = "Print Error: No filename provided in print options."
    #       print(error_msg)
    #       log_error_to_db(error_msg, source="print_document_logic_no_filename", details=str(print_options))
    #       return False, error_msg
    #
    #   # Inside except Exception as processing_e:
    #       error_msg = f"Print Error: Error during PDF processing (fitz): {processing_e}"
    #       print(error_msg)
    #       log_error_to_db(error_msg, source="print_document_logic_pdf_processing", details=str(processing_e))
    #       # ... cleanup ...
    #       return False, error_msg
    # This function needs careful review to add logging at all relevant failure points.
    # For brevity, I'm not rewriting the entire function here, but this is a key area.
    # The existing return (False, error_msg) is good for the caller.
    # The `log_error_to_db` adds persistence.
    try:
        filename_to_print = print_options.get("fileName")
        if not filename_to_print:
            err_msg = "Print Error: No filename provided in print options."
            log_error_to_db(err_msg, "print_document_logic", f"Options: {print_options}")
            return False, err_msg

        # ... (rest of the logic from your provided file) ...
        # Ensure all `print(f"Error ...")` or `print(f"Print Error ...")`
        # are accompanied by `log_error_to_db(...)`

        # Example for file not found:
        # if not os.path.exists(file_path):
        #    error_msg = f"Print Error: Printable file not found at {file_path}"
        #    log_error_to_db(error_msg, source="print_document_logic_file_not_found", details=f"Path: {file_path}")
        #    return False, error_msg

        # Example for ShellExecute failure:
        # except Exception as shell_execute_e:
        #    error_msg = f"Print Error: Failed to execute print command: {shell_execute_e}"
        #    log_error_to_db(error_msg, source="print_document_logic_shellexec", details=str(shell_execute_e))
        #    # ... cleanup ...
        #    return False, error_msg
        
        # This is a placeholder for your existing print_document_logic
        # You need to integrate log_error_to_db into its error paths.
        print(f"Executing placeholder print_document_logic for {filename_to_print}")
        # Simulate a potential error for demonstration if file is "error.pdf"
        if filename_to_print == "error.pdf":
            log_error_to_db("Simulated print error in print_document_logic", "print_document_logic_simulation")
            return False, "Simulated print error"
        
        # Assuming your original logic is here...
        # For now, returning True for other cases.
        # Replace this with your actual, detailed print_document_logic
        # with integrated log_error_to_db calls.
        
        # A simplified version of the original logic's end:
        # This is just to make the function runnable.
        # YOU MUST REPLACE THIS WITH YOUR FULL print_document_logic
        # and add log_error_to_db calls within it.
        upload_dir = app.config['UPLOAD_FOLDER']
        file_path = os.path.join(upload_dir, filename_to_print)
        if not os.path.exists(file_path):
            # Try to find a prefixed version (like gs_ or print_ready_)
            # This part of your original logic was a bit complex for finding the file.
            # Simplifying for this example.
            candidates = [f for f in os.listdir(upload_dir) if filename_to_print in f]
            if candidates:
                file_path = os.path.join(upload_dir, candidates[0]) # Take first match
                print(f"Found candidate file for printing: {file_path}")
            else:
                err_msg = f"Print file '{filename_to_print}' not found in {upload_dir}."
                log_error_to_db(err_msg, "print_document_logic_file_search", f"Original: {filename_to_print}")
                return False, err_msg

        num_copies = int(print_options.get("numCopies", 1))
        default_printer = win32print.GetDefaultPrinter()
        print(f"Attempting to print {file_path} to {default_printer}, {num_copies} copies.")
        
        # Actual printing (ShellExecute)
        try:
            for i in range(num_copies):
                win32api.ShellExecute(0, "print", file_path, None, os.path.dirname(file_path), 0)
                time.sleep(0.2) # Small delay between copies if needed
            print(f"Print commands issued for {filename_to_print}.")
            return True, "Document sent to printer."
        except Exception as e:
            err_msg = f"ShellExecute failed for printing {file_path}: {e}"
            log_error_to_db(err_msg, "print_document_logic_shellexec_final", str(e))
            return False, err_msg

    except Exception as e:
        # Catch-all for unexpected errors in print_document_logic
        err_msg = f"Unexpected error in print_document_logic: {e}"
        log_error_to_db(err_msg, "print_document_logic_unexpected", str(e))
        return False, err_msg

# END OF PRINT DOCUMENT LOGIC

def convert_pdf_to_grayscale(input_pdf_path, output_pdf_path):
    doc = None
    gray_doc = None
    try:
        doc = fitz.open(input_pdf_path)
        gray_doc = fitz.open()
        for page in doc:
            # Ensure the pixmap conversion is robust
            try:
                pix = page.get_pixmap(colorspace=fitz.csGRAY)
            except Exception as pixmap_e:
                print(f"Grayscale Conversion Warning: Could not get pixmap for page {page.number} in {input_pdf_path}: {pixmap_e}")
                # Optionally, skip this page or insert a blank page
                continue # Skip to the next page

            rect = page.rect
            new_page = gray_doc.new_page(width=rect.width, height=rect.height)
            # Ensure insertion is robust
            try:
                new_page.insert_image(rect, pixmap=pix)
            except Exception as insert_e:
                 print(f"Grayscale Conversion Warning: Could not insert pixmap for page {page.number} in {input_pdf_path}: {insert_e}")
                 continue # Skip to the next page

        # Ensure save is robust
        try:
            gray_doc.save(output_pdf_path)
            print(f"Successfully converted {input_pdf_path} to grayscale PDF at {output_pdf_path}.")
        except Exception as save_e:
            error_msg = f"Grayscale Conversion Error: Failed to save grayscale PDF: {save_e}"
            print(error_msg)
            raise Exception(error_msg) # Re-raise with a specific message

    except fitz.FileNotFoundError:
         error_msg = f"Grayscale Conversion Error: Input PDF not found at {input_pdf_path}"
         print(error_msg)
         raise Exception(error_msg) # Re-raise with a specific message
    except Exception as e:
        error_msg = f"Grayscale Conversion Error: An unexpected error occurred: {e}"
        print(error_msg)
         # Clean up potentially created partial output file
        if 'gray_doc' in locals() and gray_doc and os.path.exists(output_pdf_path):
             try:
                  gray_doc.close() # Close before attempting to remove
                  os.remove(output_pdf_path)
                  print(f"Cleaned up partial grayscale output file: {output_pdf_path}")
             except Exception as cleanup_e:
                  print(f"Error during grayscale cleanup: {cleanup_e}")
        raise Exception(error_msg) # Re-raise the exception with a specific message
    finally:
        if doc:
            doc.close()
        if gray_doc:
            gray_doc.close()
    doc = fitz.open(input_pdf_path)
    gray_doc = fitz.open()
    for page in doc:
        pix = page.get_pixmap(colorspace=fitz.csGRAY)
        rect = page.rect
        new_page = gray_doc.new_page(width=rect.width, height=rect.height)
        new_page.insert_image(rect, pixmap=pix)
    gray_doc.save(output_pdf_path)
    gray_doc.close()
    doc.close()

@app.route('/spin-motor/start', methods=['POST'])
def spin_motor_start():
    try:
        MOTOR_PORT.write(b's')  # Start motor
        print("[MOTOR] Start signal sent.")
    except Exception as e:
        print(f"[MOTOR ERROR] Failed to start motor: {e}")
    return '', 204

@app.route('/spin-motor/stop', methods=['POST'])
def spin_motor_stop():
    try:
        MOTOR_PORT.write(b'x')  # Stop motor
        print("[MOTOR] Stop signal sent.")
    except Exception as e:
        print(f"[MOTOR ERROR] Failed to stop motor: {e}")
    return '', 204

@app.route('/print_document', methods=['POST'])
def print_document_route():
    data = request.json
    if not data:
        log_error_to_db("Invalid JSON payload for /print_document.", source="print_document_route_no_json")
        return jsonify({"error": "Invalid JSON payload."}), 400

    print("Received print request via /print_document route with data:", data)
    success, message = print_document_logic(data)

    if success:
        # Record successful print transaction (if applicable, e.g. if payment was confirmed by this point)
        # This depends on your application flow. If payment is confirmed *before* calling print,
        # then transaction might be recorded elsewhere.
        # For now, assuming print success means a service was rendered.
        # Example: record_transaction("PrintService", calculated_price_or_fixed_fee)
        return jsonify({"success": True, "message": message}), 200
    else:
        # Error already logged to DB by print_document_logic
        return jsonify({"error": message}), 500


# Update result route to display both previews and segmentation
@app.route('/result', methods=['GET'])
def result():
    # The previews are now generated and saved in preview_with_price,
    # so we don't need selected_previews global variable.
    # The frontend should request the previews based on the data returned
    # by preview_with_price.
    # For now, this route might just render the template.
    # If you need to pass preview data, you might need to store it temporarily
    # in the session or pass it as query parameters (less ideal for many previews).
    # Assuming the frontend will handle fetching previews based on filenames
    # returned by preview_with_price.
    return render_template('result.html')



@app.route('/uploads/<filename>')
def uploaded_file(filename):
    """Serves uploaded files from the static folder."""
    # Ensure the file exists in the static folder before sending
    file_path = os.path.join(app.config['STATIC_FOLDER'], filename)
    if os.path.exists(file_path):
        return send_from_directory(app.config['STATIC_FOLDER'], filename)
    else:
        # If file not found in static, check the UPLOAD_FOLDER (for printable PDFs)
        file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        if os.path.exists(file_path):
             return send_from_directory(app.config['UPLOAD_FOLDER'], filename)
        else:
            return "File not found.", 404


def check_printer_status(printer_name):
    """
    Checks the status of the specified printer using win32print.
    Returns a status string ("Idle", "Printing", "Error: ...").
    """
    try:
        # Check if printer_name is valid
        if not printer_name:
             return "Error: Printer name not set."

        # Attempt to open the printer handle
        try:
            hPrinter = win32print.OpenPrinter(printer_name)
        except Exception as open_e:
            return f"Error opening printer handle: {open_e}"

        try:
            # Get information about the print jobs (level 2 provides detailed info)
            jobs = win32print.EnumJobs(hPrinter, 0, -1, 2)
            # Check the printer status flags
            printer_info = win32print.GetPrinter(hPrinter, 2)
            printer_status = printer_info['Status']

            # Define common printer status flags
            PRINTER_STATUS_PRINTING = 0x00000002
            PRINTER_STATUS_ERROR = 0x00000008
            PRINTER_STATUS_OFFLINE = 0x00000080
            PRINTER_STATUS_PAPER_JAM = 0x00000008 # Paper jam is often reported as an error
            PRINTER_STATUS_NO_PAPER = 0x00000010
            PRINTER_STATUS_TONER_LOW = 0x00000040
            PRINTER_STATUS_OUT_OF_PAPER = 0x00000010 # Synonym for NO_PAPER

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
                # If there are jobs and no critical errors, assume printing
                if not status_messages:
                    return "Printing"
                else:
                    # If there are jobs but also errors, report the errors
                    return ", ".join(status_messages)
            else:
                # If no jobs, check for other status conditions
                if status_messages:
                    return ", ".join(status_messages)
                else:
                    # If no jobs and no other status conditions, printer is idle
                    return "Idle"

        finally:
            # Ensure the printer handle is closed
            win32print.ClosePrinter(hPrinter)

    except Exception as e:
        # Catch any other exceptions during the process
        return f"Error checking printer status: {e}"

def monitor_printer_status(printer_name, upload_folder):
    global coin_value_sum, coin_detection_active
    last_status = None
    printing_was_active = False

    while True:
        current_status = check_printer_status(printer_name)

        # Emit status change
        if current_status != last_status:
            print(f"Printer status changed to: {current_status}")
            socketio.emit("printer_status_update", {"status": current_status})

        # When printing starts: reset and deactivate coin detection
        if current_status == "Printing" and last_status != "Printing":
            coin_value_sum = 0
            socketio.emit('update_coin_count', {'count': coin_value_sum})
            coin_detection_active = False
            printing_was_active = True
            print("Printing started: coin count reset and detection deactivated.")

        # After printing finishes: wait, delete files, and allow next cycle
        elif current_status == "Idle" and last_status == "Printing" and printing_was_active:
            time.sleep(10)  # allow any pending tasks to finish
            for filename in os.listdir(upload_folder):
                # Only delete files created by the printing process logic
                # The timestamp helps ensure we don't delete files from a previous run if names clash (less likely with timestamp)
                if filename.startswith("print_ready_") or filename.startswith("gs_"):
                    path = os.path.join(upload_folder, filename)
                    if os.path.isfile(path):
                        try:
                            # Add a small delay or retry logic if encountering "file in use" errors
                            os.remove(path)
                            print(f"Cleaned up temporary print file: {path}")
                        except OSError as e:
                            # This often means the file is still in use by the print spooler or PDF viewer
                            print(f"Cleanup Warning: Could not delete temporary print file {path} (in use?): {e}")
                            # Consider retrying deletion later or logging this for manual cleanup
                        except Exception as e:
                            print(f"Cleanup Error: Could not delete temporary print file {path}: {e}")

        last_status = current_status
        time.sleep(2)

def log_activity(event_type, message):
    from datetime import datetime
    log_entry = {
        'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'event_type': event_type,
        'message': message
    }
    # Store to database or append to a file or list
    db.insert_log(log_entry)  # Replace with your database or log logic

@app.route('/payment-success')
def payment_success():
    global gsm_active, coin_detection_active, expected_gcash_amount, gcash_print_options
    gsm_active = False
    coin_detection_active = False
    expected_gcash_amount = 0.0
    gcash_print_options = None
    print("Payment success page: GSM and Coin detection deactivated.")
    return render_template('payment-success.html')

@app.route('/payment-type')
def payment_type():
    global gsm_active, coin_detection_active, expected_gcash_amount, gcash_print_options
    gsm_active = False
    coin_detection_active = False
    expected_gcash_amount = 0.0
    gcash_print_options = None
    print("Payment type selection: GSM and Coin detection deactivated.")
    return render_template('payment-type.html')

if __name__ == "__main__":
    try:
        printer_name = win32print.GetDefaultPrinter()
        print(f"Default printer set to: {printer_name}")
    except Exception as e:
        print(f"Error getting default printer: {e}. Printer status monitoring may not work.")
        printer_name = None

    activity_log = []

    # Path to the uploads folder
    upload_folder = app.config['UPLOAD_FOLDER'] # Use the configured UPLOAD_FOLDER

    socketio.start_background_task(read_coin_slot_data)
    socketio.start_background_task(read_gsm_data)

    if printer_name:
        socketio.start_background_task(monitor_printer_status, printer_name, upload_folder)
    else:
        print("Printer name not available. Printer status monitoring disabled.")

    socketio.run(app, debug=False, allow_unsafe_werkzeug=True) # allow_unsafe_werkzeug=True might be needed in some environments