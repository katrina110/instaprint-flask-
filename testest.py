import http.server
import socketserver
import os
from pathlib import Path
import html
import base64 # For encoding SVGs
from datetime import datetime, date, timedelta # timedelta for 5 minutes
import mimetypes 
from urllib.parse import quote, unquote

# --- Global Program Start Time ---
PROGRAM_START_TIME = datetime.now()

# --- Configuration ---
try:
    DOWNLOADS_DIR = Path.home() / "Downloads"
except Exception as e:
    print(f"Could not automatically determine Downloads folder: {e}")
    DOWNLOADS_DIR = Path("YOUR_DOWNLOADS_FOLDER_PATH_HERE") # Replace if auto-detection fails

PORT = 8000
ALLOWED_EXTENSIONS = {'.pdf', '.docx', '.jpg', '.jpeg', '.png'}

# --- EXTREMELY DANGEROUS: Automatic Deletion Configuration ---
ENABLE_AUTO_DELETION = False  # !!! DANGER: SET TO True TO ENABLE AUTOMATIC FILE DELETION !!!
# Deletion system activates after the program runs for this many minutes.
# Then, it deletes files older than this many minutes from their modification time.
AUTO_DELETION_ACTIVATION_AND_FILE_AGE_MINUTES = 5 
# --- End EXTREMELY DANGEROUS Zone ---

# --- SVG Icon Definitions ---
# (SVG_ICONS dictionary and create_svg_data_url function are unchanged from previous version)
def create_svg_data_url(svg_xml_content):
    encoded_svg = base64.b64encode(svg_xml_content.encode('utf-8')).decode('utf-8')
    return f"data:image/svg+xml;base64,{encoded_svg}"
SVG_ICONS = {
    ".pdf": create_svg_data_url("""<svg width="48" height="48" viewBox="0 0 48 48" xmlns="http://www.w3.org/2000/svg"><rect x="6" y="2" width="36" height="44" rx="3" fill="#E53935"/><text x="24" y="30" font-family="sans-serif" font-weight="bold" font-size="12" fill="#FFFFFF" text-anchor="middle">PDF</text></svg>"""),
    ".docx": create_svg_data_url("""<svg width="48" height="48" viewBox="0 0 48 48" xmlns="http://www.w3.org/2000/svg"><rect x="6" y="2" width="36" height="44" rx="3" fill="#1E88E5"/><line x1="12" y1="14" x2="36" y2="14" stroke="#FFFFFF" stroke-width="2"/><line x1="12" y1="22" x2="36" y2="22" stroke="#FFFFFF" stroke-width="2"/><line x1="12" y1="30" x2="28" y2="30" stroke="#FFFFFF" stroke-width="2"/></svg>"""),
    ".jpg": create_svg_data_url("""<svg width="48" height="48" viewBox="0 0 48 48" xmlns="http://www.w3.org/2000/svg"><rect x="6" y="6" width="36" height="36" rx="3" fill="#FB8C00"/><circle cx="16" cy="16" r="4" fill="#FFFFFF"/><polygon points="14 38, 24 24, 34 30, 34 42, 6 42, 6 38" fill="#FDD835"/></svg>"""),
    ".png": create_svg_data_url("""<svg width="48" height="48" viewBox="0 0 48 48" xmlns="http://www.w3.org/2000/svg"><rect x="6" y="6" width="36" height="36" rx="3" fill="#43A047"/><path d="M16 16 L24 24 L32 16 L24 32 Z" fill="#FFFFFF" stroke="#FFFFFF" stroke-width="1"/></svg>"""),
    "default_file": create_svg_data_url("""<svg width="48" height="48" viewBox="0 0 48 48" xmlns="http://www.w3.org/2000/svg"><path d="M12 2 H28 L40 14 V42 C40 43.1 39.1 44 38 44 H12 C10.9 44 10 43.1 10 42 V6 C10 3.9 10.9 2 12 2 Z" fill="#B0BEC5"/><path d="M28 2 V14 H40" fill="#78909C"/></svg>""")
}
SVG_ICONS['.jpeg'] = SVG_ICONS['.jpg']
# --- End SVG Icon Definitions ---

class DownloadsHandler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        current_time_for_request = datetime.now() 

        preview_file_prefix = "/preview_file/"
        if self.path.startswith(preview_file_prefix):
            # (File serving logic - unchanged)
            file_name_encoded = self.path[len(preview_file_prefix):]
            file_name_decoded = unquote(file_name_encoded)
            potential_file_path = DOWNLOADS_DIR / file_name_decoded
            resolved_file_path = potential_file_path.resolve()
            resolved_downloads_dir = DOWNLOADS_DIR.resolve()
            if not (resolved_file_path.is_file() and resolved_file_path.is_relative_to(resolved_downloads_dir)):
                self.send_error(403, "Forbidden")
                return
            try:
                content_type, _ = mimetypes.guess_type(str(resolved_file_path))
                if content_type is None: content_type = 'application/octet-stream'
                self.send_response(200)
                self.send_header("Content-type", content_type)
                self.send_header("Content-Length", str(resolved_file_path.stat().st_size))
                if content_type == 'application/pdf': self.send_header("Content-Disposition", "inline")
                self.end_headers()
                with open(resolved_file_path, 'rb') as f: self.wfile.write(f.read())
            except FileNotFoundError: self.send_error(404, "File not found for preview.")
            except Exception as e:
                print(f"Error serving file {resolved_file_path}: {e}")
                self.send_error(500, "Server error.")
            return

        if self.path == '/':
            todays_date_for_display = current_time_for_request.date()
            
            # --- Automatic Deletion Logic (EXTREMELY DANGEROUS) ---
            deletion_info_for_html = ""
            if ENABLE_AUTO_DELETION:
                program_runtime_gate = PROGRAM_START_TIME + timedelta(minutes=AUTO_DELETION_ACTIVATION_AND_FILE_AGE_MINUTES)
                
                if current_time_for_request >= program_runtime_gate:
                    print(f"\n[AUTO-DELETER] ({current_time_for_request.strftime('%Y-%m-%d %H:%M:%S')}) Program run > {AUTO_DELETION_ACTIVATION_AND_FILE_AGE_MINUTES}m. Checking files older than {AUTO_DELETION_ACTIVATION_AND_FILE_AGE_MINUTES}m to delete...")
                    
                    file_age_cutoff_time = current_time_for_request - timedelta(minutes=AUTO_DELETION_ACTIVATION_AND_FILE_AGE_MINUTES)
                    print(f"[AUTO-DELETER] Deleting files of types {', '.join(ALLOWED_EXTENSIONS)} modified before {file_age_cutoff_time.strftime('%Y-%m-%d %H:%M:%S')}")
                    deletion_info_for_html = f"ACTIVE. Deleting files modified before {file_age_cutoff_time.strftime('%H:%M:%S')}."

                    items_for_deletion_scan = []
                    try: items_for_deletion_scan = os.listdir(DOWNLOADS_DIR)
                    except Exception as e_scan: print(f"[AUTO-DELETER] Error scanning directory {DOWNLOADS_DIR}: {e_scan}")

                    deleted_count = 0
                    for item_name_to_check in items_for_deletion_scan:
                        item_path_to_check = DOWNLOADS_DIR / item_name_to_check
                        if item_path_to_check.is_file():
                            _, file_ext_to_check = os.path.splitext(item_name_to_check)
                            if file_ext_to_check.lower() in ALLOWED_EXTENSIONS:
                                try:
                                    mtime_ts_check = os.path.getmtime(item_path_to_check)
                                    mtime_datetime_check = datetime.fromtimestamp(mtime_ts_check)
                                    if mtime_datetime_check < file_age_cutoff_time:
                                        os.remove(item_path_to_check)
                                        print(f"[AUTO-DELETER] Deleted: {item_path_to_check} (Modified: {mtime_datetime_check.strftime('%Y-%m-%d %H:%M:%S')})")
                                        deleted_count += 1
                                except FileNotFoundError: pass 
                                except Exception as e_del: print(f"[AUTO-DELETER] Error deleting {item_path_to_check}: {e_del}")
                    
                    if deleted_count > 0: print(f"[AUTO-DELETER] Finished. Deleted {deleted_count} old file(s).")
                    else: print(f"[AUTO-DELETER] No files older than {AUTO_DELETION_ACTIVATION_AND_FILE_AGE_MINUTES} minute(s) found for deletion this time.")
                else:
                    time_until_active = program_runtime_gate - current_time_for_request
                    minutes_left = int(time_until_active.total_seconds() // 60)
                    seconds_left = int(time_until_active.total_seconds() % 60)
                    activation_time_str = program_runtime_gate.strftime('%H:%M:%S')
                    print(f"\n[AUTO-DELETER] ({current_time_for_request.strftime('%Y-%m-%d %H:%M:%S')}) Auto-deletion is armed. Will activate in approx {minutes_left}m {seconds_left}s (at {activation_time_str}).")
                    deletion_info_for_html = f"ARMED. Will activate around {activation_time_str} (in {minutes_left}m {seconds_left}s)."
            # --- End Automatic Deletion Logic ---

            self.send_response(200)
            self.send_header("Content-type", "text/html; charset=utf-8")
            self.end_headers()
            
            html_page_content = f"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>File Previews (Recent First)</title>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Oxygen-Sans, Ubuntu, Cantarell, "Helvetica Neue", sans-serif; margin: 20px; background-color: #f8f9fa; color: #212529; }}
        h1 {{ color: #343a40; border-bottom: 2px solid #dee2e6; padding-bottom: 10px; }}
        ul {{ list-style-type: none; padding: 0; display: flex; flex-wrap: wrap; gap: 20px; margin-top: 20px; }}
        li {{ padding: 10px; display: flex; flex-direction: column; align-items: center; width: 120px; min-height: 160px; border: 1px solid #e0e0e0; border-radius: 4px; background-color: #ffffff; box-shadow: 0 2px 4px rgba(0,0,0,0.05); justify-content: flex-start; }}
        .preview-container {{ width: 100px; height: 100px; display: flex; align-items: center; justify-content: center; margin-bottom: 8px; overflow: hidden; background-color: #f0f0f0; }}
        li .preview-image {{ width: 100%; height: 100%; object-fit: cover; }}
        li .preview-pdf-iframe {{ width: 100%; height: 100%; border: none; }}
        li .preview-svg-icon {{ width: 48px; height: 48px; }}
        li .filename {{ font-size: 12px; color: #333333; text-align: center; word-wrap: break-word; overflow-wrap: break-word; width: 100%; line-height: 1.3; margin-top: auto; }}
        .status-message {{ margin-top: 15px; padding: 10px; border-radius: .25rem; }}
        .empty-message {{ color: #6c757d; font-style: italic; background-color: #e9ecef; border: 1px solid #ced4da; }}
        .warning-box {{ background-color: #ffee58; color: #3E2723; border: 1px solid #FDD835; font-weight: bold; padding: 15px; margin-bottom:15px; }}
        .error-message {{ color: #721c24; background-color: #f8d7da; border: 1px solid #f5c6cb; font-weight: bold; }}
        .path-info {{ font-size: 0.9em; color: #6c757d; margin-bottom: 5px; }}
        .filter-info {{ font-size: 0.9em; color: #495057; background-color: #e2e3e5; padding: 8px; border-radius: .2rem; display: inline-block; }}
    </style>
</head>
<body>
    <h1>File Previews (Downloaded Today, Recent First)</h1>"""
            
            if ENABLE_AUTO_DELETION:
                html_page_content += f"""
                 <p class='status-message warning-box'>
                     <strong>EXTREME DANGER:</strong> Auto-deletion is ENABLED! Files older than {AUTO_DELETION_ACTIVATION_AND_FILE_AGE_MINUTES} minute(s) 
                     (of types: {', '.join(sorted(list(ALLOWED_EXTENSIONS)))}) in '{html.escape(str(DOWNLOADS_DIR))}' 
                     will be PERMANENTLY DELETED. <br>
                     Deletion system status: <strong>{deletion_info_for_html}</strong>
                 </p>
                 """

            html_page_content += f"""
    <p class="path-info">Listing from: {html.escape(str(DOWNLOADS_DIR))}</p>
    <p class="filter-info">Displaying {', '.join(sorted(list(ALLOWED_EXTENSIONS)))} files modified on {todays_date_for_display.strftime('%B %d, %Y')}, newest first.</p>
"""
            # (File listing logic - displays "today's" files)
            try:
                if not DOWNLOADS_DIR.exists() or not DOWNLOADS_DIR.is_dir(): html_page_content += f"<p class='status-message error-message'>Error: Downloads directory not found.</p>"
                else:
                    all_items_in_dir = os.listdir(DOWNLOADS_DIR)
                    files_to_consider_for_display = [] 
                    for item_name in all_items_in_dir:
                        item_path = DOWNLOADS_DIR / item_name
                        if item_path.is_file():
                            _, file_extension = os.path.splitext(item_name)
                            if file_extension.lower() in ALLOWED_EXTENSIONS:
                                try:
                                    mtime_timestamp = os.path.getmtime(item_path)
                                    mtime_date = datetime.fromtimestamp(mtime_timestamp).date()
                                    if mtime_date == todays_date_for_display: 
                                        files_to_consider_for_display.append((mtime_timestamp, item_name))
                                except Exception: pass
                    if not files_to_consider_for_display: html_page_content += f"<p class='status-message empty-message'>No files matching display criteria (modified today) found.</p>"
                    else:
                        sorted_files_by_time = sorted(files_to_consider_for_display, key=lambda x: x[0], reverse=True)
                        html_page_content += "<ul>"
                        for mtime_timestamp, file_name in sorted_files_by_time: 
                            file_ext = os.path.splitext(file_name)[1].lower()
                            file_name_url_encoded = quote(file_name)
                            preview_content_html = ""
                            if file_ext in ['.jpg', '.jpeg', '.png']: preview_content_html = f"<img class='preview-image' src='/preview_file/{file_name_url_encoded}' alt='Preview of {html.escape(file_name)}'>"
                            elif file_ext == '.pdf': preview_content_html = f"<iframe class='preview-pdf-iframe' src='/preview_file/{file_name_url_encoded}#toolbar=0&navpanes=0&scrollbar=0' title='Preview of {html.escape(file_name)}'></iframe>"
                            else: 
                                icon_data_url = SVG_ICONS.get(file_ext, SVG_ICONS["default_file"])
                                preview_content_html = f"<img class='preview-svg-icon' src='{icon_data_url}' alt='{file_ext} icon'>"
                            html_page_content += f"<li><div class='preview-container'>{preview_content_html}</div><span class='filename'>{html.escape(file_name)}</span></li>"
                        html_page_content += "</ul>"
            except Exception as e: html_page_content += f"<p class='status-message error-message'>An error occurred listing files: {html.escape(str(e))}</p>"
            html_page_content += "</body></html>"
            self.wfile.write(html_page_content.encode('utf-8'))
        else:
            self.send_error(404, "Resource not found.")

def run_server():
    global DOWNLOADS_DIR # Ensure it's global if modified
    DOWNLOADS_DIR = DOWNLOADS_DIR.resolve()
    print(f"Program started at: {PROGRAM_START_TIME.strftime('%Y-%m-%d %H:%M:%S')}")

    if ENABLE_AUTO_DELETION:
        print("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
        print("!!!      E X T R E M E   D A N G E R :   A U T O - D E L E T I O N       !!!")
        print("!!!                  F E A T U R E   I S   E N A B L E D                 !!!")
        print("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
        print(f"Files in: '{DOWNLOADS_DIR}'")
        print(f"Matching types: {', '.join(sorted(list(ALLOWED_EXTENSIONS)))}")
        print(f"Deletion system will activate {AUTO_DELETION_ACTIVATION_AND_FILE_AGE_MINUTES} minutes after program start.")
        print(f"Once active, it will delete files older than {AUTO_DELETION_ACTIVATION_AND_FILE_AGE_MINUTES} minute(s) from their modification time.")
        print("This is extremely fast and can lead to data loss if you are not careful.")
        print("To disable this, edit the script and set ENABLE_AUTO_DELETION = False.")
        print("YOU HAVE BEEN WARNED. PROCEED WITH EXTREME CAUTION.")
        print("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")

    with socketserver.TCPServer(("", PORT), DownloadsHandler) as httpd:
        print(f"\nPython HTTP server started.")
        if ENABLE_AUTO_DELETION:
            print(f"Automatic Deletion: ENABLED - EXTREMELY DANGEROUS (Activates in {AUTO_DELETION_ACTIVATION_AND_FILE_AGE_MINUTES}m, then deletes files older than {AUTO_DELETION_ACTIVATION_AND_FILE_AGE_MINUTES}m)")
        else:
            print("Automatic Deletion: DISABLED")
        print(f"Serving at: http://localhost:{PORT}")
        print(f"Listing from: {DOWNLOADS_DIR}")
        if not DOWNLOADS_DIR.exists(): print(f"WARNING: Downloads directory does not exist.")
        print("Press Ctrl+C to stop server.")
        try: httpd.serve_forever()
        except KeyboardInterrupt: print("\nServer stopped.")
        except Exception as e: print(f"Server error: {e}")

if __name__ == "__main__":
    if "YOUR_DOWNLOADS_FOLDER_PATH_HERE" in str(DOWNLOADS_DIR) and not (Path.home() / "Downloads").exists():
        print("ERROR: 'DOWNLOADS_DIR' path needs to be set correctly if auto-detection failed.")
    else:
        run_server()