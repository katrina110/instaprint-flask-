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
from io import BytesIO
import pythoncom
from win32com import client
from fpdf import FPDF
import serial
import time
from datetime import datetime
from PyPDF2 import PdfReader
import threading
from flask_socketio import SocketIO, emit
import win32print
# import multiprocessing # Removed multiprocessing as we'll use threading for printer status
import pywintypes
import wmi
import multiprocessing
import json # Import json for parsing printOptions
import re # Import regex for more robust parsing

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*")

# Configuration
UPLOAD_FOLDER = os.path.join(os.getcwd(), "uploads")
STATIC_FOLDER = os.path.join(os.getcwd(), "static", "uploads")
ALLOWED_EXTENSIONS = {"pdf", "jpg", "jpeg", "png", "docx", "doc"}
printer_name = None  # Initialize printer_name here
activity_log = []
logged_errors = set()  # Set to track logged errors
last_status = None  # Track last printer status

# Store uploaded file info for admin view
uploaded_files_info = []

app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
app.config["STATIC_FOLDER"] = STATIC_FOLDER
app.config["ALLOWED_EXTENSIONS"] = ALLOWED_EXTENSIONS

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(STATIC_FOLDER, exist_ok=True)

# Global Variables
uploaded_files_info = []  # For admin view
printed_pages_today = 0
files_uploaded_today = 0
last_updated_date = datetime.now().date()
images = []  # Store images globally (consider refactoring)
total_pages = 0  # Keep track of total pages.  May not be needed globally.
selected_previews = []  # Store paths to selected preview images.  May not be needed globally.
arduino = None # This might still be used in the arduino_payment_page route, will keep for now.
coin_count = 0
coin_value_sum = 0  # Track the total value of inserted coins
coin_detection_active = False # Flag to control coin detection for COM6
gsm_active = False # Flag to control GSM detection for COM15

# Define COM ports and baud rate for both Arduinos
COIN_SLOT_PORT = 'COM6'
GSM_PORT = 'COM15'
BAUD_RATE = 9600

# Global variables to hold serial port objects
coin_slot_serial = None
gsm_serial = None

# Global variables to store GCash payment details temporarily
expected_gcash_amount = 0.0
gcash_print_options = None


# WebSocket Connection
@socketio.on("connect")
def send_initial_coin_count():
    """Sends the initial coin count to the connected WebSocket client."""
    global coin_value_sum
    print(
        f"WebSocket client connected. Emitting initial coin count: {coin_value_sum}"
    )  # Keep the log
    emit("update_coin_count", {"count": coin_value_sum})

def update_coin_count(count):
    """Emits the updated coin count to all connected WebSocket clients."""
    socketio.emit("update_coin_count", {"count": count})

# New SocketIO handler to receive GCash payment details from frontend
@socketio.on("confirm_gcash_payment")
def handle_confirm_gcash_payment(data):
    """
    Receives expected amount and print options for GCash payment.
    GSM activation is now handled when the /gcash-payment route is accessed.
    """
    global expected_gcash_amount, gcash_print_options
    try:
        expected_gcash_amount = float(data.get('totalPrice', 0.0))
        gcash_print_options = data.get('printOptions', None)
        print(f"GCash payment details received via SocketIO. Expected amount: ₱{expected_gcash_amount}. Print options received: {'Yes' if gcash_print_options else 'No'}")
        emit("gcash_payment_initiated", {"success": True, "message": "Waiting for payment confirmation via SMS."})
    except Exception as e:
        print(f"Error receiving GCash payment details via SocketIO: {e}")
        emit("gcash_payment_initiated", {"success": False, "message": f"Error receiving payment details: {e}"})


# Helper Functions
def allowed_file(filename):
    """Checks if the given filename has an allowed extension."""
    return (
        "." in filename
        and filename.rsplit(".", 1)[1].lower() in app.config["ALLOWED_EXTENSIONS"]
    )


def pdf_to_images(pdf_path):
    """Converts a PDF file to a list of OpenCV images."""
    try:
        document = fitz.open(pdf_path)
        pdf_images = []
        for page_num in range(len(document)):
            page = document.load_page(page_num)
            pix = page.get_pixmap(alpha=False)
            image = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
                (pix.height, pix.width, 3)
            )
            pdf_images.append(cv2.cvtColor(image, cv2.COLOR_RGB2BGR))
        return pdf_images
    except Exception as e:
        print(f"Error processing PDF: {e}")
        return []
    finally:
        if document:
            document.close()  # Ensure document is closed


def docx_to_images(file_path):
    """Converts a Word document (doc or docx) to a list of OpenCV images."""
    if not file_path.lower().endswith((".doc", ".docx")):
        raise ValueError("Unsupported file type for Word documents.")

    try:
        pythoncom.CoInitialize()
        word = client.Dispatch("Word.Application")
        word.visible = False
        doc = word.Documents.Open(file_path)
        pdf_path = file_path.replace(".docx", ".pdf").replace(".doc", ".pdf")
        doc.SaveAs(pdf_path, FileFormat=17)  # 17 is PDF
        doc.Close()
        word.Quit()
        images = pdf_to_images(pdf_path)
        return images
    except Exception as e:
        print(f"Error processing Word document: {e}")
        return []
    finally:
        pythoncom.CoUninitialize()  # Ensure COM is uninitialized
        if os.path.exists(pdf_path):
            os.remove(pdf_path)  # Clean up temp PDF


def parse_page_selection(selection, total_pages):
    """
    Parses a page selection string like '1-3,5,7-9' into a sorted list
    of unique page indices (0-based).
    """
    import re

    pages = set()
    if not selection:
        return list(range(total_pages))  # Default: all pages

    parts = re.split(r",\s*", selection)
    for part in parts:
        if "-" in part:
            start, end = map(int, part.split("-"))
            pages.update(range(start - 1, end))  # 0-based indexing
        elif part.isdigit():
            page_num = int(part)
            if 1 <= page_num <= total_pages:
                pages.add(page_num - 1)  # 0-based indexing
    return sorted(p for p in pages if 0 <= p < total_pages)

def highlight_bright_pixels(image, dark_threshold=50):

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
        print("Error: Unable to load image.")
        return None, 0.0

    paper_costs = {"Short": 2.0, "A4": 3.0, "Long": 5.0}
    paper_cost = paper_costs.get(page_size, 195 / 500)  # Default cost

    max_price_caps = {"Short": 15.0, "A4": 18.0, "Long": 20.0}
    max_cap = max_price_caps.get(page_size, 18.0)  # Default max

    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    total_pixels = gray.size
    white_ratio = np.sum(gray > 220) / total_pixels
    dark_text_ratio = np.sum((gray > 10) & (gray < 180)) / total_pixels

    # Text-only document
    if white_ratio > 0.85 and dark_text_ratio < 0.15:
        text_only_prices = {"Short": 3.0, "A4": 3.0, "Long": 4.0}
        return image, text_only_prices.get(page_size, 3.0)

    # White page
    if np.all(image == 255):
        return image, 0.0

    # Full black page
    if np.all(image == 0):
        base_rate = 0.000006
        total_pixels = image.shape[0] * image.shape[1]
        base_cost = total_pixels * base_rate + paper_cost
        return image, round(min(base_cost * 1.5, max_cap), 2)

    if color_option == "Grayscale":
        edges = cv2.Canny(gray, 50, 150)
        edge_density = np.sum(edges > 0) / total_pixels
        dark_ratio = np.sum(gray < 100) / total_pixels

        hist = cv2.calcHist([gray], [0], None, [256], [0, 256])
        hist_norm = hist.ravel() / hist.sum()
        entropy = -np.sum(hist_norm * np.log2(hist_norm + 1e-7))

        if white_ratio > 0.85 and dark_text_ratio < 0.15:
            base_price = 1.0  # logo + text
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

    # Color logic
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

# Printer real-time status
def get_printer_status_wmi():
    global printer_name, activity_log, logged_errors
    c = wmi.WMI()
    logs = []
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
        for printer in c.Win32_Printer(Name=printer_name):
            status_msg = status_map.get(printer.PrinterStatus, "Unknown")
            error_msg = error_state_map.get(printer.DetectedErrorState, None)

            messages = []

            if error_msg and error_msg != "Unknown":
                messages.append(error_msg)
            elif status_msg and status_msg not in ["Idle", "Unknown"]:
                messages.append(status_msg)

            for msg in messages:
                if msg not in logged_errors:
                    log = {
                        "date": now.strftime("%m/%d/%Y"),
                        "time": now.strftime("%I:%M %p"),
                        "event": msg
                    }
                    activity_log.append(log)
                    logs.append(log)
                    logged_errors.add(msg)

    except Exception as e:
        error_msg = f"WMI error: {e}"
        if error_msg not in logged_errors:
            log = {
                "date": now.strftime("%m/%d/%Y"),
                "time": now.strftime("%I:%M %p"),
                "event": error_msg
            }
            activity_log.append(log)
            logs.append(log)
            logged_errors.add(error_msg)

    return logs


# Routes - Admin
@app.route("/")
def admin_user():
    return render_template("admin-user.html")


@app.route("/admin-dashboard")
def admin_dashboard():
    data = {
        "total_sales": 5000,
        "printed_pages": printed_pages_today,
        "files_uploaded": files_uploaded_today,
        "current_balance": 3000,
        "sales_history": [
            {"method": "GCash", "amount": 500, "date": "2024-03-30", "time": "14:00"},
            {"method": "PayMaya", "amount": 250, "date": "2024-03-29", "time": "11:30"},
        ],
        "sales_chart": [500, 600, 700, 800],  # Example sales data for Chart.js
    }
    return render_template("admin-dashboard.html", data=data)


@app.route("/admin-files-upload")
def admin_printed_pages():
    return render_template("admin-files-upload.html", uploaded=uploaded_files_info)


@app.route("/admin-activity-log")
def admin_activity_log():
    return render_template('admin-activity-log.html', printed=activity_log)

@app.route('/api/printer-status')
def printer_status_api():
    global printer_name  # Declare printer_name as global here
    if printer_name is None:  # Initialize printer_name if not already initialized
        printer_name = win32print.GetDefaultPrinter()
    return jsonify(get_printer_status_wmi())


@app.route("/admin-balance")
def admin_balance():
    return render_template("admin-balance.html")


@app.route("/admin-feedbacks")
def admin_feedbacks():
    feedbacks = [
        {"user": "Alice", "message": "Loved the service!", "time": "1:42 PM", "rating": 5},
        {"user": "Bob", "message": "Could be faster.", "time": "2:55 PM", "rating": 3},
        {"user": "Charlie", "message": "Great UI/UX.", "time": "4:10 PM", "rating": 4},
        {"user": "Dana", "message": "Friendly support team.", "time": "5:20 PM", "rating": 4},
        {"user": "Eli", "message": "Quick response time!", "time": "6:12 PM", "rating": 5},
        {"user": "Faye", "message": "Highly recommended!", "time": "7:45 PM", "rating": 5},
    ]
    return render_template("admin-feedbacks.html", feedbacks=feedbacks)


# Routes - User Interface
@app.route("/file-upload")
def file_upload():
    return render_template("file-upload.html")


@app.route("/index")
def index():
    return render_template("index.html")

@app.route("/manual-upload")
def manual_upload():
    files = os.listdir(UPLOAD_FOLDER)  # List all files in the upload folder
    return render_template("manual-display-upload.html", files=files)


@app.route("/online-upload", methods=["GET", "POST"])
def online_upload():
    if request.method == "POST":
        file = request.files.get("file")
        if file:
            filename = file.filename
            file.save(os.path.join(UPLOAD_FOLDER, filename))
            return f'File "{filename}" uploaded successfully!'
        else:
            return "No file selected!", 400
    else:
        files = os.listdir(UPLOAD_FOLDER)
        toffee_share_link = "https://toffeeshare.com/nearby"
        return render_template(
            "upload_page.html", files=files, toffee_share_link=toffee_share_link
        )



@app.route("/payment")
def payment_page():
    """Renders the payment page and activates coin detection."""
    global coin_detection_active
    coin_detection_active = True
    print("Coin detection activated for payment.")
    # The background task for coin detection is started in __main__ and will now become active
    return render_template("payment.html")


@app.route("/stop_coin_detection", methods=["POST"])
def stop_coin_detection():
    """Deactivates coin detection."""
    global coin_detection_active
    coin_detection_active = False
    print("Coin detection deactivated.")
    return jsonify({"success": True, "message": "Coin detection stopped."})

# File Upload and Processing
@app.route("/upload", methods=["POST"])
def upload_file():
    """Handles file uploads, converts them to images, and stores file information."""
    global images, total_pages
    global printed_pages_today, files_uploaded_today, last_updated_date
    global uploaded_files_info

    # Reset daily stats if the date has changed
    if datetime.now().date() != last_updated_date:
        printed_pages_today = 0
        files_uploaded_today = 0
        last_updated_date = datetime.now().date()

    if "file" not in request.files:
        return jsonify({"error": "No file part"}), 400

    file = request.files["file"]
    if file.filename == "":
        return jsonify({"error": "No selected file"}), 400

    if file and allowed_file(file.filename):
        filename = file.filename
        file_ext = filename.split(".")[-1].lower()
        filepath = os.path.join(UPLOAD_FOLDER, filename)
        file.save(filepath)

        if file_ext == "pdf":
            images = pdf_to_images(filepath)
            total_pages = len(images)

        elif file_ext in ("jpg", "jpeg", "png"):
            images = [cv2.imread(filepath)]
            total_pages = 1

        elif file_ext in ("docx", "doc"):
            images = docx_to_images(filepath)
            total_pages = len(images)

        else:
            os.remove(filepath)  # Clean up unsupported file
            return jsonify({"error": "Unsupported file format"}), 400

        # Update daily stats
        printed_pages_today += total_pages
        files_uploaded_today += 1

        # Store uploaded file info
        uploaded_files_info.append(
            {"file": filename, "type": file_ext, "pages": total_pages, "time": datetime.now().strftime('%I:%M %p')}
        )

        return jsonify(
            {
                "message": "File uploaded successfully!",
                "fileName": filename,
                "totalPages": total_pages,
            }
        ), 200

    return jsonify({"error": "Invalid file format"}), 400


@app.route("/generate_preview", methods=["POST"])
def generate_preview():
    global images, selected_previews
    data = request.json
    page_selection = data.get('pageSelection', '')
    num_copies = int(data.get('numCopies', 1))
    page_size = data.get('pageSize', 'A4')
    color_option = data.get('colorOption', 'Color')
    orientation = data.get('orientationOption', 'portrait')

    try:
        selected_indexes = parse_page_selection(page_selection, len(images))
        previews = []

        for idx in selected_indexes:
            img = images[idx].copy()
            orientation = data.get("orientationOption") or "auto"
            orientation = determine_orientation(img, orientation)
            canvas_size = calculate_canvas_size(page_size, orientation)
            processed_img = fit_image_to_canvas(img, canvas_size)

            if color_option.lower() == 'grayscale':
                processed_img = cv2.cvtColor(processed_img, cv2.COLOR_BGR2GRAY)
                processed_img = cv2.cvtColor(processed_img, cv2.COLOR_GRAY2BGR)

            for copy_idx in range(num_copies):
                filename = f"print_preview_{idx+1}_{page_size}_{color_option}_{copy_idx}.jpg"
                path = os.path.join(app.config['STATIC_FOLDER'], filename)
                cv2.imwrite(path, processed_img)
                # Ensure correct URL path for frontend
                previews.append(f"/uploads/{filename}")

        selected_previews = previews

        if not previews:
            return jsonify({"error": "No previews generated. Check your page selection."}), 400

        return jsonify({"previews": previews}), 200

    except Exception as e:
        print(f"Error generating preview: {e}")
        return jsonify({"error": f"Failed to generate preview: {str(e)}"}), 500

@app.route("/check_images", methods=["GET"])
def check_images():
    global images
    # If images are available but previews aren't generated yet, allow to proceed
    return jsonify({"imagesAvailable": bool(images)})



@app.route('/preview_with_price', methods=['POST'])
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

    for idx in selected_indexes:
        original_img = images[idx].copy()
        orientation = data.get("orientationOption") or "auto"
        orientation = determine_orientation(original_img, orientation)
        canvas_size = calculate_canvas_size(page_size, orientation)
        processed_img = fit_image_to_canvas(original_img, canvas_size)

        processed_img, page_price = process_image(processed_img, page_size=page_size, color_option=color_option)
        page_price *= num_copies
        total_price += page_price

        # Save original preview
        preview_filename = f"preview_{idx+1}.jpg"
        preview_path = os.path.join(app.config['STATIC_FOLDER'], preview_filename)
        cv2.imwrite(preview_path, original_img)

        # Generate and save highlighted bright pixels image
        highlighted_img = highlight_bright_pixels(original_img)
        highlighted_filename = f"bright_pixels_{idx+1}.jpg"
        highlighted_path = os.path.join(app.config['STATIC_FOLDER'], highlighted_filename)
        cv2.imwrite(highlighted_path, highlighted_img)

        # Save processed image based on color option
        if color_option == "grayscale":
            processed_filename = f"grayscale_preview_{idx+1}.jpg"
            gray_img = cv2.cvtColor(original_img, cv2.COLOR_BGR2GRAY)
            processed_path = os.path.join(app.config['STATIC_FOLDER'], processed_filename)
            cv2.imwrite(processed_path, gray_img)
        else:
            processed_filename = f"segmented_{idx+1}.jpg"
            processed_path = os.path.join(app.config['STATIC_FOLDER'], processed_filename)
            cv2.imwrite(processed_path, processed_img)

        page_prices.append({
            "page": idx + 1,
            "price": page_price,
            "original": f"/uploads/{preview_filename}",
            "processed": f"/uploads/{processed_filename}",
            "highlighted": f"/uploads/{highlighted_filename}"  # 👈 ADD THIS!
        })


    return jsonify({
    "totalPrice": round(total_price, 2),
    "pagePrices": page_prices,
    "previews": [{"page": p["page"], "path": p["original"]} for p in page_prices],
    "totalPages": len(images)    # <<< add this
    })




# START OF ARDUINO AND COIN SLOT CONNECTION CODE

def calculate_canvas_size(page_size, orientation):
    sizes = {'A4': (2480, 3508), 'Short': (2480, 3200), 'Long': (2480, 4200)}
    w, h = sizes.get(page_size, (2480, 3508))
    return (max(w, h), min(w, h)) if orientation == 'landscape' else (min(w, h), max(w, h))

def fit_image_to_canvas(image, canvas_size):
    canvas_w, canvas_h = canvas_size
    img_h, img_w = image.shape[:2]
    scale = min(canvas_w / img_w, canvas_h / img_h)
    new_w, new_h = int(img_w * scale), int(img_h * scale)
    resized_image = cv2.resize(image, (new_w, new_h))

    # Create white canvas and center the image
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

def fit_image_to_paper(image, target_size):
    h, w = image.shape[:2]
    target_w, target_h = target_size
    scale = min(target_w / w, target_h / h)
    new_w, new_h = int(w * scale), int(h * scale)
    return cv2.resize(image, (new_w, new_h))


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
                print(f"Received from Arduino (COM6): {message}")
                if message.startswith("Detected coin worth ₱"):
                    try:
                        value = int(message.split("₱")[1])
                        coin_value_sum += value
                        print(f"Coin inserted: ₱{value}, Total coins inserted: ₱{coin_value_sum}")
                        socketio.emit('update_coin_count', {'count': coin_value_sum})
                    except ValueError:
                        print("Error: Could not parse coin value from COM6.")
                elif message.startswith("Unknown coin"):
                    print(f"Warning from COM6: {message}")
            except UnicodeDecodeError:
                print("Error decoding serial data from COM6.")
            except serial.SerialException as e:
                print(f"Serial error on {COIN_SLOT_PORT}: {e}")
                if coin_slot_serial and coin_slot_serial.is_open:
                    coin_slot_serial.close()
                coin_slot_serial = None # Indicate need to reconnect
                time.sleep(5) # Wait before attempting reconnect

        elif coin_slot_serial is None or not coin_slot_serial.is_open:
             # Attempt to reconnect if the port was closed
             try:
                 coin_slot_serial = serial.Serial(COIN_SLOT_PORT, BAUD_RATE, timeout=1)
                 print(f"Successfully re-opened serial port {COIN_SLOT_PORT}.")
                 time.sleep(2) # Wait for Arduino
             except serial.SerialException as e:
                 print(f"Failed to re-open serial port {COIN_SLOT_PORT}: {e}")
                 coin_slot_serial = None
                 time.sleep(5) # Wait before next retry


        time.sleep(0.1) # Short sleep to prevent excessive CPU usage


def read_gsm_data():
    """
    Continuously reads data from the GSM module serial port (COM15)
    and processes incoming SMS for payment detection. Handles errors robustly.
    Only active when gsm_active flag is True.
    Triggers printing and deactivates GSM if the received amount matches the expected amount.
    """
    global gsm_active, gsm_serial, expected_gcash_amount, gcash_print_options, images

    while True:
        if not gsm_active:
            # If GSM is not active, close the serial port if open and sleep
            if gsm_serial and gsm_serial.is_open:
                try:
                    gsm_serial.close()
                    print(f"Closed serial port {GSM_PORT} for GSM module.")
                except Exception as e:
                    print(f"Error closing serial port {GSM_PORT}: {e}")
                gsm_serial = None # Ensure the variable is set to None
            time.sleep(1) # Sleep longer when inactive
            continue

        # If GSM is active, ensure the serial port is open
        if gsm_serial is None or not gsm_serial.is_open:
            try:
                gsm_serial = serial.Serial(GSM_PORT, BAUD_RATE, timeout=1)
                print(f"Successfully opened serial port {GSM_PORT} for GSM module.")
                time.sleep(2) # Wait for the module to initialize
            except serial.SerialException as e:
                print(f"Error opening serial port {GSM_PORT} for GSM module: {e}")
                gsm_serial = None # Set to None to attempt reconnect later
                time.sleep(5) # Wait before retrying
                continue # Continue the outer while loop to check gsm_active again

        # Read data if the port is open and active
        if gsm_serial and gsm_serial.is_open and gsm_serial.in_waiting > 0:
            try:
                message = gsm_serial.readline().decode("utf-8").strip()
                if message:
                    print(f"Received from GSM (COM15): {message}")
                    # --- Extract Payment Amount from the specific SMS format ---
                    # Updated regex to match the new format "You received PHP X.XX via QRPH"
                    match = re.search(r"You received PHP (\d+\.\d{2}) via QRPH", message)
                    if match:
                        try:
                            extracted_amount = float(match.group(1))
                            print(f"Successfully extracted amount from SMS: ₱{extracted_amount}")

                            # Check if the extracted amount matches the expected amount
                            if extracted_amount >= expected_gcash_amount and gcash_print_options:
                                print("GCash payment amount matched. Triggering print.")
                                # Trigger the printing process
                                success = print_document_logic(gcash_print_options)

                                if success:
                                    print("Printing initiated successfully for GCash payment.")
                                    # Emit success event to frontend
                                    socketio.emit("gcash_payment_success", {"success": True, "message": "Payment confirmed and printing started."})
                                else:
                                     print("Failed to initiate printing for GCash payment.")
                                     # Emit failure event to frontend
                                     socketio.emit("gcash_payment_success", {"success": False, "message": "Payment confirmed, but printing failed."})


                                # Deactivate GSM after successful payment and printing attempt
                                gsm_active = False
                                print("GSM detection deactivated after GCash payment.")
                                # Reset stored GCash details
                                expected_gcash_amount = 0.0
                                gcash_print_options = None

                            else:
                                print(f"Received amount ₱{extracted_amount} does not match expected amount ₱{expected_gcash_amount}.")
                                # Optionally emit an event for incorrect amount received
                                socketio.emit("gcash_payment_failed", {"success": False, "message": f"Received incorrect payment amount: ₱{extracted_amount}. Expected: ₱{expected_gcash_amount}"})


                        except ValueError:
                            print("Error: Could not convert extracted amount to float from COM15.")
                            socketio.emit("gcash_payment_failed", {"success": False, "message": "Could not convert amount to number."})
                    else:
                         print("Received SMS does not match expected payment format.")
                         # Optionally emit an event for unexpected SMS format
                         # socketio.emit("gcash_payment_failed", {"success": False, "message": "Received unexpected SMS format."})

            except UnicodeDecodeError:
                print("Error decoding serial data from COM15.")
                socketio.emit("gcash_payment_failed", {"success": False, "message": "Error decoding SMS."})
            except serial.SerialException as e:
                print(f"Serial error on {GSM_PORT}: {e}")
                if gsm_serial and gsm_serial.is_open:
                    gsm_serial.close()
                gsm_serial = None # Indicate need to reconnect
                time.sleep(5) # Wait before attempting reconnect
                socketio.emit("gcash_payment_failed", {"success": False, "message": f"Serial communication error: {e}"})

        time.sleep(0.1) # Short sleep to prevent excessive CPU usage when active


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

# GCASH PAYMENT
@app.route('/gcash-payment')
def gcash_payment():
    """
    Renders the GCash payment page and activates GSM detection.
    """
    global gsm_active
    gsm_active = True # Activate GSM detection when the page is accessed
    print("GSM detection activated for GCash payment page.")
    # Reset stored GCash details when accessing the page
    global expected_gcash_amount, gcash_print_options
    expected_gcash_amount = 0.0
    gcash_print_options = None
    return render_template('gcash-payment.html')

@app.route('/stop_gsm_detection', methods=['POST'])
def stop_gsm_detection():
    """Deactivates GSM detection."""
    global gsm_active
    gsm_active = False
    print("GSM detection deactivated via /stop_gsm_detection route.")
    # Reset stored GCash details on manual stop
    global expected_gcash_amount, gcash_print_options
    expected_gcash_amount = 0.0
    gcash_print_options = None
    return jsonify({"success": True, "message": "GSM detection stopped."})


# START OF PRINT DOCUMENT LOGIC (extracted from route)
def print_document_logic(print_options):
    """
    Contains the core logic for generating a printable PDF and sending it to the printer.
    This function can be called internally or via a route.
    Returns True if printing is initiated, False otherwise.
    """
    global images
    if not images:
        print("Error: No images available for printing.")
        return False

    try:
        # print_options is already a dictionary from the SocketIO event
        # data = print_options # Use print_options directly

        selection = print_options.get("pageSelection", "")
        num_copies = int(print_options.get("numCopies", 1))
        page_size = print_options.get("pageSize", "A4")
        color_option = print_options.get("colorOption", "Color").lower()

        selected_indexes = parse_page_selection(selection, len(images))
        if not selected_indexes:
            print("Error: Invalid page selection for printing.")
            return False

        temp_previews = []

        for idx in selected_indexes:
            img = images[idx].copy()

            orientation = print_options.get("orientationOption") or "auto"
            orientation = determine_orientation(img, orientation)
            canvas_size = calculate_canvas_size(page_size, orientation)
            processed_img = fit_image_to_canvas(img, canvas_size)

            if color_option == 'grayscale':
                processed_img = cv2.cvtColor(processed_img, cv2.COLOR_BGR2GRAY)
                processed_img = cv2.cvtColor(processed_img, cv2.COLOR_GRAY2BGR)

            # Generate temporary images for the PDF creation
            for copy_idx in range(num_copies):
                filename = f"printable_{idx+1}_{page_size}_{color_option}_{copy_idx}.jpg"
                path = os.path.join(app.config['STATIC_FOLDER'], filename) # Use STATIC_FOLDER for temporary files
                cv2.imwrite(path, processed_img)
                temp_previews.append(path)

        pdf_path = os.path.join(app.config['UPLOAD_FOLDER'], "printable_document.pdf") # Save PDF in UPLOAD_FOLDER
        pdf = FPDF()
        pdf.set_auto_page_break(auto=True, margin=15)

        for preview_path in temp_previews:
            if not os.path.exists(preview_path):
                continue
            try:
                # Get image dimensions to determine PDF page size
                img_temp = cv2.imread(preview_path)
                if img_temp is None:
                    print(f"Error: Could not read temporary image file {preview_path}")
                    continue

                height, width, _ = img_temp.shape
                aspect_ratio = width / height

                # FPDF default unit is millimeters. A4 is 210x297 mm.
                # We can set custom page size based on image aspect ratio and selected paper size
                # For simplicity, let's assume we fit the image to the width of a standard PDF page size
                # and adjust height accordingly. Using A4 dimensions as a base.
                pdf_width = 210 # mm
                pdf_height = pdf_width / aspect_ratio

                # If the calculated height is too large for a standard page, adjust
                max_pdf_height = 297 # A4 height in mm
                if pdf_height > max_pdf_height:
                    pdf_height = max_pdf_height
                    pdf_width = pdf_height * aspect_ratio # Adjust width to maintain aspect ratio

                pdf.add_page()
                # Add image to PDF, positioned at top-left with calculated dimensions
                pdf.image(preview_path, x=0, y=0, w=pdf_width, h=pdf_height)
            except Exception as img_e:
                 print(f"Error adding image {preview_path} to PDF: {img_e}")
                 continue # Continue with the next image

        pdf.output(pdf_path)

        # Clean up temporary image files
        for preview_path in temp_previews:
            if os.path.exists(preview_path):
                try:
                    os.remove(preview_path)
                except Exception as clean_e:
                    print(f"Error cleaning up temporary file {preview_path}: {clean_e}")


        if os.name == 'nt':
            import win32print
            import win32api
            printer_name = win32print.GetDefaultPrinter()
            # Use ShellExecute with "print" verb to print the PDF
            # The last parameter (0) means show the window minimized, but for print it often means no window
            print(f"Attempting to print PDF: {pdf_path} to printer: {printer_name}")
            win32api.ShellExecute(0, "print", pdf_path, None, ".", 0)
            print("ShellExecute called.")
        else:
            # For Linux/macOS using lp command
            print(f"Attempting to print PDF: {pdf_path} using lp command.")
            os.system(f'lp "{pdf_path}"')
            print("lp command executed.")


        print(f"Print process initiated for PDF: {pdf_path}")
        return True # Indicate printing was initiated

    except Exception as e:
        print(f"Error in print_document_logic: {e}")
        return False # Indicate printing failed


# END OF PRINT DOCUMENT LOGIC


# The print_document route now calls the print_document_logic function
@app.route('/print_document', methods=['POST'])
def print_document_route():
    """Handles print requests received via HTTP POST."""
    data = request.json
    if not data:
        return jsonify({"error": "Invalid JSON payload."}), 400

    print("Received print request via /print_document route.")
    success = print_document_logic(data)

    if success:
        return jsonify({"success": True, "message": "Document sent to the printer successfully!"}), 200
    else:
        return jsonify({"error": "Failed to print document."}), 500


# Update result route to display both previews and segmentation
@app.route('/result', methods=['GET'])
def result():
    global images, selected_previews
    if not selected_previews or not images:
        return redirect('/file-upload')
    return render_template('result.html', previews=selected_previews)



@app.route('/uploads/<filename>')
def uploaded_file(filename):
    """Serves uploaded files from the static folder."""
    return send_from_directory(app.config['STATIC_FOLDER'], filename)



def check_printer_status(printer_name):
    """
    Checks the status of the specified printer.
    """
    try:
        hPrinter = win32print.OpenPrinter(printer_name)  # Open a handle to the printer
        jobs = win32print.EnumJobs(hPrinter, 0, -1, 2)  # Get information about the print jobs
        win32print.ClosePrinter(hPrinter)  # Close the printer handle

        if jobs:  # Printer is busy (printing)
            return "Printing"
        else:  # Printer is idle
            socketio.emit("printer_status_idle")  # Emit idle status to the frontend
            return "Idle"

    except Exception as e:
        return f"Error: {e}"




def monitor_printer_status(printer_name, upload_folder):
    """
    Continuously checks the printer status and resets the coin count
    when the printer becomes Printing, and deletes files from the upload folder
    when the printer becomes idle after printing. Runs as a background thread.
    """
    global coin_value_sum, coin_detection_active # Added coin_detection_active here
    last_status = "Idle"

    while True:
        current_status = check_printer_status(printer_name)

        if current_status == "Printing" and last_status == "Idle":
            print(f"Printer Status: {current_status}")
            # Reset coin count when printing starts
            coin_value_sum = 0
            print(f"Coin count reset to: {coin_value_sum}")
            # Emit update to all connected clients
            socketio.emit('update_coin_count', {'count': coin_value_sum})
            # Stop coin detection when printing starts
            coin_detection_active = False
            print("Coin detection deactivated due to printer status: Printing.")


        elif current_status == "Idle" and last_status == "Printing":
            print(f"Printer Status: {current_status}")
            # File deletion after printing
            for filename in os.listdir(upload_folder):
                file_path = os.path.join(upload_folder, filename)
                try:
                    os.remove(file_path)
                    print(f"Deleted file: {file_path}")
                except FileNotFoundError:
                    pass

        last_status = current_status
        time.sleep(1) # Check status every second


@app.route('/payment-success')
def payment_success():
    """Renders the payment success page."""
    # Consider stopping both detections here if the user goes directly to payment-success
    global gsm_active, coin_detection_active
    gsm_active = False
    coin_detection_active = False
    print("GSM and Coin detection deactivated on payment success.")
    # Reset stored GCash details on payment success
    global expected_gcash_amount, gcash_print_options
    expected_gcash_amount = 0.0
    gcash_print_options = None
    return render_template('payment-success.html')


@app.route('/payment-type')
def payment_type():
    """Renders the payment type selection page."""
    # Consider stopping both detections here if the user navigates back
    global gsm_active, coin_detection_active
    gsm_active = False
    coin_detection_active = False
    print("GSM and Coin detection deactivated on payment type selection.")
    # Reset stored GCash details on navigating back
    global expected_gcash_amount, gcash_print_options
    expected_gcash_amount = 0.0
    gcash_print_options = None
    return render_template('payment-type.html')


if __name__ == "__main__":
    # Get the default printer name
    printer_name = win32print.GetDefaultPrinter()
    activity_log = []

    # Path to the uploads folder (replace with your actual path)
    upload_folder = "uploads"

    # Start the Arduino coin detection thread (COM6)
    # This thread runs continuously but only processes data when coin_detection_active is True
    socketio.start_background_task(read_coin_slot_data)

    # Start the GSM module thread (COM15)
    # This thread runs continuously but only processes data when gsm_active is True
    socketio.start_background_task(read_gsm_data)

    # Start the printer status monitoring thread in the same process
    socketio.start_background_task(monitor_printer_status, printer_name, upload_folder)

    # Start the Flask app
    socketio.run(app, debug=False)

