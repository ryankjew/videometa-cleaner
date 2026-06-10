import os
import json
import uuid
import subprocess
import threading
import shutil
from pathlib import Path
from flask import Flask, request, jsonify, render_template, send_file, after_this_request

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024  # 500MB por arquivo

WORK_DIR = Path("/tmp/videometa")
WORK_DIR.mkdir(exist_ok=True)

jobs = {}

VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".flv", ".wmv", ".m4v", ".ts", ".mts", ".3gp"}

META_LABELS = {
    "title": "Título",
    "artist": "Artista",
    "author": "Autor",
    "album": "Álbum",
    "comment": "Comentário / ID rastreamento",
    "description": "Descrição",
    "copyright": "Copyright",
    "creation_time": "Data de criação",
    "date": "Data",
    "encoder": "Encoder / software",
    "handler_name": "Handler / câmera",
    "com.apple.quicktime.location.iso6709": "Localização GPS (Apple)",
    "location": "Localização GPS",
    "location-eng": "Localização GPS (eng)",
    "com.apple.quicktime.make": "Fabricante (Apple)",
    "com.apple.quicktime.model": "Modelo (Apple)",
    "com.apple.quicktime.software": "Software (Apple)",
    "com.apple.quicktime.author": "Autor (Apple)",
    "com.apple.quicktime.creationdate": "Data de criação (Apple)",
    "make": "Fabricante da câmera",
    "model": "Modelo da câmera",
    "software": "Software de edição",
    "device": "Dispositivo",
    "aigcinfo": "Info IA (TikTok)",
    "vidmd5": "Hash rastreamento (TikTok)",
    "keywords": "Palavras-chave",
    "major_brand": "Formato/Brand",
    "minor_version": "Versão do formato",
    "compatible_brands": "Formatos compatíveis",
}

def check_ffmpeg():
    try:
        r = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            return True, r.stdout.split("\n")[0]
    except:
        pass
    return False, None

def extract_metadata(filepath):
    try:
        cmd = ["ffprobe", "-v", "quiet", "-print_format", "json",
               "-show_format", "-show_streams", str(filepath)]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            return {}
        data = json.loads(r.stdout)
        all_tags = {}
        fmt = data.get("format", {})
        for k, v in fmt.get("tags", {}).items():
            all_tags[k.lower()] = {"value": str(v), "source": "container"}
        for i, stream in enumerate(data.get("streams", [])):
            codec_type = stream.get("codec_type", f"stream{i}")
            for k, v in stream.get("tags", {}).items():
                key = k.lower()
                if key not in all_tags:
                    all_tags[key] = {"value": str(v), "source": codec_type}
        return all_tags
    except:
        return {}

def get_duration(filepath):
    try:
        cmd = ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", str(filepath)]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        data = json.loads(r.stdout)
        return float(data.get("format", {}).get("duration", 0))
    except:
        return 0

def delete_job_files(job_id):
    """Delete all files for a job from disk."""
    job_dir = WORK_DIR / job_id
    if job_dir.exists():
        shutil.rmtree(str(job_dir), ignore_errors=True)

def process_video(job_id, file_id, input_path, output_path):
    job = jobs[job_id]
    fs = next(f for f in job["files"] if f["id"] == file_id)
    try:
        fs["status"] = "extracting"
        fs["progress"] = 5
        before = extract_metadata(input_path)
        fs["progress"] = 15

        output_path = Path(str(output_path).rsplit(".", 1)[0] + ".mp4")
        fs["output_path"] = str(output_path)

        cmd = [
            "ffmpeg", "-y", "-i", str(input_path),
            "-map_metadata", "-1",
            "-map_chapters", "-1",
            "-map", "0",
            "-c:v", "copy",
            "-c:a", "copy",
            "-movflags", "+faststart",
            str(output_path)
        ]

        fs["status"] = "processing"
        fs["progress"] = 20

        proc = subprocess.Popen(cmd, stderr=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1)
        duration = get_duration(input_path)
        stderr_lines = []

        for line in proc.stderr:
            line = line.strip()
            stderr_lines.append(line)
            if "time=" in line and duration > 0:
                try:
                    t_str = line.split("time=")[1].split(" ")[0].strip()
                    parts = t_str.split(":")
                    t = float(parts[0])*3600 + float(parts[1])*60 + float(parts[2])
                    fs["progress"] = min(90, 20 + int((t / duration) * 70))
                except:
                    pass

        proc.wait()

        # Delete input immediately after processing
        try:
            Path(input_path).unlink()
        except:
            pass

        if proc.returncode != 0:
            err = [l for l in stderr_lines if any(x in l.lower() for x in ["error","invalid","failed","cannot"])]
            raise RuntimeError("\n".join(err[-5:]) or "\n".join(stderr_lines[-8:]))

        fs["progress"] = 92
        after = extract_metadata(output_path)

        removed = [{"field": k, "label": META_LABELS.get(k, k), "value": v["value"][:200], "source": v["source"]} for k, v in before.items()]
        kept = [{"field": k, "label": META_LABELS.get(k, k), "value": v["value"][:200], "source": v["source"]} for k, v in after.items()]

        fs.update({
            "status": "done",
            "progress": 100,
            "removed": removed,
            "kept": kept,
            "removed_count": len(removed),
            "kept_count": len(kept),
            "in_size": fs.get("in_size", 0),
            "out_size": os.path.getsize(output_path),
            "output_filename": Path(output_path).name,
        })

    except Exception as e:
        fs["status"] = "error"
        fs["error"] = str(e)
        fs["progress"] = 0
        op = Path(fs.get("output_path", ""))
        if op.exists():
            op.unlink()

def run_job(job_id, max_workers=4):
    job = jobs[job_id]
    job["status"] = "running"
    sem = threading.Semaphore(max_workers)
    threads = []

    def worker(fst):
        with sem:
            process_video(job_id, fst["id"], Path(fst["input_path"]), Path(fst["output_path"]))

    for f in job["files"]:
        t = threading.Thread(target=worker, args=(f,), daemon=True)
        threads.append(t)
        t.start()
    for t in threads:
        t.join()

    job["status"] = "done"

    # Auto-delete all files after 10 minutes
    def cleanup():
        import time
        time.sleep(600)
        delete_job_files(job_id)
        if job_id in jobs:
            del jobs[job_id]

    threading.Thread(target=cleanup, daemon=True).start()


@app.route("/")
def index():
    ffmpeg_ok, ffmpeg_ver = check_ffmpeg()
    return render_template("index.html", ffmpeg_ok=ffmpeg_ok, ffmpeg_ver=ffmpeg_ver)

@app.route("/api/upload", methods=["POST"])
def api_upload():
    if "files" not in request.files:
        return jsonify({"error": "Nenhum arquivo enviado"}), 400

    job_id = str(uuid.uuid4())[:8]
    job_dir = WORK_DIR / job_id
    job_dir.mkdir(parents=True)

    file_states = []
    for f in request.files.getlist("files"):
        if not f.filename:
            continue
        ext = Path(f.filename).suffix.lower()
        if ext not in VIDEO_EXTS:
            continue

        fid = str(uuid.uuid4())[:8]
        safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in f.filename)
        input_path = job_dir / f"in_{fid}_{safe}"
        output_path = job_dir / f"out_{fid}.mp4"

        f.save(str(input_path))
        in_size = os.path.getsize(str(input_path))

        file_states.append({
            "id": fid,
            "original_name": f.filename,
            "input_path": str(input_path),
            "output_path": str(output_path),
            "status": "queued",
            "progress": 0,
            "removed": [], "kept": [],
            "removed_count": 0, "kept_count": 0,
            "in_size": in_size, "out_size": 0,
            "error": None,
        })

    if not file_states:
        shutil.rmtree(str(job_dir))
        return jsonify({"error": "Nenhum vídeo válido"}), 400

    jobs[job_id] = {"id": job_id, "status": "pending", "files": file_states}
    threading.Thread(target=run_job, args=(job_id,), daemon=True).start()
    return jsonify({"job_id": job_id, "file_count": len(file_states)})

@app.route("/api/job/<job_id>")
def api_job(job_id):
    if job_id not in jobs:
        return jsonify({"error": "Job não encontrado ou expirado"}), 404
    job = jobs[job_id]
    files_out = [{
        "id": f["id"], "original_name": f["original_name"],
        "status": f["status"], "progress": f["progress"],
        "removed": f.get("removed", []), "kept": f.get("kept", []),
        "removed_count": f.get("removed_count", 0), "kept_count": f.get("kept_count", 0),
        "in_size": f.get("in_size", 0), "out_size": f.get("out_size", 0),
        "error": f.get("error"), "output_filename": f.get("output_filename"),
    } for f in job["files"]]
    return jsonify({"id": job_id, "status": job["status"], "files": files_out})

@app.route("/api/download/<job_id>/<file_id>")
def api_download(job_id, file_id):
    if job_id not in jobs:
        return jsonify({"error": "Job não encontrado ou expirado"}), 404
    job = jobs[job_id]
    fs = next((f for f in job["files"] if f["id"] == file_id), None)
    if not fs or fs["status"] != "done":
        return jsonify({"error": "Arquivo não disponível"}), 404
    output_path = Path(fs["output_path"])
    if not output_path.exists():
        return jsonify({"error": "Arquivo não encontrado"}), 404

    download_name = Path(fs["original_name"]).stem + "_limpo.mp4"

    @after_this_request
    def delete_after_download(response):
        def remove():
            import time; time.sleep(2)
            try: output_path.unlink()
            except: pass
        threading.Thread(target=remove, daemon=True).start()
        return response

    return send_file(str(output_path), as_attachment=True,
                     download_name=download_name, mimetype="video/mp4")

@app.route("/api/download-all/<job_id>")
def api_download_all(job_id):
    if job_id not in jobs:
        return jsonify({"error": "Job não encontrado"}), 404
    job = jobs[job_id]
    links = [{"url": f"/api/download/{job_id}/{f['id']}",
               "name": Path(f["original_name"]).stem + "_limpo.mp4"}
             for f in job["files"] if f["status"] == "done"]
    return jsonify({"files": links})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    ffmpeg_ok, ver = check_ffmpeg()
    print(f"\n🎬 VideoMeta Cleaner — porta {port}")
    print(f"{'✅' if ffmpeg_ok else '❌'} FFmpeg: {ver or 'não encontrado'}\n")
    app.run(host="0.0.0.0", port=port, threaded=True)
