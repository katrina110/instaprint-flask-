import os
from flask import Flask, request, render_template, jsonify, send_from_directory
import fitz  # PyMuPDF
import cv2
import numpy as np

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['STATIC_FOLDER'] = 'static/uploads'
app.config['ALLOWED_EXTENSIONS'] = {'pdf'}

# Ensure the upload folder and static folder exist
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
os.makedirs(app.config['STATIC_FOLDER'], exist_ok=True)

def allowed_file(filename):
    """Check if the file extension is allowed."""
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in app.config['ALLOWED_EXTENSIONS']

def pdf_to_images(pdf_path):
    """Convert PDF pages to images."""
    images = []
    document = fitz.open(pdf_path)
    for page_num in range(len(document)):
        page = document.load_page(page_num)
        pix = page.get_pixmap(alpha=False)
        if pix.n == 1:
            image = np.frombuffer(pix.samples, dtype=np.uint8).reshape((pix.height, pix.width))
            image = cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
        elif pix.n == 3:
            image = np.frombuffer(pix.samples, dtype=np.uint8).reshape((pix.height, pix.width, 3))
            image = cv2.cvtColor(image, cv2.COLOR_RGB2BGR)
        elif pix.n == 4:
            image = np.frombuffer(pix.samples, dtype=np.uint8).reshape((pix.height, pix.width, 4))
            image = cv2.cvtColor(image, cv2.COLOR_RGBA2BGR)
        else:
            raise ValueError(f"Unsupported number of channels: {pix.n}")
        images.append(image)
    document.close()
    return images

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
    rounded_price = round(price)

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

@app.route('/upload', methods=['POST'])
def upload_file():
    """Handle file upload and image processing."""
    if 'file' not in request.files:
        return jsonify({"error": "No file part"}), 400

    file = request.files['file']
    
    if file.filename == '':
        return jsonify({"error": "No selected file"}), 400

    if file and allowed_file(file.filename):
        # Save the uploaded file
        filename = os.path.join(app.config['UPLOAD_FOLDER'], file.filename)
        file.save(filename)
        
        # Convert PDF pages to images
        images = pdf_to_images(filename)
        total_price = 0
        output_images = []

        # Process each image and save the results
        for idx, image in enumerate(images):
            output_image, price = process_image(image)
            total_price += price
            # Save the processed image temporarily in static folder
            image_filename = f"output_page_{idx + 1}.jpg"
            output_image_path = os.path.join(app.config['STATIC_FOLDER'], image_filename)
            cv2.imwrite(output_image_path, output_image)
            output_images.append(image_filename)

        # Render result page with price and images
        return render_template('result.html', total_price=total_price, images=output_images)

    else:
        return jsonify({"error": "Invalid file format"}), 400

@app.route('/uploads/<filename>')
def uploaded_file(filename):
    """Serve processed images from the static folder."""
    return send_from_directory(app.config['STATIC_FOLDER'], filename)

if __name__ == "__main__":
    app.run(debug=True)
