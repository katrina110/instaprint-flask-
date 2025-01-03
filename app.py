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

app = Flask(__name__)
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


def docx_to_images(docx_path):
    try:
        pythoncom.CoInitialize()  # Initialize COM
        word = client.Dispatch("Word.Application")
        word.visible = False
        doc = word.Documents.Open(docx_path)

        pdf_path = docx_path.replace('.docx', '.pdf')
        doc.SaveAs(pdf_path, FileFormat=17)
        doc.Close()

        images = pdf_to_images(pdf_path)
        os.remove(pdf_path)
        return images
    except Exception as e:
        print(f"Error during DOCX to PDF conversion: {e}")
        return []


# Add the process_image function
def process_image(image):
    """Process the image to calculate price and apply image segmentation."""
    if image is None:
        print("Error: Unable to load image.")
        return None, 0

    # Define thresholds for dark and bright pixels
    lower_dark = np.array([0, 0, 0], dtype=np.uint8)
    upper_dark = np.array([50, 50, 50], dtype=np.uint8)
    lower_bright = np.array([200, 200, 200], dtype=np.uint8)
    upper_bright = np.array([255, 255, 255], dtype=np.uint8)

    dark_mask = cv2.inRange(image, lower_dark, upper_dark)
    bright_mask = cv2.inRange(image, lower_bright, upper_bright)

    # Calculate number of bright pixels
    num_bright_pixels = np.sum(bright_mask == 255)

    # Define price per bright pixel
    price_per_pixel = 0.0000075

    # Calculate total price based on number of bright pixels
    price = num_bright_pixels * price_per_pixel
    rounded_price = round(price, 2)

    # Convert image to HSV color space for gray mask
    hsv_image = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)

    # Define thresholds for gray pixels in HSV color space
    lower_gray = np.array([0, 0, 0], dtype=np.uint8)
    upper_gray = np.array([179, 50, 255], dtype=np.uint8)

    # Create mask for gray pixels
    gray_mask = cv2.inRange(hsv_image, lower_gray, upper_gray)

    # Combine masks for dark, bright, and gray pixels
    combined_mask = cv2.bitwise_or(dark_mask, bright_mask)
    combined_mask = cv2.bitwise_or(combined_mask, gray_mask)

    # Apply combined mask to the original image to get the processed image
    output_image = cv2.bitwise_and(image, image, mask=cv2.bitwise_not(combined_mask))

    # Replace the black (dark) pixels with white background
    output_image[combined_mask == 255] = [255, 255, 255]  # Set to white where mask is dark

    return output_image, rounded_price


@app.route('/')
def index():
    return render_template('index.html')


# Route for file-upload page after PIN validation
@app.route('/file-upload')
def file_upload():
    return render_template('file-upload.html')

# Route for manual file upload page
@app.route('/manual-upload')
def manual_upload():
    files = os.listdir(app.config['UPLOAD_FOLDER'])  # List all files in the upload folder
    return render_template('manual-display-upload.html', files=files)

@app.route('/online-upload')
def online_upload():
    files = os.listdir(app.config['UPLOAD_FOLDER'])  # List all files in the upload folder
    return render_template('online-display-upload.html', files=files)


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
            "totalPages": total_pages
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
            # Skip segmentation for grayscale; only generate previews and count pages
            for idx, img in enumerate(selected_images):
                preview_path = os.path.join(app.config['STATIC_FOLDER'], f"grayscale_preview_{page_from + idx}.jpg")
                gray_preview = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
                cv2.imwrite(preview_path, gray_preview)
                previews.append(f"/uploads/grayscale_preview_{page_from + idx}.jpg")

            return jsonify({
                "totalPrice": 0,  # No cost calculation for grayscale
                "pagePrices": [{"page": i + page_from, "price": 0} for i in range(len(selected_images))],
                "previews": [{"page": page_from + idx, "path": preview} for idx, preview in enumerate(previews)]
            }), 200

        # Process for color
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

        return jsonify({
            "totalPrice": total_price,
            "pagePrices": page_prices,
            "previews": previews
        }), 200

    except Exception as e:
        return jsonify({"error": f"Failed to generate previews with pricing: {e}"}), 500


# New Route: Print the Document
@app.route('/print_document', methods=['POST'])
def print_document():
    global images
    if not images:
        return jsonify({"error": "No file uploaded or processed yet. Please upload a file."}), 400

    try:
        # Get latest print options from the request
        data = request.json
        page_from = int(data.get('pageFrom', 1))
        page_to = int(data.get('pageTo', 1))
        num_copies = int(data.get('numCopies', 1))
        page_size = data.get('pageSize', 'A4')
        color_option = data.get('colorOption', 'Color')

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




# Update result route to display both previews and segmentation
@app.route('/result', methods=['GET'])
def result():
    global selected_previews
    return render_template('result.html', previews=selected_previews, price_info=None)


@app.route('/uploads/<filename>')
def uploaded_file(filename):
    return send_from_directory(app.config['STATIC_FOLDER'], filename)


if __name__ == "__main__":
    app.run(debug=True)
