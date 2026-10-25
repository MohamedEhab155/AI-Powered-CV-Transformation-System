# app.py  (FastAPI)
"""
Local web UI for the CV transformation demo.

Run:
    pip install fastapi uvicorn python-multipart python-dotenv pdfplumber cohere lxml

Put your key in a file named .env next to this file:
    api_key_cohere="your-key"
    python app.py
Open:
    http://127.0.0.1:8000        (API docs: http://127.0.0.1:8000/docs)
"""
import json
import os
import re
import threading
import time
import traceback
import uuid
from pathlib import Path

from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, File, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from CV_Parser import parse_cv
from PDF_Reader import read_pdf

# Your renderer file may be named renderer.py or Render.py
try:
    from Render import plan_columns, render_cv, to_pdf
except ImportError:
    from Render import plan_columns, render_cv, to_pdf

BASE = Path(__file__).resolve().parent
TEMPLATE = BASE / "templates" / "DTC_CV_Template.docx"
ASSETS = BASE / "assets"
JOBS_DIR = BASE / "jobs"
JOBS_DIR.mkdir(exist_ok=True)
MAX_UPLOAD = 20 * 1024 * 1024  # 20 MB

load_dotenv(BASE / ".env")
API_KEY = (os.getenv("api_key_cohere") or os.getenv("COHERE_API_KEY") or "").strip()

app = FastAPI(title="CV Transformation")

JOBS = {}
LOCK = threading.Lock()
ID_RE = re.compile(r"^[0-9a-f]{32}$")


def _set(job_id, **kw):
    with LOCK:
        JOBS[job_id].update(kw)


def _error(message, status=400):
    return JSONResponse({"error": message}, status_code=status)


# ---------------------------------------------------------------- pipeline --
def run_job(job_id: str, pdf_path: Path, stem: str, api_key: str):
    folder = pdf_path.parent
    started = time.time()
    try:
        _set(job_id, step="read")
        pdf = read_pdf(str(pdf_path))
        if not pdf["text"]:
            raise RuntimeError("No text could be read from this PDF.")

        _set(job_id, step="parse")
        cv = parse_cv(pdf["text"], api_key=api_key)
        (folder / "cv.json").write_text(json.dumps(cv, ensure_ascii=False, indent=2), encoding="utf-8")

        _set(job_id, step="build")
        docx_path = folder / "cv_new.docx"
        render_cv(cv, str(TEMPLATE), str(docx_path))

        _set(job_id, step="pdf")
        pdf_out = to_pdf(str(docx_path))

        sections = [s for s in (cv.get("sections") or []) if s.get("title")]
        columns = plan_columns(sections)
        result = {
            "name": cv.get("name"),
            "job_title": cv.get("job_title"),
            "stem": stem,
            "has_pdf": bool(pdf_out and Path(pdf_out).exists()),
            "pages_in": pdf["num_pages"],
            "method": pdf["method"],
            "seconds": round(time.time() - started, 1),
            "sections": [
                {"title": s["title"], "items": len(s.get("items") or []), "column": c}
                for s, c in zip(sections, columns)
            ],
        }
        _set(job_id, status="done", step="done", result=result)
    except Exception as e:  # show the real reason in the UI
        traceback.print_exc()
        _set(job_id, status="error", error=f"{type(e).__name__}: {e}")


# ------------------------------------------------------------------ routes --
@app.get("/", response_class=HTMLResponse)
def index():
    return PAGE


@app.get("/assets/{name}")
def assets(name: str):
    path = (ASSETS / name).resolve()
    if ASSETS.resolve() not in path.parents or not path.is_file():
        return _error("Not found", 404)
    return FileResponse(path)


@app.get("/api/status")
def status():
    return {"has_key": bool(API_KEY), "has_template": TEMPLATE.exists()}


@app.post("/api/convert")
async def convert(background: BackgroundTasks, file: UploadFile = File(...)):
    if not (file.filename or "").lower().endswith(".pdf"):
        return _error("Please upload a PDF file.")
    if not TEMPLATE.exists():
        return _error(f"Template not found: {TEMPLATE}", 500)
    if not API_KEY:
        return _error('Cohere API key not found. Add  api_key_cohere="..."  to the .env file and restart.', 500)

    data = await file.read()
    if len(data) > MAX_UPLOAD:
        return _error("File is too large (max 20 MB).", 413)

    job_id = uuid.uuid4().hex
    folder = JOBS_DIR / job_id
    folder.mkdir()
    pdf_path = folder / "original.pdf"
    pdf_path.write_bytes(data)
    stem = re.sub(r"[^\w\- ]", "", Path(file.filename).stem).strip() or "cv"

    with LOCK:
        JOBS[job_id] = {"status": "running", "step": "read"}
    background.add_task(run_job, job_id, pdf_path, stem, API_KEY)  # runs in a worker thread
    return {"job_id": job_id}


@app.get("/api/job/{job_id}")
def job(job_id: str):
    if not ID_RE.match(job_id) or job_id not in JOBS:
        return _error("Job not found", 404)
    with LOCK:
        return dict(JOBS[job_id])


@app.get("/api/job/{job_id}/file/{kind}")
def job_file(job_id: str, kind: str, download: int = 0):
    if not ID_RE.match(job_id) or job_id not in JOBS:
        return _error("Job not found", 404)
    files = {
        "original": ("original.pdf", "application/pdf", "pdf"),
        "docx": ("cv_new.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "docx"),
        "pdf": ("cv_new.pdf", "application/pdf", "pdf"),
        "json": ("cv.json", "application/json", "json"),
    }
    if kind not in files:
        return _error("Unknown file", 404)
    name, mime, ext = files[kind]
    path = JOBS_DIR / job_id / name
    if not path.is_file():
        return _error("File not ready", 404)
    stem = (JOBS[job_id].get("result") or {}).get("stem", "cv")
    download_name = f"{stem}.pdf" if kind == "original" else f"{stem}_new.{ext}"
    return FileResponse(
        path,
        media_type=mime,
        filename=download_name,
        content_disposition_type="attachment" if download else "inline",
    )


# --------------------------------------------------------------------- UI --
PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>CV Transformation</title>
<style>
  :root{--navy:#1B2A6B;--navy2:#13204F;--green:#5FA83C;--bg:#f4f6fb;--card:#fff;--line:#e3e7f1;--text:#25304a;--muted:#6b7590}
  *{box-sizing:border-box}
  body{margin:0;font-family:"Segoe UI",Roboto,Arial,sans-serif;background:var(--bg);color:var(--text)}
  header{background:#fff;border-bottom:3px solid var(--green);padding:14px 32px;display:flex;align-items:center;gap:20px}
  header img{height:54px}
  header h1{margin:0;font-size:20px;color:var(--navy)}
  header p{margin:2px 0 0;color:var(--muted);font-size:14px}
  main{max-width:1200px;margin:28px auto;padding:0 20px}
  .card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:24px;margin-bottom:20px;box-shadow:0 2px 10px rgba(27,42,107,.05)}
  #drop{border:2px dashed #b9c2de;border-radius:12px;padding:44px 20px;text-align:center;cursor:pointer;transition:.2s;color:var(--muted)}
  #drop:hover,#drop.over{border-color:var(--green);background:#f3faee}
  #drop strong{color:var(--navy);display:block;font-size:18px;margin-bottom:6px}
  #fileinfo{margin-top:14px;font-size:15px;color:var(--navy2);font-weight:600}
  .row{display:flex;gap:14px;align-items:end;margin-top:18px;flex-wrap:wrap}
  label{font-size:13px;color:var(--muted);display:block;margin-bottom:4px}
  input[type=password]{padding:10px 12px;border:1px solid var(--line);border-radius:8px;width:340px;font-size:14px}
  button,.btn{background:var(--navy);color:#fff;border:0;border-radius:9px;padding:12px 22px;font-size:15px;font-weight:600;cursor:pointer;text-decoration:none;display:inline-block}
  button:hover,.btn:hover{background:var(--navy2)}
  button:disabled{background:#b4bbd3;cursor:not-allowed}
  .btn.green{background:var(--green)} .btn.green:hover{background:#4c8f2e}
  .btn.ghost{background:#fff;color:var(--navy);border:1px solid var(--navy)}
  .steps{display:flex;gap:0;flex-wrap:wrap}
  .step{flex:1;min-width:170px;padding:14px;border-radius:10px;background:#f4f6fb;margin:4px;display:flex;gap:10px;align-items:center;color:var(--muted);font-weight:600;font-size:14px}
  .dot{width:22px;height:22px;border-radius:50%;background:#d5daea;flex:none;display:grid;place-items:center;color:#fff;font-size:13px}
  .step.active{background:#eaf0ff;color:var(--navy)} .step.active .dot{background:var(--navy);animation:pulse 1s infinite}
  .step.done{color:#2f7a14} .step.done .dot{background:var(--green)}
  @keyframes pulse{50%{transform:scale(1.25)}}
  .err{background:#fff1f0;border:1px solid #ffc9c4;color:#a1281c;padding:14px 16px;border-radius:10px;margin-top:14px;white-space:pre-wrap}
  .summary{display:flex;justify-content:space-between;gap:20px;flex-wrap:wrap;align-items:center}
  .summary h2{margin:0;color:var(--navy);font-size:22px}
  .summary .sub{color:var(--green);font-weight:600;margin-top:2px}
  .meta{color:var(--muted);font-size:13px;margin-top:6px}
  .actions{display:flex;gap:10px;flex-wrap:wrap}
  .panes{display:grid;grid-template-columns:1fr 1fr;gap:20px}
  .pane h3{margin:0 0 10px;font-size:14px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted)}
  iframe{width:100%;height:780px;border:1px solid var(--line);border-radius:10px;background:#fff}
  table{width:100%;border-collapse:collapse;font-size:14px;margin-top:6px}
  th{text-align:left;color:var(--muted);font-weight:600;padding:8px 10px;border-bottom:1px solid var(--line)}
  td{padding:9px 10px;border-bottom:1px solid #f0f2f8}
  .badge{padding:3px 10px;border-radius:20px;font-size:12px;font-weight:700}
  .badge.sidebar{background:#e3e8ff;color:var(--navy)} .badge.main{background:#e6f4de;color:#2f7a14}
  .nopdf{padding:30px;text-align:center;color:var(--muted);border:1px dashed var(--line);border-radius:10px}
  @media(max-width:900px){.panes{grid-template-columns:1fr}}
</style>
</head>
<body>
<header>
  <img src="/assets/logo.jpeg" alt="DTC">
  <div><h1>AI-Powered CV Transformation</h1><p>Upload any CV. Get it back in the company template, with the original sections and titles.</p></div>
</header>
<main>
  <section class="card" id="uploadCard">
    <div id="drop"><strong>Drop the original CV (PDF) here</strong>or click to choose a file
      <input type="file" id="file" accept="application/pdf,.pdf" hidden></div>
    <div id="fileinfo"></div>
    <div class="row">
      <button id="go" disabled>Transform CV</button>
    </div>
    <div id="err" class="err" hidden></div>
  </section>

  <section class="card" id="progressCard" hidden>
    <div class="steps" id="steps">
      <div class="step" data-s="read"><span class="dot">1</span>Reading PDF</div>
      <div class="step" data-s="parse"><span class="dot">2</span>Understanding sections (AI)</div>
      <div class="step" data-s="build"><span class="dot">3</span>Applying template</div>
      <div class="step" data-s="pdf"><span class="dot">4</span>Preparing preview</div>
    </div>
  </section>

  <div id="result" hidden>
    <section class="card summary">
      <div>
        <h2 id="rName"></h2><div class="sub" id="rTitle"></div>
        <div class="meta" id="rMeta"></div>
      </div>
      <div class="actions">
        <a class="btn green" id="dlDocx">Download Word (.docx)</a>
        <a class="btn" id="dlPdf" hidden>Download PDF</a>
        <button class="btn ghost" id="again">Convert another</button>
      </div>
    </section>
    <section class="card">
      <div class="panes">
        <div class="pane"><h3>Original</h3><iframe id="frOrig"></iframe></div>
        <div class="pane"><h3>New CV</h3><iframe id="frNew" hidden></iframe>
          <div class="nopdf" id="noPdf" hidden>PDF preview is not available on this computer.<br>Download the Word file to see the result.<br><small>(Install Microsoft Word + <code>pip install docx2pdf</code> to enable the preview.)</small></div></div>
      </div>
    </section>
    <section class="card">
      <h3 style="margin:0 0 4px;color:var(--navy)">Sections detected in the original CV</h3>
      <div class="meta">Titles are copied exactly from the original. Short lists (skills, personal info) go to the sidebar.</div>
      <table><thead><tr><th>Title (from original)</th><th>Items</th><th>Placed in</th></tr></thead><tbody id="secRows"></tbody></table>
    </section>
  </div>
</main>

<script>
const $ = s => document.querySelector(s);
let chosen = null, ready = true, timer = null;
const ORDER = ["read","parse","build","pdf"];

fetch("/api/status").then(r=>r.json()).then(s=>{
  if(!s.has_key){ ready = false; showErr('Cohere API key not found. Add  api_key_cohere="..."  to the .env file and restart the server.'); }
  if(!s.has_template) showErr("Template file is missing: templates/DTC_CV_Template.docx");
});

function showErr(m){ const e=$("#err"); e.textContent=m; e.hidden=!m; }
function refresh(){ $("#go").disabled = !chosen || !ready; }

const drop = $("#drop"), fileIn = $("#file");
drop.onclick = () => fileIn.click();
drop.ondragover = e => { e.preventDefault(); drop.classList.add("over"); };
drop.ondragleave = () => drop.classList.remove("over");
drop.ondrop = e => { e.preventDefault(); drop.classList.remove("over"); pick(e.dataTransfer.files[0]); };
fileIn.onchange = () => pick(fileIn.files[0]);

function pick(f){
  showErr("");
  if(!f) return;
  if(!f.name.toLowerCase().endsWith(".pdf")){ showErr("Please choose a PDF file."); return; }
  chosen = f; $("#fileinfo").textContent = f.name + "  (" + Math.round(f.size/1024) + " KB)"; refresh();
}

$("#go").onclick = async () => {
  showErr(""); $("#go").disabled = true;
  const fd = new FormData(); fd.append("file", chosen);
  const r = await fetch("/api/convert", {method:"POST", body:fd});
  const j = await r.json();
  if(!r.ok){ showErr(j.error || "Upload failed."); refresh(); return; }
  $("#progressCard").hidden = false; $("#result").hidden = true; mark("read");
  poll(j.job_id);
};

function mark(step){
  const i = ORDER.indexOf(step);
  document.querySelectorAll(".step").forEach(el=>{
    const k = ORDER.indexOf(el.dataset.s);
    el.className = "step" + (step==="done" || k < i ? " done" : k===i ? " active" : "");
    el.querySelector(".dot").textContent = el.className.includes("done") ? "✓" : k+1;
  });
}

function poll(id){
  clearInterval(timer);
  timer = setInterval(async () => {
    const j = await (await fetch("/api/job/"+id)).json();
    if(j.status==="running") mark(j.step);
    else if(j.status==="error"){ clearInterval(timer); $("#progressCard").hidden=true; showErr(j.error); refresh(); }
    else { clearInterval(timer); mark("done"); show(id, j.result); }
  }, 700);
}

function show(id, r){
  $("#rName").textContent = r.name || r.stem;
  $("#rTitle").textContent = r.job_title || "";
  $("#rMeta").textContent = `${r.pages_in} page(s) read via ${r.method} · ${r.sections.length} sections · ${r.seconds}s`;
  $("#dlDocx").href = `/api/job/${id}/file/docx?download=1`;
  $("#frOrig").src = `/api/job/${id}/file/original`;
  const pdf = r.has_pdf;
  $("#dlPdf").hidden = !pdf; $("#frNew").hidden = !pdf; $("#noPdf").hidden = pdf;
  if(pdf){ $("#dlPdf").href = `/api/job/${id}/file/pdf?download=1`; $("#frNew").src = `/api/job/${id}/file/pdf`; }
  $("#secRows").innerHTML = r.sections.map(s =>
    `<tr><td>${esc(s.title)}</td><td>${s.items}</td><td><span class="badge ${s.column}">${s.column==="sidebar"?"Left sidebar":"Main column"}</span></td></tr>`).join("");
  $("#result").hidden = false; $("#result").scrollIntoView({behavior:"smooth"});
}
const esc = t => t.replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));

$("#again").onclick = () => {
  $("#result").hidden = true; $("#progressCard").hidden = true; chosen = null; fileIn.value = "";
  $("#fileinfo").textContent = ""; refresh(); window.scrollTo({top:0, behavior:"smooth"});
};
</script>
</body>
</html>
"""


if __name__ == "__main__":
    import uvicorn

    print("Open http://127.0.0.1:8000")
    uvicorn.run(app, host="127.0.0.1", port=8000)