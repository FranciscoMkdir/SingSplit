import os
import time
from flask import Flask, request, jsonify, send_file, url_for
from werkzeug.utils import secure_filename
import io
from pathlib import Path
import select
from shutil import rmtree
import subprocess as sp
import sys
from typing import Dict, Tuple, Optional, IO
from pathlib import Path
import glob

app = Flask(__name__)

UPLOAD_FOLDER = 'uploads'
OUTPUT_FOLDER = 'separated'
ALLOWED_EXTENSIONS = {'mp3', 'wav', 'ogg'}

app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['OUTPUT_FOLDER'] = OUTPUT_FOLDER

# Asegúrate de que las carpetas existan
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
os.makedirs(app.config['OUTPUT_FOLDER'], exist_ok=True)

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def find_files(in_path):
    if os.path.isfile(in_path):
        return [in_path]
    out = []
    for file in Path(in_path).iterdir():
        if file.suffix.lower().lstrip(".") in ALLOWED_EXTENSIONS:
            out.append(str(file))
    return out

def copy_process_streams(process: sp.Popen):
    def raw(stream: Optional[IO[bytes]]) -> IO[bytes]:
        assert stream is not None
        if isinstance(stream, io.BufferedIOBase):
            stream = stream.raw
        return stream
    
    p_stdout, p_stderr = raw(process.stdout), raw(process.stderr)
    stream_by_fd: Dict[int, Tuple[IO[bytes], IO[str]]] = {
        p_stdout.fileno(): (p_stdout, sys.stdout),
        p_stderr.fileno(): (p_stderr, sys.stderr),
    }
    
    fds = list(stream_by_fd.keys())
    while fds:
        ready, _, _ = select.select(fds, [], [])
        for fd in ready:
            p_stream, std = stream_by_fd[fd]
            raw_buf = p_stream.read(2 ** 16)
            if not raw_buf:
                fds.remove(fd)
                continue
            buf = raw_buf.decode()
            std.write(buf)
            std.flush()

def separate(inp, outp, model="htdemucs", mp3=True, mp3_rate=256, float32=False, int24=False, two_stems=None):

    # Asegúrate de que las rutas sean absolutas
    in_path = os.path.abspath(inp)
    out_path = os.path.abspath(outp)
    

    cmd = ["python3", "-m", "demucs.separate", "-o", out_path, "-n", model]
    if mp3:
        cmd += ["--mp3", f"--mp3-bitrate={mp3_rate}"]
    if float32:
        cmd += ["--float32"]
    if int24:
        cmd += ["--int24"]
    if two_stems is not None:
        cmd += [f"--two-stems={two_stems}"]
    
    files = [str(f) for f in find_files(in_path)]
    if not files:
        return "No valid audio files found"

    
    p = sp.Popen(cmd + files, stdout=sp.PIPE, stderr=sp.PIPE)
    copy_process_streams(p)
    p.wait()
    
    if p.returncode != 0:
        return "Command failed, something went wrong."
    
    return "Separation completed successfully"


@app.route('/upload', methods=['POST'])
def upload_file():
    if 'file' not in request.files:
        return jsonify({"error": "No file part"}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({"error": "No selected file"}), 400
    if file and allowed_file(file.filename):
        filename = secure_filename(file.filename)
        file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        file.save(file_path)
        return jsonify({"message": "File uploaded successfully", "filename": filename}), 200
    return jsonify({"error": "File type not allowed"}), 400


@app.route('/download/<filename>', methods=['GET'])
def download_file(filename):
    base_path = os.path.join(app.config['OUTPUT_FOLDER'], 'htdemucs', filename)
    if not os.path.exists(base_path):
        return jsonify({"error": "File not found"}), 404
    
    songs = []
    download_urls = []
    
    for song_file in os.listdir(base_path):
        if song_file.endswith(('.mp3', '.wav', '.ogg')):
            songs.append(song_file)
            download_url = url_for('download_song', filename=filename, song=song_file, _external=True)
            download_urls.append(download_url)
    
    return jsonify({
        "songs": songs,
        "download_urls": download_urls
    }), 200
    
@app.route('/download/<filename>/<song>', methods=['GET'])
def download_song(filename, song):
    file_path = os.path.join(app.config['OUTPUT_FOLDER'], 'htdemucs', filename, song)
    if not os.path.exists(file_path):
        return jsonify({"error": "File not found"}), 404
    return send_file(file_path, as_attachment=True)
    
    
    
@app.route('/separate', methods=['POST'])
def api_separate():
    data = request.json
    filename = data.get('filename')
    separation_type = data.get('separation_type', 'full')
    if not filename:
        return jsonify({"error": "No filename provided"}), 400
    
    in_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    out_path = app.config['OUTPUT_FOLDER']
    
    if not os.path.isfile(in_path):
        return jsonify({"error": f"File not found: {filename}"}), 404
    
    if separation_type == 'vocals':
        result = separate(in_path, out_path, two_stems="vocals")
    else:
        result = separate(in_path, out_path)
    
    if result != "Separation completed successfully":
        return jsonify({"error": result}), 500
    
    # Wait for a short time to ensure files are written
    time.sleep(2)
    
    # Get download information
    base_path = os.path.join(app.config['OUTPUT_FOLDER'], 'htdemucs', filename.rsplit('.', 1)[0])
    if not os.path.exists(base_path):
        return jsonify({"error": "Separated files not found"}), 404
    
    songs = []
    download_urls = []
    
    for song_file in os.listdir(base_path):
        if song_file.endswith(('.mp3', '.wav', '.ogg')):
            songs.append(song_file)
            download_url = url_for('download_song', filename=filename.rsplit('.', 1)[0], song=song_file, _external=True)
            download_urls.append(download_url)
    
    return jsonify({
        "message": result,
        "songs": songs,
        "download_urls": download_urls
    }), 200


@app.route('/delete/<filename>', methods=['DELETE'])
def delete_song(filename):
    # Rutas de los archivos a eliminar
    htdemucs_path = os.path.join('separated', 'htdemucs', filename)
    uploads_path_pattern = os.path.join('uploads', f"{filename}.*")
    
    # Verificar si los archivos existen y eliminarlos
    files_deleted = []
    
    # Borra directorio
    if os.path.exists(htdemucs_path):
        rmtree(htdemucs_path)
        files_deleted.append(htdemucs_path)
    
    # Buscar archivos que coincidan con el patrón en uploads
    matching_files = glob.glob(uploads_path_pattern)
    for file_path in matching_files:
        os.unlink(file_path)
        files_deleted.append(file_path)
    
    # Preparar la respuesta
    if files_deleted:
        return jsonify({
            'message': 'Archivos eliminados exitosamente',
            'deleted_files': files_deleted
        }), 200
    else:
        return jsonify({
            'message': 'No se encontraron archivos para eliminar',
            'filename': filename
        }), 404

    

if __name__ == '__main__':
    app.run()
