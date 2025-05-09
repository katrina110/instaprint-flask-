import os
from flask import Flask, request, render_template, jsonify, send_from_directory, redirect, url_for
import cv2
import numpy as np
import fitz  # PyMuPDF
from docx import Document
from io import BytesIO
import pythoncom
from win32com import client
from fpdf import FPDF

import csv
import requests
from io import StringIO

import serial
import time
from datetime import datetime
from PyPDF2 import PdfReader
from docx import Document
import threading
from flask_socketio import SocketIO, emit

import win32print
import multiprocessing

app = Flask(__name__)
socketio = SocketIO(app)

# Store uploaded file info for admin view
uploaded_files_info = []

# Track today's stats
printed_pages_today = 0
files_uploaded_today = 0
last_updated_date = datetime.now().date()

@socketio.on('connect')
def send_initial_coin_count():
    global coin_value_sum
    print(f"WebSocket client connected. Emitting initial coin count: {coin_value_sum}") # ADD THIS LINE
    emit('update_coin_count', {'count': coin_value_sum})

# Emit updates to connected clients
def update_coin_count(count):
    socketio.emit('update_coin_count', {'count': count})

arduino = None
coin_count = 0
coin_value_sum = 0  # Track the total value of inserted coins

app.config['UPLOAD_FOLDER'] = os.path.join(os.getcwd(), 'uploads')
app.config['STATIC_FOLDER'] = os.path.join(os.getcwd(), 'static', 'uploads')
app.config['ALLOWED_EXTENSIONS'] = {'pdf', 'jpg', 'jpeg', 'png', 'docx', 'doc'}

os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
os.makedirs(app.config['STATIC_FOLDER'], exist_ok=True)

images = []  # Store images globally for simplicity
total_pages = 0  # Keep track of total pages
selected_previews = []  # Store paths to selected preview images

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in app.config['ALLOWED_EXTENSIONS']

def pdf_to_images(pdf_path):
    document = fitz.open(pdf_path)
    pdf_images = []
    for page_num in range(len(document)):
        page = document.load_page(page_num)
        pix = page.get_pixmap(alpha=False)
        image = np.frombuffer(pix.samples, dtype=np.uint8).reshape((pix.height, pix.width, 3))
        pdf_images.append(cv2.cvtColor(image, cv2.COLOR_RGB2BGR))
    document.close()
    return pdf_images

def docx_to_images(file_path):
    try:
        if not file_path.lower().endswith(('.doc', '.docx')):
            raise ValueError("Unsupported file type for Word documents.")
        
        pythoncom.CoInitialize()  # Initialize COM
        word = client.Dispatch("Word.Application")
        word.visible = False

        doc = word.Documents.Open(file_path)
        pdf_path = file_path.replace('.docx', '.pdf').replace('.doc', '.pdf')
        
        doc.SaveAs(pdf_path, FileFormat=17)  # 17 corresponds to PDF format
        doc.Close()

        word.Quit()
        pythoncom.CoUninitialize()

        images = pdf_to_images(pdf_path)
        os.remove(pdf_path)  # Clean up temporary file
        return images

    except Exception as e:
        print(f"Error processing Word document: {e}")
        return []
    
# def process_image(image):

#     if image is None:
#         print("Error: Unable to load image.")
#         return None, 0

#     # --- Blank White Page Detection ---
#     if np.all(image == 255):  # Check if the image is completely white
#         return image, 0.0  # Price is 0 for blank white pages

#     # --- Fully Black Page Pricing ---
#     if np.all(image == 0):  # Check if the image is completely black
#         price_per_pixel_color = 0.0000075
#         total_pixels = image.shape[0] * image.shape[1]  # Total pixels in the image
#         total_price_black = total_pixels * price_per_pixel_color
#         rounded_price_black = round(total_price_black, 2)
#         return image, min(rounded_price_black, 20.0)  # Cap price at 20

#     # --- Normal Page Processing ---
#     gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)  # Convert to grayscale for easier processing
#     thresh = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 11, 2)
#     contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
#     min_area = 1000  # Adjust this value as needed
#     filtered_contours = [cnt for cnt in contours if cv2.contourArea(cnt) > min_area]
#     mask_pixels = np.zeros_like(gray)
#     cv2.drawContours(mask_pixels, filtered_contours, -1, 255, thickness=cv2.FILLED)

#     # Color Area Pricing
#     hsv_image = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
#     lower_red = np.array([160, 50, 50])
#     upper_red = np.array([190, 255, 255])
#     lower_green = np.array([50, 50, 50])
#     upper_green = np.array([100, 255, 255])
#     lower_blue = np.array([100, 50, 50])
#     upper_blue = np.array([140, 255, 255])
#     red_mask = cv2.inRange(hsv_image, lower_red, upper_red)
#     green_mask = cv2.inRange(hsv_image, lower_green, upper_green)
#     blue_mask = cv2.inRange(hsv_image, lower_blue, upper_blue)
#     mask_colors = cv2.bitwise_or(red_mask, green_mask)
#     mask_colors = cv2.bitwise_or(mask_colors, blue_mask)

#     # Black Pixel Pricing
#     lower_black = np.array([0, 0, 0], dtype=np.uint8)
#     upper_black = np.array([50, 50, 50], dtype=np.uint8)
#     black_mask = cv2.inRange(image, lower_black, upper_black)

#     # Price Calculation
#     price_per_pixel_group = 0.0000065
#     price_per_pixel_color = 0.0000075
#     non_black_pixel_area = cv2.countNonZero(mask_pixels)
#     total_price_pixels = non_black_pixel_area * price_per_pixel_group
#     black_pixel_area = cv2.countNonZero(black_mask)
#     total_price_black = black_pixel_area * price_per_pixel_color
#     non_black_color_area = cv2.countNonZero(mask_colors)
#     total_price_colors = non_black_color_area * price_per_pixel_color
#     total_price = total_price_pixels + total_price_colors + total_price_black

#     # Cap total price at 20
#     total_price = min(total_price, 20.0)

#     # Apply Masks and Create Output Image
#     combined_mask = cv2.bitwise_or(mask_pixels, mask_colors)
#     processed_image = cv2.bitwise_and(image, image, mask=combined_mask)
#     rounded_price = round(total_price, 2)

#     return processed_image, rounded_price

def parse_page_selection(selection, total_pages):
    """Parses a page selection string like '1-3,5,7-9' into a sorted list of unique page indices (0-based)."""
    import re
    pages = set()
    if not selection:
        return list(range(total_pages))  # default: all pages

    parts = re.split(r',\s*', selection)
    for part in parts:
        if '-' in part:
            start, end = map(int, part.split('-'))
            pages.update(range(start - 1, end))  # 0-based
        elif part.isdigit():
            page_num = int(part)
            if 1 <= page_num <= total_pages:
                pages.add(page_num - 1)
    return sorted(p for p in pages if 0 <= p < total_pages)



def process_image(image, page_size="A4", color_option="Color"):
    import cv2
    import numpy as np

    if image is None:
        print("Error: Unable to load image.")
        return None, 0.0

    # === Paper cost based on size (per ream / 500 pcs) ===
    paper_costs = {
        "Short": 2.0,
        "A4": 3.0,
        "Long": 5.0
    }
    paper_cost = paper_costs.get(page_size, 195 / 500)

    # === Max allowed prices per page size ===
    max_price_caps = {
        "Short": 15.0,
        "A4": 18.0,
        "Long": 20.0
    }
    max_cap = max_price_caps.get(page_size, 18.0)
    # === Detect text-only document ===
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    total_pixels = gray.size
    white_ratio = np.sum(gray > 220) / total_pixels
    dark_text_ratio = np.sum((gray > 10) & (gray < 180)) / total_pixels

    # Criteria for text-only: mostly white, with light text coverage
    if white_ratio > 0.85 and dark_text_ratio < 0.15:
        text_only_prices = {
            "Short": 3.0,
            "A4": 3.0,
            "Long": 4.0
        }
        return image, text_only_prices.get(page_size, 3.0)
    

    # === White page ===
    if np.all(image == 255):
        return image, 0.0

    # === Full black page ===
    if np.all(image == 0):
        base_rate = 0.000006
        total_pixels = image.shape[0] * image.shape[1]
        base_cost = total_pixels * base_rate + paper_cost
        return image, round(min(base_cost * 1.5, max_cap), 2)

    # === Grayscale logic ===
    if color_option == "Grayscale":
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        total_pixels = gray.size

        edges = cv2.Canny(gray, 50, 150)
        edge_density = np.sum(edges > 0) / total_pixels
        dark_ratio = np.sum(gray < 100) / total_pixels

        hist = cv2.calcHist([gray], [0], None, [256], [0, 256])
        hist_norm = hist.ravel() / hist.sum()
        entropy = -np.sum(hist_norm * np.log2(hist_norm + 1e-7))

        # Explicit check for simple documents: mostly white, with small dark areas (text + logo)
        white_ratio = np.sum(gray > 220) / total_pixels
        dark_content_ratio = np.sum((gray > 10) & (gray < 180)) / total_pixels

        if white_ratio > 0.85 and dark_content_ratio < 0.15:
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



    # === Color logic ===
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    color_mask = cv2.inRange(hsv, np.array([0, 50, 50]), np.array([180, 255, 255]))
    color_pixel_count = cv2.countNonZero(color_mask)
    total_pixels = image.shape[0] * image.shape[1]
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
def get_printer_status():
    return [
        {
            "date": datetime.date.today().strftime("%m/%d/%Y"),
            "time": datetime.datetime.now().strftime("%I:%M %p"),
            "event": "Low Ink – Please refill the cyan cartridge."
        }
    ]


# Route for the main page
@app.route('/')
def admin_user():
    return render_template('admin-user.html')

@app.route('/admin-dashboard')
def admin_dashboard():
    data = {
        "total_sales": 5000,
        "printed_pages": printed_pages_today,
        "files_uploaded": files_uploaded_today,
        "current_balance": 3000,
        "sales_history": [
            {"method": "GCash", "amount": 500, "date": "2024-03-30", "time": "14:00"},
            {"method": "PayMaya", "amount": 250, "date": "2024-03-29", "time": "11:30"}
        ],
        "sales_chart": [500, 600, 700, 800]  # Example sales data for Chart.js
    }
    return render_template('admin-dashboard.html', data=data)

# Route for the admin files upload
@app.route('/admin-files-upload')
def admin_printed_pages():
    return render_template('admin-files-upload.html', uploaded=uploaded_files_info)


# Route for the admin activity log
@app.route('/admin-activity-log')
def admin_activity_log():
    printed = [
        {
            "date": "9/18/2024",
            "time": "11:52 PM",
            "event": "Paper Jam – The paper is stuck in the printer, causing a blockage."
        },
        {
            "date": "9/18/2024",
            "time": "11:52 PM",
            "event": "Low Ink/Toner – The printer's ink or toner is running low and needs to be replaced."
        },
        {
            "date": "9/18/2024",
            "time": "11:52 PM",
            "event": "Printer Offline – The printer is not connected to the network or is turned off."
        },
        {
            "date": "9/18/2024",
            "time": "11:52 PM",
            "event": "Out of Paper – The printer has run out of paper in the tray."
        },
        {
            "date": "9/18/2024",
            "time": "11:52 PM",
            "event": "Printer Error – A general error, often requiring troubleshooting or a reset."
        }
    ]
    return render_template('admin-activity-log.html', printed=printed)

# Route for the admin balance
@app.route('/admin-balance')
def admin_balance():
    return render_template('admin-balance.html')

# Route for the admin feedbacks
@app.route('/admin-feedbacks')
def admin_feedbacks():
    url = "https://docs.google.com/spreadsheets/d/e/2PACX-1vQlpo4U0Z7HHUmeM3XIV9GW6xZ31WGILRjNm8hGCEuEahnxTzyFg5PLOXpFctb_69PZEDH8UAAoOEtk/pub?output=csv"
    response = requests.get(url)
    f = StringIO(response.text)
    reader = csv.DictReader(f)

    feedbacks = []
    for row in reader:
        rating_str = row.get("How would you rate your experience?", "0")
        try:
            rating = int(float(rating_str))
        except (ValueError, TypeError):
            rating = 0
            
        feedbacks.append({
            "user": row.get("Nickname"),
            "message": row.get("Comment"),
            "time": row.get("Timestamp"),
            "rating": rating  # use parsed value
        })

    return render_template('admin-feedbacks.html', feedbacks=feedbacks)


# Route for the file upload page
@app.route('/file-upload')
def file_upload():
    return render_template('file-upload.html')

# Route for the index
@app.route('/index')
def index():
    return render_template('index.html')

# Route for manual file upload page
@app.route('/manual-upload')
def manual_upload():
    files = os.listdir(app.config['UPLOAD_FOLDER'])  # List all files in the upload folder
    return render_template('manual-display-upload.html', files=files)

@app.route('/online-upload', methods=['GET', 'POST'])
def online_upload():
    # If it's a POST request (file upload)
    if request.method == 'POST':
        # Check if a file is part of the request
        file = request.files.get('file')
        if file:
            filename = file.filename
            file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
            return f'File "{filename}" uploaded successfully!'
        else:
            return 'No file selected!', 400

    # If it's a GET request (display upload form and ToffeeShare link)
    else:
        # List the files in the upload folder if you want to display them
        files = os.listdir(app.config['UPLOAD_FOLDER'])
        # You can provide a link to ToffeeShare or any related functionality
        toffee_share_link = "https://toffeeshare.com/nearby"
        return render_template('upload_page.html', files=files, toffee_share_link=toffee_share_link)
    
@app.route('/payment')
def payment_page():
    return render_template('payment.html')

@app.route('/upload', methods=['POST'])
def upload_file():
    global images, total_pages
    global printed_pages_today, files_uploaded_today, last_updated_date
    global uploaded_files_info

# Check if the day has changed
    if datetime.now().date() != last_updated_date:
        printed_pages_today = 0
        files_uploaded_today = 0
        last_updated_date = datetime.now().date()

    if 'file' not in request.files:
        return jsonify({"error": "No file part"}), 400
    
    file = request.files['file']
    if file.filename == '':
        return jsonify({"error": "No selected file"}), 400

    if file and allowed_file(file.filename):
        filename = file.filename
        file_ext = filename.split('.')[-1].lower()
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        file.save(filepath)

        if file_ext == 'pdf':
            images = pdf_to_images(filepath)
            total_pages = len(images)

        elif file_ext in ('jpg', 'jpeg', 'png'):
            images = [cv2.imread(filepath)]
            total_pages = 1
        
        elif file_ext in ('docx', 'doc'):
            images = docx_to_images(filepath)
            total_pages = len(images)

        else:
            return jsonify({"error": "Unsupported file format"}), 400

        printed_pages_today += total_pages
        files_uploaded_today += 1

        # ✅ Save to uploaded_files_info
        uploaded_files_info.append({
            'file': filename,
            'type': file_ext,
            'pages': total_pages,
            'time': datetime.now().strftime('%I:%M %p')
        })

        return jsonify({
            "message": "File uploaded successfully!",
            "fileName": filename,
            "totalPages": total_pages
        }), 200

    return jsonify({"error": "Invalid file format"}), 400

@app.route('/generate_preview', methods=['POST'])
def generate_preview():
    global images, selected_previews
    data = request.json
    page_selection = data.get('pageSelection', '')  # e.g., '1-2,4'
    num_copies = int(data.get('numCopies', 1))
    page_size = data.get('pageSize', 'A4')
    color_option = data.get('colorOption', 'Color')
    orientation = data.get('orientationOption', 'portrait')  # Get the orientation

    try:
        selected_indexes = parse_page_selection(page_selection, len(images))
        previews = []

        for idx in selected_indexes:
            if idx >= len(images):
                continue
            processed_img = images[idx].copy()

            # Calculate aspect ratio of the image
            aspect_ratio = processed_img.shape[1] / processed_img.shape[0]

            # Set the new dimensions based on the orientation
            if orientation == 'landscape':
                # Landscape orientation - Width is larger
                new_width = 1200  # Example landscape width
                new_height = int(new_width / aspect_ratio)  # Scale proportionally
                processed_img = cv2.resize(processed_img, (new_width, new_height))
            else:  # Portrait orientation
                # Portrait orientation - Height is larger
                new_height = 1200  # Example portrait height
                new_width = int(new_height * aspect_ratio)  # Scale proportionally
                processed_img = cv2.resize(processed_img, (new_width, new_height))

            # Handle color option (Grayscale or Color)
            if color_option == 'Grayscale':
                processed_img = cv2.cvtColor(processed_img, cv2.COLOR_BGR2GRAY)
                processed_img = cv2.cvtColor(processed_img, cv2.COLOR_GRAY2BGR)

            # Save the preview for each copy
            for copy_idx in range(num_copies):
                filename = f"preview_{idx + 1}_{page_size}_{color_option}_{orientation}_{copy_idx}.jpg"
                preview_path = os.path.join(app.config['STATIC_FOLDER'], filename)
                cv2.imwrite(preview_path, processed_img)
                previews.append(f"/uploads/{filename}")

        selected_previews = previews
        return jsonify({"previews": previews}), 200

    except Exception as e:
        return jsonify({"error": f"Failed to generate previews: {e}"}), 500


# def generate_preview():
#     global images, selected_previews
#     data = request.json
#     page_from = int(data.get('pageFrom', 1))
#     page_to = int(data.get('pageTo', 1))
#     num_copies = int(data.get('numCopies', 1))
#     page_size = data.get('pageSize', 'A4')
#     color_option = data.get('colorOption', 'Color')

#     try:
#         selected_images = images[page_from-1:page_to]

#         previews = []
#         for idx, img in enumerate(selected_images):
#             processed_img = img.copy()

#             if page_size == 'Short':
#                 processed_img = cv2.resize(processed_img, (800, 1000))
#             elif page_size == 'Long':
#                 processed_img = cv2.resize(processed_img, (800, 1200))
#             elif page_size == 'A4':
#                 processed_img = cv2.resize(processed_img, (800, 1100))

#             if color_option == 'Grayscale':
#                 processed_img = cv2.cvtColor(processed_img, cv2.COLOR_BGR2GRAY)
#                 processed_img = cv2.cvtColor(processed_img, cv2.COLOR_GRAY2BGR)

#             for _ in range(num_copies):
#                 preview_path = os.path.join(app.config['STATIC_FOLDER'], f"preview_{idx + 1}_{page_size}_{color_option}.jpg")
#                 cv2.imwrite(preview_path, processed_img)
#                 previews.append(f"/uploads/preview_{idx + 1}_{page_size}_{color_option}.jpg")

#         selected_previews = previews  # Save for rendering in result.html
#         return jsonify({"previews": previews}), 200

#     except Exception as e:
#         return jsonify({"error": f"Failed to generate previews: {e}"}), 500

@app.route('/preview_with_price', methods=['POST'])
def preview_with_price():
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


# @app.route('/preview_with_price', methods=['POST'])
# def preview_with_price():
#     """Generate previews and calculate prices for selected pages."""
#     global images
#     if not images:
#         return jsonify({"error": "No images available. Please upload a file first."}), 400

#     data = request.json
#     page_from = int(data.get('pageFrom', 1))
#     page_to = int(data.get('pageTo', 1))
#     num_copies = int(data.get('numCopies', 1))
#     page_size = data.get('pageSize', 'A4')
#     color_option = data.get('colorOption', 'Color')

#     try:
#         # Fetch selected pages based on the range
#         selected_images = images[page_from - 1:page_to]
#         previews = []
#         page_prices = []
#         total_price = 0

#         if color_option == 'Grayscale':
#             for idx, img in enumerate(selected_images):
#                 processed_img, page_price = process_image(img, page_size=page_size, color_option="Grayscale")
#                 page_price *= num_copies
#                 total_price += page_price

#                 # Save grayscale preview image
#                 preview_path = os.path.join(app.config['STATIC_FOLDER'], f"grayscale_preview_{page_from + idx}.jpg")
#                 gray_preview = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
#                 cv2.imwrite(preview_path, gray_preview)
#                 previews.append(f"/uploads/grayscale_preview_{page_from + idx}.jpg")

#                 # Add pricing info
#                 page_prices.append({
#                     "page": page_from + idx,
#                     "price": page_price,
#                     "processed": f"/uploads/grayscale_preview_{page_from + idx}.jpg"
#                 })

#             total_price = round(total_price)

#             return jsonify({
#                 "totalPrice": total_price,
#                 "pagePrices": page_prices,
#                 "previews": [{"page": page_from + idx, "path": preview} for idx, preview in enumerate(previews)]
#             }), 200

#         # Process for color pages
#         for idx, img in enumerate(selected_images):
#             processed_img, page_price = process_image(img, page_size=page_size, color_option=color_option)
#             page_price *= num_copies  # Multiply price by the number of copies
#             total_price += page_price

#             # Save original preview
#             preview_path = os.path.join(app.config['STATIC_FOLDER'], f"preview_{page_from + idx}.jpg")
#             cv2.imwrite(preview_path, img)
#             previews.append(f"/uploads/preview_{page_from + idx}.jpg")

#             # Save segmented/processed image
#             segmented_path = os.path.join(app.config['STATIC_FOLDER'], f"segmented_{page_from + idx}.jpg")
#             cv2.imwrite(segmented_path, processed_img)
#             page_prices.append({
#                 "page": page_from + idx,
#                 "price": page_price,
#                 "original": f"/uploads/preview_{page_from + idx}.jpg",
#                 "processed": f"/uploads/segmented_{page_from + idx}.jpg"
#             })

#         total_price = round(total_price)  # Round the total price to a whole number

#         return jsonify({
#             "totalPrice": total_price,
#             "pagePrices": page_prices,
#             "previews": previews
#         }), 200

#     except Exception as e:
#         return jsonify({"error": f"Failed to generate previews with pricing: {e}"}), 500

# def preview_with_price():
#     """Generate previews and calculate prices for selected pages."""
#     global images
#     if not images:
#         return jsonify({"error": "No images available. Please upload a file first."}), 400

#     data = request.json
#     page_from = int(data.get('pageFrom', 1))
#     page_to = int(data.get('pageTo', 1))
#     num_copies = int(data.get('numCopies', 1))
#     page_size = data.get('pageSize', 'A4')
#     color_option = data.get('colorOption', 'Color')

#     try:
#         selected_images = images[page_from - 1:page_to]
#         previews = []
#         page_prices = []
#         total_price = 0

#         if color_option == 'Grayscale':
#             for idx, img in enumerate(selected_images):
#                 processed_img, page_price = process_image(img, page_size=page_size, color_option="Grayscale")
#                 page_price *= num_copies
#                 total_price += page_price

#         # Save grayscale preview image
#                 preview_path = os.path.join(app.config['STATIC_FOLDER'], f"grayscale_preview_{page_from + idx}.jpg")
#                 gray_preview = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
#                 cv2.imwrite(preview_path, gray_preview)
#                 previews.append(f"/uploads/grayscale_preview_{page_from + idx}.jpg")

#                 # Add pricing info
#                 page_prices.append({
#                     "page": page_from + idx,
#                     "price": page_price,
#                     "processed": f"/uploads/grayscale_preview_{page_from + idx}.jpg"
#                 })

#             total_price = round(total_price)

#             return jsonify({
#                 "totalPrice": total_price,
#                 "pagePrices": page_prices,
#                 "previews": [{"page": page_from + idx, "path": preview} for idx, preview in enumerate(previews)]
#             }), 200

#         # Process for color (this part remains unchanged)
#         for idx, img in enumerate(selected_images):
#             processed_img, page_price = process_image(img, page_size=page_size, color_option=color_option)
#             page_price *= num_copies  # Multiply price by the number of copies
#             total_price += page_price

#             # Save original preview
#             preview_path = os.path.join(app.config['STATIC_FOLDER'], f"preview_{page_from + idx}.jpg")
#             cv2.imwrite(preview_path, img)
#             previews.append(f"/uploads/preview_{page_from + idx}.jpg")

#             # Save segmented/processed image
#             segmented_path = os.path.join(app.config['STATIC_FOLDER'], f"segmented_{page_from + idx}.jpg")
#             cv2.imwrite(segmented_path, processed_img)
#             page_prices.append({
#                 "page": page_from + idx,
#                 "price": page_price,
#                 "original": f"/uploads/preview_{page_from + idx}.jpg",
#                 "processed": f"/uploads/segmented_{page_from + idx}.jpg"
#             })

#         total_price = round(total_price)  # Round the total price to a whole number

#         return jsonify({
#             "totalPrice": total_price,
#             "pagePrices": page_prices,
#             "previews": previews
#         }), 200

#     except Exception as e:
#         return jsonify({"error": f"Failed to generate previews with pricing: {e}"}), 500

# START OF ARDUINO AND COIN SLOT CONNECTION CODE
@app.route('/arduino-payment')
def arduino_payment_page():
    global arduino
    if not arduino:
        try:
            arduino = serial.Serial('COM6', 9600, timeout=1)
            time.sleep(2)
            print("Arduino successfully initialized.")
        except Exception as e:
            print(f"Failed to initialize Arduino: {e}")
            return "Error: Could not connect to Arduino."
    
    return render_template('payment.html')

def read_serial_data_arduino():
    global arduino, coin_value_sum
    serial_port = 'COM15'  # Replace with your Arduino's serial port
    baud_rate = 9600
    try:
        arduino = serial.Serial(serial_port, baud_rate, timeout=1)
        print(f"Successfully opened serial port {serial_port} for Arduino coin detection.")
        while True:
            if arduino.in_waiting > 0:
                try:
                    message = arduino.readline().decode('utf-8').strip()
                    print(f"Received from Arduino: {message}")
                    if message.startswith("Detected coin worth ₱"):
                        try:
                            value = int(message.split("₱")[1])
                            coin_value_sum += value  # ACCUMULATE THE VALUE HERE
                            print(f"Coin inserted: ₱{value}, Total: ₱{coin_value_sum}")
                            print(f"Emitting WebSocket event: update_coin_count - {coin_value_sum}")
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
        arduino = None
    finally:
        if arduino and arduino.is_open:
            arduino.close()
            print(f"Closed serial port {serial_port} for Arduino.")

@app.route('/detect_coins')
def start_coin_detection():
    # This route is no longer needed as the coin detection runs in a background thread.
    return "Coin detection service is running in the background."

@app.route('/coin_count')
def get_coin_count():
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

        temp_previews = []
        for idx in selected_indexes:
            img = images[idx].copy()

            if page_size == 'Short':
                img = cv2.resize(img, (800, 1000))
            elif page_size == 'Long':
                img = cv2.resize(img, (800, 1200))
            elif page_size == 'A4':
                img = cv2.resize(img, (800, 1100))

            if color_option == 'Grayscale':
                img = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
                img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)

            for copy_idx in range(num_copies):
                filename = f"print_preview_{idx + 1}_{page_size}_{color_option}_{copy_idx}.jpg"
                path = os.path.join(app.config['STATIC_FOLDER'], filename)
                cv2.imwrite(path, img)
                temp_previews.append(path)

        pdf_path = os.path.join(app.config['UPLOAD_FOLDER'], "printable_preview.pdf")
        pdf = FPDF()
        pdf.set_auto_page_break(auto=True, margin=15)

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

        pdf.output(pdf_path)

        if os.name == 'nt':
            import win32print
            import win32api
            printer_name = win32print.GetDefaultPrinter()
            win32api.ShellExecute(0, "print", pdf_path, None, ".", 0)
        else:
            os.system(f'lp "{pdf_path}"')

        return jsonify({"success": True, "message": "Document sent to the printer successfully!"}), 200

    except Exception as e:
        print(f"Error printing document: {e}")
        return jsonify({"error": f"Failed to print document: {e}"}), 500
    
# Update result route to display both previews and segmentation
@app.route('/result', methods=['GET'])
def result():
    global selected_previews
    return render_template('result.html', previews=selected_previews, price_info=None)

@app.route('/uploads/<filename>')
def uploaded_file(filename):
    return send_from_directory(app.config['STATIC_FOLDER'], filename)

# CHECK PRINTER STATUS
def check_printer_status(printer_name):
    try:
        hPrinter = win32print.OpenPrinter(printer_name)
        jobs = win32print.EnumJobs(hPrinter, 0, -1, 2)
        win32print.ClosePrinter(hPrinter)

        if jobs:
            return "Printing"
        else:
            return "Idle"

    except Exception as e:
        return f"Error: {e}"
    
def check_printer_status_loop(printer_name, upload_folder):
    last_status = "Idle"  # Initialize last_status to "Idle"

    while True:
        current_status = check_printer_status(printer_name)

        if current_status == "Printing" and last_status == "Idle":
            print(f"Printer Status: {current_status}") 

        elif current_status == "Idle" and last_status == "Printing":
            print(f"Printer Status: {current_status}") 
            # FILE DELETION AFTER PRINTING
            for filename in os.listdir(upload_folder):
                file_path = os.path.join(upload_folder, filename)
                try:
                    os.remove(file_path)
                    print(f"Deleted file: {file_path}")
                except FileNotFoundError:
                    pass  # File might have already been deleted

        last_status = current_status

        time.sleep(1)  # Check every second
# END OF CHECK PRINTER STATUS

def read_serial_data():
    """Continuously reads data from the serial port, extracts amount, and emits it."""
    serial_port = 'COM6'  # Replace with your Arduino's serial port
    baud_rate = 9600

    try:
        ser = serial.Serial(serial_port, baud_rate, timeout=1)
        print(f"Successfully opened serial port {serial_port}")
        while True:
            if ser.in_waiting > 0:
                try:
                    message = ser.readline().decode('utf-8').strip()
                    if message:
                        print(f"Received from Arduino: {message}")
                        # --- Extract Payment Amount from the specific SMS format ---
                        if "You have received PHP" in message and "via QRPH" in message:
                            try:
                                start_index = message.find("PHP") + 4  # Find the start of the amount
                                end_index = message.find(" via QRPH")  # Find the end of the amount
                                if start_index != -1 and end_index != -1 and end_index > start_index:
                                    amount_str = message[start_index:end_index].strip()
                                    extracted_amount = float(amount_str)
                                    print(f"Extracted amount: {extracted_amount}")
                                    socketio.emit('payment_received', {'amount': extracted_amount})
                                else:
                                    print("Could not find amount delimiters in SMS.")
                            except ValueError:
                                print("Could not convert extracted amount to float.")
                except UnicodeDecodeError:
                    print("Error decoding serial data.")
            time.sleep(0.1)
    except serial.SerialException as e:
        print(f"Error opening serial port {serial_port}: {e}")
    finally:
        if 'ser' in locals() and ser.is_open:
            ser.close()
            print(f"Closed serial port {serial_port}")

@app.route('/payment-success')
def payment_success():
    return render_template('payment-success.html')

@app.route('/payment-type')
def payment_type():
    return render_template('payment-type.html')

if __name__ == "__main__":
    # Get the default printer name
    printer_name = win32print.GetDefaultPrinter()

    # Path to the uploads folder (replace with your actual path)
    upload_folder = "uploads" 

    # Start the serial reading in a separate thread
    serial_thread = threading.Thread(target=read_serial_data, daemon=True)
    serial_thread.start()

    # Start the coin detection thread (assuming it's defined elsewhere)
    arduino_thread = threading.Thread(target=read_serial_data_arduino, daemon=True)
    arduino_thread.start()

    # Create a tuple of arguments for the worker function
    args = (printer_name, upload_folder)

    # Start the printer status monitoring in a separate process
    printer_process = multiprocessing.Process(target=check_printer_status_loop, 
                                             args=args) 
    printer_process.start()

    # Start the Flask app
    socketio.run(app, debug=False)