from flask import Flask, render_template, request, redirect, url_for, send_from_directory, jsonify
import os

app = Flask(__name__)

# Set up the upload folder and allowed file extensions
UPLOAD_FOLDER = 'uploads'
ALLOWED_EXTENSIONS = {'jpg', 'jpeg', 'png', 'pdf', 'doc', 'docx'}
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

# Ensure the upload folder exists
if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)

# Function to check allowed file extensions
def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

# Main Route - Index page
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

# Route to handle the file upload
@app.route('/upload', methods=['POST'])
def upload_file():
    if 'file' not in request.files:
        return redirect(request.url)  # No file part
    file = request.files['file']
    if file and allowed_file(file.filename):
        filename = file.filename
        file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
        return redirect(url_for('manual_upload'))  # Redirect to manual-upload page after successful upload
    else:
        return "Invalid file type", 400  # If the file type is not allowed

# Route to serve the uploaded files
@app.route('/uploads/<filename>')
def uploaded_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

# DELETE FILES
@app.route('/delete-file', methods=['DELETE'])
def delete_file():
    file_name = request.args.get('filename')
    if file_name:
        try:
            os.remove(os.path.join(UPLOAD_FOLDER, file_name))  # Adjust UPLOAD_FOLDER as needed
            return jsonify({'message': 'File deleted successfully'}), 200
        except FileNotFoundError:
            return jsonify({'error': 'File not found'}), 404
        except Exception as e:
            return jsonify({'error': str(e)}), 500
    return jsonify({'error': 'Filename not provided'}), 400

# REDIRECT TO colored-pages.html
@app.route('/colored-page')
def colored_page():
    return render_template('colored-page.html')
    
# Main entry point for the Flask app
if __name__ == '__main__':
    app.run(debug=True)
