import os
from flask import Flask, render_template, request, redirect, url_for

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['ALLOWED_EXTENSIONS'] = {'pdf', 'docx', 'jpg', 'png'}

# Helper function to list uploaded files
def get_uploaded_files():
    files = []
    for filename in os.listdir(app.config['UPLOAD_FOLDER']):
        if os.path.isfile(os.path.join(app.config['UPLOAD_FOLDER'], filename)):
            files.append(filename)
    return files

@app.route('/uploaded-files')
def uploaded_files():
    files = get_uploaded_files()
    return render_template('uploaded-files.html', files=files)

if __name__ == '__main__':
    app.run(debug=True)
