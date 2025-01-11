import os
from flask import Flask, request, render_template, jsonify, send_from_directory
import cv2
import numpy as np
import fitz  # PyMuPDF
from docx import Document
from io import BytesIO
import pythoncom
from win32com import client
from fpdf import FPDF

import serial
import time
import threading
from flask_socketio import SocketIO, emit

app = Flask(__name__)
socketio = SocketIO(app)

@socketio.on('connect')
def send_total_price():
    total_price = 45.75
    emit('update_total_price', {'price': total_price})

# Emit updates to connected clients
def update_coin_count(count):
    socketio.emit('update_coin_count', {'count': count})

arduino = None
coin_count = 0

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
    
def process_image(image):
    """
    Process the image to calculate price and apply image segmentation.
    This version separately calculates prices for pixel groups and colored areas,
    assigns a price of 0 for blank white pages, and charges black pages.

    Args:
        image: The input image as a NumPy array.

    Returns:
        A tuple containing:
            - The processed image as a NumPy array.
            - The calculated price as a float.
    """
    if image is None:
        print("Error: Unable to load image.")
        return None, 0

    # --- Blank White Page Detection ---
    if np.all(image == 255):  # Check if the image is completely white
        return image, 0.0  # Price is 0 for blank white pages

    # --- Fully Black Page Pricing ---
    if np.all(image == 0):  # Check if the image is completely black
        price_per_pixel_color = 0.0000075
        total_pixels = image.shape[0] * image.shape[1]  # Total pixels in the image
        total_price_black = total_pixels * price_per_pixel_color
        rounded_price_black = round(total_price_black, 2)
        return image, min(rounded_price_black, 20.0)  # Cap price at 20

    # --- Normal Page Processing ---
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)  # Convert to grayscale for easier processing
    thresh = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 11, 2)
    contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    min_area = 1000  # Adjust this value as needed
    filtered_contours = [cnt for cnt in contours if cv2.contourArea(cnt) > min_area]
    mask_pixels = np.zeros_like(gray)
    cv2.drawContours(mask_pixels, filtered_contours, -1, 255, thickness=cv2.FILLED)

    # Color Area Pricing
    hsv_image = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    lower_red = np.array([160, 50, 50])
    upper_red = np.array([190, 255, 255])
    lower_green = np.array([50, 50, 50])
    upper_green = np.array([100, 255, 255])
    lower_blue = np.array([100, 50, 50])
    upper_blue = np.array([140, 255, 255])
    red_mask = cv2.inRange(hsv_image, lower_red, upper_red)
    green_mask = cv2.inRange(hsv_image, lower_green, upper_green)
    blue_mask = cv2.inRange(hsv_image, lower_blue, upper_blue)
    mask_colors = cv2.bitwise_or(red_mask, green_mask)
    mask_colors = cv2.bitwise_or(mask_colors, blue_mask)

    # Black Pixel Pricing
    lower_black = np.array([0, 0, 0], dtype=np.uint8)
    upper_black = np.array([50, 50, 50], dtype=np.uint8)
    black_mask = cv2.inRange(image, lower_black, upper_black)

    # Price Calculation
    price_per_pixel_group = 0.0000065
    price_per_pixel_color = 0.0000075
    non_black_pixel_area = cv2.countNonZero(mask_pixels)
    total_price_pixels = non_black_pixel_area * price_per_pixel_group
    black_pixel_area = cv2.countNonZero(black_mask)
    total_price_black = black_pixel_area * price_per_pixel_color
    non_black_color_area = cv2.countNonZero(mask_colors)
    total_price_colors = non_black_color_area * price_per_pixel_color
    total_price = total_price_pixels + total_price_colors + total_price_black

    # Cap total price at 20
    total_price = min(total_price, 20.0)

    # Apply Masks and Create Output Image
    combined_mask = cv2.bitwise_or(mask_pixels, mask_colors)
    processed_image = cv2.bitwise_and(image, image, mask=combined_mask)
    rounded_price = round(total_price, 2)

    return processed_image, rounded_price

@app.route('/')
def file_upload():
    return render_template('file-upload.html')

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
    
@app.route('/upload', methods=['POST'])
def upload_file():
    global images, total_pages

    if 'file' not in request.files:
        return jsonify({"error": "No file part"}), 400
    
    file = request.files['file']
    if file.filename == '':
        return jsonify({"error": "No selected file"}), 400

    if file and allowed_file(file.filename):
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], file.filename)
        file.save(filepath)

        if filepath.endswith('.pdf'):
            images = pdf_to_images(filepath)
            total_pages = len(images)

        elif filepath.endswith(('.jpg', '.jpeg', '.png')):
            images = [cv2.imread(filepath)]
            total_pages = 1
        
        elif filepath.endswith(('.docx', '.doc')):
            images = docx_to_images(filepath)
            total_pages = len(images)

        else:
            return jsonify({"error": "Unsupported file format"}), 400


        return jsonify({
            "message": "File uploaded successfully!",
            "fileName": file.filename,
            "totalPages": total_pages,
            # "flashDrivePath": flash_file_path
        }), 200

    return jsonify({"error": "Invalid file format"}), 400

@app.route('/generate_preview', methods=['POST'])
def generate_preview():
    global images, selected_previews
    data = request.json
    page_from = int(data.get('pageFrom', 1))
    page_to = int(data.get('pageTo', 1))
    num_copies = int(data.get('numCopies', 1))
    page_size = data.get('pageSize', 'A4')
    color_option = data.get('colorOption', 'Color')

    try:
        selected_images = images[page_from-1:page_to]

        previews = []
        for idx, img in enumerate(selected_images):
            processed_img = img.copy()

            if page_size == 'Short':
                processed_img = cv2.resize(processed_img, (800, 1000))
            elif page_size == 'Long':
                processed_img = cv2.resize(processed_img, (800, 1200))
            elif page_size == 'A4':
                processed_img = cv2.resize(processed_img, (800, 1100))

            if color_option == 'Grayscale':
                processed_img = cv2.cvtColor(processed_img, cv2.COLOR_BGR2GRAY)
                processed_img = cv2.cvtColor(processed_img, cv2.COLOR_GRAY2BGR)

            for _ in range(num_copies):
                preview_path = os.path.join(app.config['STATIC_FOLDER'], f"preview_{idx + 1}_{page_size}_{color_option}.jpg")
                cv2.imwrite(preview_path, processed_img)
                previews.append(f"/uploads/preview_{idx + 1}_{page_size}_{color_option}.jpg")

        selected_previews = previews  # Save for rendering in result.html
        return jsonify({"previews": previews}), 200

    except Exception as e:
        return jsonify({"error": f"Failed to generate previews: {e}"}), 500

@app.route('/preview_with_price', methods=['POST'])
def preview_with_price():
    """Generate previews and calculate prices for selected pages."""
    global images
    if not images:
        return jsonify({"error": "No images available. Please upload a file first."}), 400

    data = request.json
    page_from = int(data.get('pageFrom', 1))
    page_to = int(data.get('pageTo', 1))
    num_copies = int(data.get('numCopies', 1))
    color_option = data.get('colorOption', 'Color')

    try:
        selected_images = images[page_from - 1:page_to]
        previews = []
        page_prices = []
        total_price = 0

        if color_option == 'Grayscale':
            # Set price per page for grayscale
            price_per_page = 2

            for idx, img in enumerate(selected_images):
                preview_path = os.path.join(app.config['STATIC_FOLDER'], f"grayscale_preview_{page_from + idx}.jpg")
                gray_preview = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
                cv2.imwrite(preview_path, gray_preview)
                previews.append(f"/uploads/grayscale_preview_{page_from + idx}.jpg")

                # Add price for grayscale page (2 per page)
                page_price = price_per_page * num_copies
                total_price += page_price

                # Add page price breakdown for grayscale
                page_prices.append({
                    "page": page_from + idx,
                    "price": page_price,
                    "profit": 0
                })

            return jsonify({
                "totalPrice": total_price,
                "pagePrices": page_prices,
                "previews": [{"page": page_from + idx, "path": preview} for idx, preview in enumerate(previews)]
            }), 200

        # Process for color (this part remains unchanged)
        for idx, img in enumerate(selected_images):
            processed_img, page_price = process_image(img)
            page_price *= num_copies  # Multiply price by the number of copies
            total_price += page_price

            # Save original preview
            preview_path = os.path.join(app.config['STATIC_FOLDER'], f"preview_{page_from + idx}.jpg")
            cv2.imwrite(preview_path, img)
            previews.append(f"/uploads/preview_{page_from + idx}.jpg")

            # Save segmented/processed image
            segmented_path = os.path.join(app.config['STATIC_FOLDER'], f"segmented_{page_from + idx}.jpg")
            cv2.imwrite(segmented_path, processed_img)
            page_prices.append({
                "page": page_from + idx,
                "price": page_price,
                "original": f"/uploads/preview_{page_from + idx}.jpg",
                "processed": f"/uploads/segmented_{page_from + idx}.jpg"
            })

        total_price = round(total_price)  # Round the total price to a whole number

        return jsonify({
            "totalPrice": total_price,
            "pagePrices": page_prices,
            "previews": previews
        }), 200

    except Exception as e:
        return jsonify({"error": f"Failed to generate previews with pricing: {e}"}), 500

# START OF ARDUINO AND COIN SLOT CONNECTION CODE
@app.route('/payment')
def payment_page():
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

@app.route('/detect_coins')
def detect_coin():
    global coin_count, arduino
    print("Starting coin detection...")
    try:
        while True:
            if arduino and arduino.in_waiting > 0:
                # Read and decode the data from Arduino
                coin_status = arduino.readline().decode('utf-8').strip()
                print(f"Arduino Output: {coin_status}")  # Print raw data to the terminal
                
                # Check if the message contains "Credits:"
                if "Credits:" in coin_status:
                    try:
                        # Extract and update the coin count
                        coin_count = int(coin_status.split(":")[1].strip())
                        print(f"Updated Coin Count: {coin_count}")
                        
                        # Emit the updated coin count to clients (if using Flask-SocketIO)
                        socketio.emit('update_coin_count', {'count': coin_count})
                    except ValueError:
                        print("Invalid coin count format received.")
                
                time.sleep(0.1)  # Prevent overwhelming the CPU
    except KeyboardInterrupt:
        print("Coin detection stopped.")
        return "Coin detection stopped."

@app.route('/coin_count')
def get_coin_count():
    global coin_count
    return f"Total coins inserted: {coin_count}"
# END OF ARDUINO AND COIN SLOT CONNECTION CODE

# START OF PRINT DOCUMENT
@app.route('/print_document', methods=['POST'])
def print_document():
    global images
    if not images:
        return jsonify({"error": "No file uploaded or processed yet. Please upload a file."}), 400

    try:
        # Get latest print options from the request
        data = request.json
        print("Received print options:", data)

        # Extract and validate data
        page_from = int(data.get('pageFrom', 1))
        page_to = int(data.get('pageTo', 1))
        num_copies = int(data.get('numCopies', 1))
        page_size = data.get('pageSize', 'A4')
        color_option = data.get('colorOption', 'Color')

        if page_from < 1 or page_to < page_from:
            return jsonify({"error": "Invalid page range"}), 400
        
        # Select the pages and regenerate previews
        selected_images = images[page_from - 1:page_to]
        temp_previews = []

        for idx, img in enumerate(selected_images):
            processed_img = img.copy()

            # Apply page size
            if page_size == 'Short':
                processed_img = cv2.resize(processed_img, (800, 1000))
            elif page_size == 'Long':
                processed_img = cv2.resize(processed_img, (800, 1200))
            elif page_size == 'A4':
                processed_img = cv2.resize(processed_img, (800, 1100))

            # Apply color option
            if color_option == 'Grayscale':
                processed_img = cv2.cvtColor(processed_img, cv2.COLOR_BGR2GRAY)
                processed_img = cv2.cvtColor(processed_img, cv2.COLOR_GRAY2BGR)

            for _ in range(num_copies):
                preview_path = os.path.join(app.config['STATIC_FOLDER'], f"print_preview_{page_from + idx}_{page_size}_{color_option}.jpg")
                cv2.imwrite(preview_path, processed_img)
                temp_previews.append(preview_path)

        # Generate PDF from the latest previews
        pdf_path = os.path.join(app.config['UPLOAD_FOLDER'], "printable_preview.pdf")
        pdf = FPDF()
        pdf.set_auto_page_break(auto=True, margin=15)

        for preview_path in temp_previews:
            full_path = preview_path
            if not os.path.exists(full_path):
                continue
            # Get image dimensions
            img = cv2.imread(full_path)
            height, width, _ = img.shape
            aspect_ratio = width / height
            page_width = 210  # A4 width in mm
            page_height = page_width / aspect_ratio

            pdf.add_page()
            pdf.image(full_path, x=10, y=10, w=page_width, h=page_height)

        pdf.output(pdf_path)

        # Send the PDF to the printer
        if os.name == 'nt':  # Windows
            import win32print
            import win32api

            printer_name = win32print.GetDefaultPrinter()
            win32api.ShellExecute(0, "print", pdf_path, None, ".", 0)
        else:  # Linux/MacOS
            os.system(f'lp "{pdf_path}"')

        return jsonify({"success": True, "message": "Document sent to the printer successfully!"}), 200

    except Exception as e:
        print(f"Error printing document: {e}")
        return jsonify({"error": f"Failed to print document: {e}"}), 500
# END OF PRINT DOCUMENT

# Update result route to display both previews and segmentation
@app.route('/result', methods=['GET'])
def result():
    global selected_previews
    return render_template('result.html', previews=selected_previews, price_info=None)

@app.route('/uploads/<filename>')
def uploaded_file(filename):
    return send_from_directory(app.config['STATIC_FOLDER'], filename)

if __name__ == "__main__":
    # Start the coin detection thread first
    detection_thread = threading.Thread(target=detect_coin, daemon=True)
    detection_thread.start()

    # Start the Flask app
    socketio.run(app, debug=False)
