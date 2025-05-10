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
import multiprocessing

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*")

# Configuration
UPLOAD_FOLDER = os.path.join(os.getcwd(), "uploads")
STATIC_FOLDER = os.path.join(os.getcwd(), "static", "uploads")
ALLOWED_EXTENSIONS = {"pdf", "jpg", "jpeg", "png", "docx", "doc"}

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
arduino = None
coin_count = 0
coin_value_sum = 0  # Track the total value of inserted coins
coin_detection_active = False # Flag to control coin detection


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
    printed = [
        {
            "date": "9/18/2024",
            "time": "11:52 PM",
            "event": "Paper Jam – The paper is stuck in the printer, causing a blockage.",
        },
        {
            "date": "9/18/2024",
            "time": "11:52 PM",
            "event": "Low Ink/Toner – The printer's ink or toner is running low and needs to be replaced.",
        },
        {
            "date": "9/18/2024",
            "time": "11:52 PM",
            "event": "Printer Offline – The printer is not connected to the network or is turned off.",
        },
        {
            "date": "9/18/2024",
            "time": "11:52 PM",
            "event": "Out of Paper – The printer has run out of paper in the tray.",
        },
        {
            "date": "9/18/2024",
            "time": "11:52 PM",
            "event": "Printer Error – A general error, often requiring troubleshooting or a reset.",
        },
    ]
    return render_template("admin-activity-log.html", printed=printed)


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
    global coin_detection_active
    coin_detection_active = True
    # Start the arduino thread if it's not running.
    global arduino_thread
    if 'arduino_thread' not in globals() or not arduino_thread.is_alive():
        arduino_thread = threading.Thread(target=read_serial_data_arduino, daemon=True)
        arduino_thread.start()
        print("Arduino coin detection thread started.")
    else:
        print("Arduino coin detection thread is already running.")
    return render_template("payment.html")


@app.route("/stop_coin_detection", methods=["POST"])
def stop_coin_detection():
    global coin_detection_active
    coin_detection_active = False
    print("Coin detection stopped.")
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
    """Generates preview images based on user selections."""
    global images
    data = request.json;
    page_selection = data.get('pageSelection', '');
    num_copies = int(data.get('numCopies', 1));
    page_size = data.get('pageSize', 'A4');
    color_option = data.get('colorOption', 'Color');
    orientation = data.get('orientationOption', 'portrait');

    try:
        selected_indexes = parse_page_selection(page_selection, len(images));
        previews = []

        for idx in selected_indexes:
            if idx >= len(images):
                continue
            processed_img = images[idx].copy()
            aspect_ratio = processed_img.shape[1] / processed_img.shape[0]

            if orientation == 'landscape':
                new_width = 1200
                new_height = int(new_width / aspect_ratio)
                processed_img = cv2.resize(processed_img, (new_width, new_height))
            else:
                new_height = 1200
                new_width = int(new_height * aspect_ratio)
                processed_img = cv2.resize(processed_img, (new_width, new_height))

            if color_option == 'Grayscale':
                processed_img = cv2.cvtColor(processed_img, cv2.COLOR_BGR2GRAY)
                processed_img = cv2.cvtColor(processed_img, cv2.COLOR_GRAY2BGR)

            for copy_idx in range(num_copies):
                filename = f"preview_{idx + 1}_{page_size}_{color_option}_{orientation}_{copy_idx}.jpg"
                preview_path = os.path.join(app.config['STATIC_FOLDER'], filename)
                cv2.imwrite(preview_path, processed_img)
                previews.append(f"/uploads/{filename}")
        return jsonify({"previews": previews}), 200
    except Exception as e:
        return jsonify({"error": f"Failed to generate previews: {e}"}), 500



@app.route('/preview_with_price', methods=['POST'])
def preview_with_price():
    """Generates previews with pricing information for selected pages."""
    global images
    if not images:
        return jsonify({"error": "No images available. Please upload a file first."}), 400

    data = request.json
    selection = data.get("pageSelection", "")
    num_copies = int(data.get("numCopies", 1))
    page_size = data.get("pageSize", "A4")
    color_option = data.get("colorOption", "Color")

    selected_indexes = parse_page_selection(selection, len(images))
    if not selected_indexes:
        return jsonify({"error": "No valid pages selected."}), 400

    previews = []
    page_prices = []
    total_price = 0

    for idx in selected_indexes:
        img = images[idx]
        processed_img, page_price = process_image(img, page_size=page_size, color_option=color_option)
        page_price *= num_copies
        total_price += page_price

        preview_filename = f"preview_{idx+1}.jpg"
        preview_path = os.path.join(app.config['STATIC_FOLDER'], preview_filename)
        cv2.imwrite(preview_path, img)

        if color_option != "Grayscale":
            processed_filename = f"segmented_{idx+1}.jpg"
            processed_path = os.path.join(app.config['STATIC_FOLDER'], processed_filename)
            cv2.imwrite(processed_path, processed_img)
        else:
            processed_filename = f"grayscale_preview_{idx+1}.jpg"
            processed_path = os.path.join(app.config['STATIC_FOLDER'], processed_filename)
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            cv2.imwrite(processed_path, gray)


        page_prices.append({
            "page": idx + 1,
            "price": page_price,
            "original": f"/uploads/{preview_filename}",
            "processed": f"/uploads/{processed_filename}"
        })

    return jsonify({
        "totalPrice": round(total_price, 2),
        "pagePrices": page_prices,
        "previews": [{"page": p["page"], "path": p["original"]} for p in page_prices]
    })



# START OF ARDUINO AND COIN SLOT CONNECTION CODE
@app.route('/arduino-payment')
def arduino_payment_page():
    """Renders the payment page and initializes the Arduino connection."""
    global arduino
    if not arduino:
        try:
            arduino = serial.Serial('COM6', 9600, timeout=1)  # Use a constant for COM port
            time.sleep(2)
            print("Arduino successfully initialized.")
        except Exception as e:
            print(f"Failed to initialize Arduino: {e}")
            return "Error: Could not connect to Arduino."  # Consistent error response
    return render_template('payment.html')



def read_serial_data_arduino():
    """
    Continuously reads data from the Arduino serial port, parses coin values,
    and updates the total coin count.  Handles errors robustly.
    """
    global arduino, coin_value_sum
    serial_port = 'COM6'  # Consider making this a constant
    baud_rate = 9600
    local_arduino = None #create a local arduino variable

    try:
        local_arduino = serial.Serial(serial_port, baud_rate, timeout=1)
        print(f"Successfully opened serial port {serial_port} for Arduino coin detection.")
        while True:
            if not coin_detection_active:
                time.sleep(0.5) # Avoid busy waiting
                continue
            if local_arduino.in_waiting > 0:
                try:
                    message = local_arduino.readline().decode('utf-8').strip()
                    print(f"Received from Arduino: {message}")
                    if message.startswith("Detected coin worth ₱"):
                        try:
                            value = int(message.split("₱")[1])
                            coin_value_sum += value
                            print(f"Coin inserted: ₱{value}, Total: ₱{coin_value_sum}")
                            print(
                                f"Emitting WebSocket event: update_coin_count - {coin_value_sum}"
                            )
                            socketio.emit('update_coin_count', {'count': coin_value_sum})
                        except ValueError:
                            print("Error: Could not parse coin value.")
                    elif message.startswith("Unknown coin"):
                        print(f"Warning: {message}")
                except UnicodeDecodeError:
                    print("Error decoding serial data from Arduino.")
            time.sleep(0.1)
    except serial.SerialException as e:
        print(f"Error opening serial port {serial_port} for Arduino: {e}")
    finally:
        if local_arduino and local_arduino.is_open:
            local_arduino.close()
            print(f"Closed serial port {serial_port} for Arduino.")



@app.route('/detect_coins')
def start_coin_detection():
    # This route is no longer needed as the coin detection runs in a background thread.
    return "Coin detection service is running in the background."


@app.route('/coin_count')
def get_coin_count():
    """Returns the current total coin value as a JSON response."""
    global coin_value_sum
    return jsonify({'count': coin_value_sum})
# END OF ARDUINO AND COIN SLOT CONNECTION CODE

# GCASH PAYMENT
@app.route('/gcash-payment')
def gcash_payment():
    return render_template('gcash-payment.html')

# START OF PRINT DOCUMENT
@app.route('/print_document', methods=['POST'])
def print_document():
    """
    Handles printing a document based on user-specifiedparameters.  Generates a PDF
    from the selected images and sends it to the default printer.
    """
    global images
    if not images:
        return jsonify({"error": "No file uploaded or processed yet."}), 400

    try:
        data = request.json
        selection = data.get("pageSelection", "")
        num_copies = int(data.get("numCopies", 1))
        page_size = data.get("pageSize", "A4")
        color_option = data.get("colorOption", "Color")

        selected_indexes = parse_page_selection(selection, len(images))
        if not selected_indexes:
            return jsonify({"error": "Invalid page selection."}), 400

        temp_previews = []  # Store paths to temporary preview images

        for idx in selected_indexes:
            img = images[idx].copy()  # Create a copy to avoid modifying the original

            # Resize the image based on the selected page size
            if page_size == 'Short':
                img = cv2.resize(img, (800, 1000))
            elif page_size == 'Long':
                img = cv2.resize(img, (800, 1200))
            elif page_size == 'A4':
                img = cv2.resize(img, (800, 1100))

            # Convert to grayscale if specified
            if color_option == 'Grayscale':
                img = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
                img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)  # Convert back to BGR for saving

            # Generate a preview image for each copy
            for copy_idx in range(num_copies):
                filename = f"print_preview_{idx + 1}_{page_size}_{color_option}_{copy_idx}.jpg"
                path = os.path.join(app.config['STATIC_FOLDER'], filename)
                cv2.imwrite(path, img)  # Save the preview image
                temp_previews.append(path)  # Store the path for PDF generation

        # Create a PDF from the generated preview images
        pdf_path = os.path.join(app.config['UPLOAD_FOLDER'], "printable_preview.pdf")
        pdf = FPDF()
        pdf.set_auto_page_break(auto=True, margin=15)  # Enable auto page breaks

        for preview_path in temp_previews:
            if not os.path.exists(preview_path):
                continue
            img = cv2.imread(preview_path)
            height, width, _ = img.shape
            aspect_ratio = width / height
            page_width = 210
            page_height = page_width / aspect_ratio
            pdf.add_page()
            pdf.image(preview_path, x=10, y=10, w=page_width, h=page_height)

        pdf.output(pdf_path)  # Save the generated PDF

        # Print the PDF using the appropriate OS command
        if os.name == 'nt':  # Windows
            import win32print
            import win32api

            printer_name = win32print.GetDefaultPrinter()  # Get the default printer name
            win32api.ShellExecute(0, "print", pdf_path, None, ".", 0)  # Print the PDF
        else:  # Assume Unix-like (Linux, macOS)
            os.system(f'lp "{pdf_path}"')  # Use the lp command to print

        return (
            jsonify({"success": True, "message": "Document sent to the printer successfully!"}),
            200,
        )

    except Exception as e:
        print(f"Error printing document: {e}")
        return jsonify({"error": f"Failed to print document: {e}"}), 500



# Update result route to display both previews and segmentation
@app.route('/result', methods=['GET'])
def result():
    """Displays the result page, showing previews."""
    global selected_previews
    return render_template('result.html', previews=selected_previews, price_info=None)  # Pass data to template



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




def check_printer_status_loop(printer_name, upload_folder):
    """
    Continuously checks the printer status and deletes files from the upload folder
    when the printer becomes idle after printing.

    Args:
        printer_name (str): The name of the printer to monitor.
        upload_folder (str): The path to the folder containing files to delete.
    """
    last_status = "Idle"  # Initialize last_status to "Idle"

    while True:
        current_status = check_printer_status(
            printer_name
        )  # Get the current printer status

        if (
            current_status == "Printing" and last_status == "Idle"
        ):  # Transition from Idle to Printing
            print(f"Printer Status: {current_status}")  # Log the status change

        elif (
            current_status == "Idle" and last_status == "Printing"
        ):  # Transition from Printing to Idle
            print(f"Printer Status: {current_status}")  # Log the status change
            # File deletion after printing
            for filename in os.listdir(
                upload_folder
            ):  # Iterate through files in the upload folder
                file_path = os.path.join(
                    upload_folder, filename
                )  # Get the full file path
                try:
                    os.remove(
                        file_path
                    )  # Attempt to delete the file.  This is the core functionality.
                    print(f"Deleted file: {file_path}")  # Log the deletion
                except FileNotFoundError:
                    pass  # File might have already been deleted by another process

        last_status = (
            current_status
        )  # Update last_status for the next iteration.  Crucial for detecting transitions.

        time.sleep(
            1
        )  # Check the printer status every second.  Adjust as needed for performance.



def read_serial_data():
    """
    Continuously reads data from the serial port, extracts payment amounts from
    received messages, and emits the amounts via a SocketIO event.
    """
    serial_port = 'COM6'  # Replace with your Arduino's serial port.  Use a constant.
    baud_rate = 9600  # Define baud rate as a constant
    ser = None

    try:
        ser = serial.Serial(
            serial_port, baud_rate, timeout=1
        )  # Open the serial port.
        print(f"Successfully opened serial port {serial_port}")
        while True:
            if (
                ser.in_waiting > 0
            ):  # Check if there is data waiting to be read.  Non-blocking.
                try:
                    message = (
                        ser.readline().decode("utf-8").strip()
                    )  # Read and decode the data
                    if message:
                        print(f"Received from Arduino: {message}")
                        # --- Extract Payment Amount from the specific SMS format ---
                        if (
                            "You have received PHP" in message
                            and "via QRPH" in message
                        ):
                            try:
                                start_index = (
                                    message.find("PHP") + 4
                                )  # Find the start of the amount
                                end_index = message.find(
                                    " via QRPH"
                                )  # Find the end of the amount
                                if (
                                    start_index != -1
                                    and end_index != -1
                                    and end_index > start_index
                                ):
                                    amount_str = message[
                                        start_index:end_index
                                    ].strip()  # Extract the amount string
                                    extracted_amount = float(
                                        amount_str
                                    )  # Convert to float
                                    print(f"Extracted amount: {extracted_amount}")
                                    socketio.emit(
                                        "payment_received",
                                        {"amount": extracted_amount},
                                    )  # Emit the amount
                                else:
                                    print("Could not find amount delimiters in SMS.")
                            except ValueError:
                                print("Could not convert extracted amount to float.")
                except UnicodeDecodeError:
                    print("Error decoding serial data.")
            time.sleep(
                0.1
            )  # Short sleep to prevent excessive CPU usage.  Adjust as needed.
    except serial.SerialException as e:
        print(f"Error opening serial port {serial_port}: {e}")
    finally:
        if (
            ser and ser.is_open
        ):  # Check if the serial port is open before closing.  Important.
            ser.close()  # Close the serial port to release the resource.
            print(f"Closed serial port {serial_port}")



@app.route('/payment-success')
def payment_success():
    """Renders the payment success page."""
    return render_template('payment-success.html')



@app.route('/payment-type')
def payment_type():
    """Renders the payment type selection page."""
    return render_template('payment-type.html')



if __name__ == "__main__":
    # Get the default printer name
    printer_name = win32print.GetDefaultPrinter()

    # Path to the uploads folder (replace with your actual path)
    upload_folder = "uploads"

    socketio.start_background_task(read_serial_data_arduino)


    # Start the printer status monitoring in a separate process
    printer_process = multiprocessing.Process(
        target=check_printer_status_loop, args=(printer_name, upload_folder)
    )
    printer_process.start()

    # Start the Flask app
    socketio.run(app, debug=False)