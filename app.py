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
# import multiprocessing # Removed multiprocessing as we'll use threading for printer status
import pywintypes
import wmi
import multiprocessing
import json # Import json for parsing printOptions
import re # Import regex for more robust parsing
import math

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
images = []  # Store images globally (consider refactoring for very large files)
total_pages = 0  # Keep track of total pages.  May not be needed globally.
# selected_previews = []  # Store paths to selected preview images. Removed global variable, generate previews on demand.
arduino = None # This might still be used in the arduino_payment_page route, will keep for now.
coin_count = 0
coin_value_sum = 0  # Track the total value of inserted coins
coin_detection_active = False # Flag to control coin detection for
coin_detection_active = False # Flag to control coin detection for COM3
gsm_active = False # Flag to control GSM detection for COM15

# Define COM ports and baud rate for both Arduinos
COIN_SLOT_PORT = 'COM3'
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

@app.route("/set_price", methods=["POST"])
def set_price():
    global coin_slot_serial
    data = request.get_json() or {}
    price = int(data.get("price", 0))

    if not coin_slot_serial or not coin_slot_serial.is_open:
        try:
            coin_slot_serial = serial.Serial(COIN_SLOT_PORT, BAUD_RATE, timeout=1)
            # give Arduino a moment to reset
            time.sleep(2)
        except serial.SerialException as e:
             return jsonify(success=False, message=f"Failed to open serial port {COIN_SLOT_PORT}: {str(e)}"), 500


    try:
        coin_slot_serial.write(f"{price}\n".encode())
        return jsonify(success=True)
    except Exception as e:
        return jsonify(success=False, message=str(e)), 500

def serial_listener():
    global coin_slot_serial
    while True:
        # Check if serial port is open before trying to read
        if coin_slot_serial and coin_slot_serial.is_open and coin_slot_serial.in_waiting:
            try:
                line = coin_slot_serial.readline().decode().strip()
                # Arduino will send lines like:
                #   COIN:<total>
                #   PRINTING
                #   CHANGE:<n>
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
                        socketio.emit("change_dispensed", {"amount": amt})
                    except ValueError:
                         print(f"Could not parse change amount from serial: {line}")
            except serial.SerialException as e:
                print(f"Serial error during read on {COIN_SLOT_PORT}: {e}")
                # Attempt to close and reopen the port on error
                if coin_slot_serial and coin_slot_serial.is_open:
                    try:
                        coin_slot_serial.close()
                        print(f"Closed serial port {COIN_SLOT_PORT} due to error.")
                    except Exception as close_e:
                         print(f"Error closing serial port {COIN_SLOT_PORT}: {close_e}")
                coin_slot_serial = None # Set to None to trigger reconnect logic

        # Reconnect logic if serial port is not open
        elif coin_slot_serial is None or not coin_slot_serial.is_open:
             try:
                 # Attempt to open the serial port
                 coin_slot_serial = serial.Serial(COIN_SLOT_PORT, BAUD_RATE, timeout=1)
                 print(f"Successfully re-opened serial port {COIN_SLOT_PORT}.")
                 time.sleep(2) # Wait for Arduino
             except serial.SerialException as e:
                 # If opening fails, print error and wait before retrying
                 print(f"Failed to re-open serial port {COIN_SLOT_PORT}: {e}")
                 coin_slot_serial = None # Ensure it's None if opening failed
                 time.sleep(5) # Wait longer before next retry

        time.sleep(0.05) # Short sleep to prevent excessive CPU usage


# Helper Functions
def allowed_file(filename):
    """Checks if the given filename has an allowed extension."""
    return (
        "." in filename
        and filename.rsplit(".", 1)[1].lower() in app.config["ALLOWED_EXTENSIONS"]
    )


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
    """Converts a Word document (doc or docx) to a list of OpenCV images."""
    # Optimization Note: Using win32com is platform-dependent and requires Microsoft Word.
    # For a production system, consider a cross-platform library that doesn't rely on COM.
    if not file_path.lower().endswith((".doc", ".docx")):
        raise ValueError("Unsupported file type for Word documents.")

    pdf_path = None # Initialize pdf_path to None
    try:
        pythoncom.CoInitialize()
        word = client.Dispatch("Word.Application")
        word.visible = False
        doc = word.Documents.Open(file_path)
        # Create a temporary PDF path in the UPLOAD_FOLDER
        pdf_path = os.path.join(app.config['UPLOAD_FOLDER'], os.path.basename(file_path).replace(".docx", ".pdf").replace(".doc", ".pdf"))
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
        # Clean up temp PDF if it was created
        if pdf_path and os.path.exists(pdf_path):
            try:
                os.remove(pdf_path)
            except Exception as cleanup_e:
                print(f"Error cleaning up temporary PDF {pdf_path}: {cleanup_e}")


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
            try:
                start, end = map(int, part.split("-"))
                # Ensure start and end are within valid page range (1-based)
                start = max(1, start)
                end = min(total_pages, end)
                pages.update(range(start - 1, end))  # 0-based indexing
            except ValueError:
                 print(f"Warning: Invalid range format in page selection: {part}")
                 continue # Skip this part and continue parsing
        elif part.isdigit():
            page_num = int(part)
            if 1 <= page_num <= total_pages:
                pages.add(page_num - 1)  # 0-based indexing
            else:
                 print(f"Warning: Page number out of range in selection: {page_num}")
        else:
            print(f"Warning: Invalid format in page selection: {part}")

    return sorted(p for p in pages if 0 <= p < total_pages) # Final check to ensure indices are valid


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
        # Iterate through printers with the specified name
        for printer in c.Win32_Printer(Name=printer_name):
            # Get printer status message from the map, default to "Unknown" if not found
            status_msg = status_map.get(printer.PrinterStatus, "Unknown")
            # Get error state message from the map, default to None if no specific error
            error_msg = error_state_map.get(printer.DetectedErrorState, None)

            messages = []

            # Add error message if present and not "Unknown"
            if error_msg and error_msg != "Unknown":
                messages.append(error_msg)
            # Add status message if present and not "Idle" or "Unknown"
            elif status_msg and status_msg not in ["Idle", "Unknown"]:
                messages.append(status_msg)

            # Log new messages that haven't been logged before
            for msg in messages:
                if msg not in logged_errors:
                    log = {
                        "date": now.strftime("%m/%d/%Y"),
                        "time": now.strftime("%I:%M %p"),
                        "event": msg
                    }
                    activity_log.append(log) # Add to global activity log
                    logs.append(log) # Add to logs for this check
                    logged_errors.add(msg) # Add to set of logged errors

    except Exception as e:
        # Handle potential WMI query errors
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

    return logs # Return the list of new logs generated during this check


# Routes - Admin
@app.route("/")
def admin_user():
    return render_template("admin-user.html")


@app.route("/admin-dashboard")
def admin_dashboard():
    # Example data, replace with actual data retrieval logic
    data = {
        "total_sales": 5000, # Example total sales
        "printed_pages": printed_pages_today, # Global counter for pages printed today
        "files_uploaded": files_uploaded_today, # Global counter for files uploaded today
        "current_balance": 3000, # Example current balance
        "sales_history": [ # Example sales history data
            {"method": "GCash", "amount": 500, "date": "2024-03-30", "time": "14:00"},
            {"method": "PayMaya", "amount": 250, "date": "2024-03-29", "time": "11:30"},
        ],
        "sales_chart": [500, 600, 700, 800],  # Example sales data for Chart.js
    }
    return render_template("admin-dashboard.html", data=data)


@app.route("/admin-files-upload")
def admin_printed_pages():
    # Pass the global list of uploaded files info to the template
    return render_template("admin-files-upload.html", uploaded=uploaded_files_info)


@app.route("/admin-activity-log")
def admin_activity_log():
    # Pass the global activity log to the template
    return render_template('admin-activity-log.html', printed=activity_log)

@app.route('/api/printer-status')
def printer_status_api():
    global printer_name  # Declare printer_name as global here
    if printer_name is None:  # Initialize printer_name if not already initialized
        try:
            printer_name = win32print.GetDefaultPrinter()
        except Exception as e:
            return jsonify({"error": f"Could not get default printer: {e}"}), 500

    # Return the logs from the WMI status check
    return jsonify(get_printer_status_wmi())


@app.route("/admin-balance")
def admin_balance():
    # This route seems to just render a static template
    return render_template("admin-balance.html")


@app.route("/admin-feedbacks")
def admin_feedbacks():
    # Example feedback data, replace with actual data retrieval
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
    # This route seems to just render a static template
    return render_template("file-upload.html")


@app.route("/index")
def index():
    # This route seems to just render a static template
    return render_template("index.html")

@app.route("/manual-upload")
def manual_upload():
    # List files in the upload folder and pass to the template
    files = os.listdir(UPLOAD_FOLDER)
    return render_template("manual-display-upload.html", files=files)


@app.route("/online-upload", methods=["GET", "POST"])
def online_upload():
    if request.method == "POST":
        file = request.files.get("file")
        if file:
            filename = file.filename
            # Save the uploaded file to the UPLOAD_FOLDER
            file.save(os.path.join(UPLOAD_FOLDER, filename))
            return f'File "{filename}" uploaded successfully!'
        else:
            return "No file selected!", 400
    else:
        # List files in the upload folder and provide a Toffee Share link
        files = os.listdir(UPLOAD_FOLDER)
        toffee_share_link = "https://toffeeshare.com/nearby"
        return render_template(
            "upload_page.html", files=files, toffee_share_link=toffee_share_link
        )



@app.route("/payment")
def payment_page():
    """Renders the payment page and activates coin detection."""
    global coin_detection_active
    coin_detection_active = True # Activate coin detection when the payment page is accessed
    print("Coin detection activated for payment.")
    # The background task for coin detection is started in __main__ and will now become active
    return render_template("payment.html")


@app.route("/stop_coin_detection", methods=["POST"])
def stop_coin_detection():
    """Deactivates coin detection."""
    global coin_detection_active
    coin_detection_active = False # Deactivate coin detection
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
        uploaded_files_info = [] # Clear uploaded files info for the new day

    if "file" not in request.files:
        return jsonify({"error": "No file part"}), 400

    file = request.files["file"]
    if file.filename == "":
        return jsonify({"error": "No selected file"}), 400

    if file and allowed_file(file.filename):
        filename = file.filename
        file_ext = filename.split(".")[-1].lower()
        # Create a safe and unique filename to prevent overwriting
        # Using a timestamp and original filename
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
        safe_filename = f"{timestamp}_{filename}"
        filepath = os.path.join(UPLOAD_FOLDER, safe_filename)
        file.save(filepath)

        # Clear previous images and total_pages before processing a new file
        images = []
        total_pages = 0

        if file_ext == "pdf":
            images = pdf_to_images(filepath)
            total_pages = len(images)

        elif file_ext in ("jpg", "jpeg", "png"):
            # Read image directly using cv2
            img = cv2.imread(filepath)
            if img is not None:
                images = [img]
                total_pages = 1
            else:
                 print(f"Error reading image file: {filepath}")
                 os.remove(filepath) # Clean up the uploaded file
                 return jsonify({"error": "Failed to read image file"}), 500


        elif file_ext in ("docx", "doc"):
            images = docx_to_images(filepath)
            total_pages = len(images)

        else:
            os.remove(filepath)  # Clean up unsupported file
            return jsonify({"error": "Unsupported file format"}), 400

        # Check if any images were generated
        if not images:
             os.remove(filepath) # Clean up the uploaded file if processing failed
             return jsonify({"error": "Failed to process file into images"}), 500


        # Update daily stats
        # Note: printed_pages_today is a bit misleading here, it counts total pages uploaded, not printed.
        # Consider renaming this variable or tracking printed pages separately.
        # For now, keeping the original logic.
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
    Continuously reads data from the Arduino serial port (COM3) for coin detection,
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
                    print(f"Received from Arduino (COM3): {message}")
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
                            print(f"Error: Could not convert extracted coin value to integer from COM3 message: {message}.")
                    elif message.startswith("Unknown coin"):
                        print(f"Warning from COM3: {message}")
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
                print("Error decoding serial data from COM3.")
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


# The read_gsm_data function remains largely the same, its performance depends on the GSM module and network.
# Error handling and reconnect logic are already present.
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
                message = gsm_serial.readline().decode("utf-8", errors='ignore').strip() # Use errors='ignore' for robustness
                if message:
                    print(f"Received from GSM (COM15): {message}")
                    # --- Extract Payment Amount from the specific SMS format ---
                    # Updated regex to match the new format "You received PHP X.XX via QRPH"
                    match = re.search(r"You received PHP (\d+\.\d{2}) via QRPH", message)
                    if match:
                        try:
                            extracted_amount = float(match.group(1))
                            print(f"Successfully extracted amount from SMS: ₱{extracted_amount}")

                            # Check if the extracted amount matches or exceeds the expected amount
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
                         # Handle other SMS messages if necessary, or ignore them
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
            except Exception as e:
                 print(f"An unexpected error occurred in read_gsm_data: {e}")
                 socketio.emit("gcash_payment_failed", {"success": False, "message": f"An internal error occurred: {e}"})


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
    Print the original uploaded file directly without creating a PDF from images.
    Returns True if printing initiated, False otherwise.
    """
    try:
        # The original filename must be passed in print_options under "fileName"
        filename = print_options.get("fileName")
        if not filename:
            print("Error: No filename provided in print options.")
            return False

        # Construct full path to the uploaded file
        file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        if not os.path.exists(file_path):
            print(f"Error: File not found: {file_path}")
            return False

        if os.name == 'nt':  # Windows
            import win32print
            import win32api
            printer_name = win32print.GetDefaultPrinter()
            print(f"Sending original file to printer: {file_path} on printer: {printer_name}")
            win32api.ShellExecute(0, "print", file_path, None, ".", 0)
            print("Print command sent via ShellExecute.")
        else:
            # For Linux/macOS: use lp command
            print(f"Sending original file to printer using lp: {file_path}")
            os.system(f'lp "{file_path}"')
            print("lp command executed.")

        print(f"Print process initiated for original file: {file_path}")
        return True

    except Exception as e:
        print(f"Error in print_document_logic: {e}")
        return False

# END OF PRINT DOCUMENT LOGIC



# The print_document route now calls the print_document_logic function
@app.route('/print_document', methods=['POST'])
def print_document_route():
    """Handles print requests received via HTTP POST."""
    data = request.json
    if not data:
        return jsonify({"error": "Invalid JSON payload."}), 400

    print("Received print request via /print_document route.")
    success = print_document_logic(data) # Call the core printing logic

    if success:
        return jsonify({"success": True, "message": "Document sent to the printer successfully!"}), 200
    else:
        return jsonify({"error": "Failed to print document."}), 500


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
    """
    Continuously checks the printer status and resets the coin count
    when the printer becomes Printing, and deletes files from the upload folder
    when the printer becomes idle after printing. Runs as a background thread.
    Also emits printer status updates via SocketIO.
    """
    global coin_value_sum, coin_detection_active
    last_status = "Unknown" # Initialize last_status to something other than Idle or Printing

    while True:
        current_status = check_printer_status(printer_name)

        # Emit the current status to the frontend
        if current_status != last_status:
             print(f"Printer status changed to: {current_status}")
             socketio.emit("printer_status_update", {"status": current_status})


        # Logic for resetting coin count and stopping detection when printing starts
        if current_status == "Printing" and last_status != "Printing":
            print(f"Printer Status: {current_status}. Resetting coin count and stopping detection.")
            # Reset coin count when printing starts
            coin_value_sum = 0
            print(f"Coin count reset to: {coin_value_sum}")
            # Emit update to all connected clients
            socketio.emit('update_coin_count', {'count': coin_value_sum})
            # Stop coin detection when printing starts
            coin_detection_active = False
            print("Coin detection deactivated due to printer status: Printing.")


        # Logic for file deletion after printing is complete
        # Check if the status transitioned from Printing to Idle
        elif current_status == "Idle" and last_status == "Printing":
            print(f"Printer Status: {current_status}. Printing finished. Deleting files.")
            # File deletion from the upload folder
            # Be careful with file deletion; ensure only temporary or finished print files are removed.
            # The current logic deletes ALL files in the UPLOAD_FOLDER.
            # Consider a more selective approach if the UPLOAD_FOLDER contains files
            # that should persist.
            for filename in os.listdir(upload_folder):
                file_path = os.path.join(upload_folder, filename)
                try:
                    # Add a check to ensure it's a file and not a directory
                    if os.path.isfile(file_path):
                        os.remove(file_path)
                        print(f"Deleted file: {file_path}")
                except FileNotFoundError:
                    # File might have been deleted by another process, ignore
                    pass
                except Exception as e:
                    # Log any other errors during file deletion
                    print(f"Error deleting file {file_path}: {e}")

        last_status = current_status # Update last_status for the next iteration
        time.sleep(2) # Check status less frequently (every 2 seconds) to reduce overhead


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
    try:
        printer_name = win32print.GetDefaultPrinter()
        print(f"Default printer set to: {printer_name}")
    except Exception as e:
        print(f"Error getting default printer: {e}. Printer status monitoring may not work.")
        printer_name = None # Ensure printer_name is None if getting it fails

    activity_log = []

    # Path to the uploads folder
    upload_folder = app.config['UPLOAD_FOLDER'] # Use the configured UPLOAD_FOLDER

    # Start the Arduino coin detection thread (COM3)
    # This thread runs continuously but only processes data when coin_detection_active is True
    socketio.start_background_task(read_coin_slot_data)

    # Start the GSM module thread (COM15)
    # This thread runs continuously but only processes data when gsm_active is True
    socketio.start_background_task(read_gsm_data)

    # Start the printer status monitoring thread in the same process
    # Only start if a printer name was successfully obtained
    if printer_name:
        socketio.start_background_task(monitor_printer_status, printer_name, upload_folder)
    else:
        print("Printer name not available. Printer status monitoring disabled.")


    # Start the Flask app using socketio.run
    # debug=False is recommended for production
    socketio.run(app, debug=False, allow_unsafe_werkzeug=True) # allow_unsafe_werkzeug=True might be needed in some environments

