# app.py  (FastAPI)
"""
Local web UI for the CV transformation demo.

Run:
    pip install fastapi uvicorn python-multipart python-dotenv pdfplumber cohere lxml
    python app.py
Open:
    http://127.0.0.1:8000        (API docs: http://127.0.0.1:8000/docs)

Put your key in a file named .env next to this file:
    api_key_cohere="your-key"
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
from pydantic import BaseModel

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


class LayoutItem(BaseModel):
    index: int
    column: str


class LayoutBody(BaseModel):
    layout: list[LayoutItem]


def _set(job_id, **kw):
    with LOCK:
        JOBS[job_id].update(kw)


def _error(message, status=400):
    return JSONResponse({"error": message}, status_code=status)


# ---------------------------------------------------------------- pipeline --
def build_outputs(job_id: str, layout=None):
    """Render cv.json with the given layout. Returns (sections_for_ui, has_pdf)."""
    folder = JOBS_DIR / job_id
    cv = json.loads((folder / "cv.json").read_text(encoding="utf-8"))
    sections = [s for s in (cv.get("sections") or []) if str(s.get("title") or "").strip()]
    if layout is None:
        layout = list(enumerate(plan_columns(sections)))

    _set(job_id, step="build")
    docx_path = folder / "cv_new.docx"
    render_cv(cv, str(TEMPLATE), str(docx_path), layout=layout)

    _set(job_id, step="pdf")
    stale = folder / "cv_new.pdf"
    if stale.exists():
        stale.unlink()
    pdf_out = to_pdf(str(docx_path))
    has_pdf = bool(pdf_out and Path(pdf_out).exists())

    ui = [
        {"index": i, "title": sections[i]["title"], "items": len(sections[i].get("items") or []), "column": c}
        for i, c in layout
    ]
    return ui, has_pdf


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

        ui, has_pdf = build_outputs(job_id)
        result = {
            "name": cv.get("name"),
            "job_title": cv.get("job_title"),
            "stem": stem,
            "has_pdf": has_pdf,
            "pages_in": pdf["num_pages"],
            "method": pdf["method"],
            "seconds": round(time.time() - started, 1),
            "version": 1,
            "sections": ui,
        }
        _set(job_id, status="done", step="done", result=result)
    except Exception as e:  # show the real reason in the UI
        traceback.print_exc()
        _set(job_id, status="error", error=f"{type(e).__name__}: {e}")


def rebuild_job(job_id: str, layout: list):
    """Re-render with the layout chosen by the user."""
    try:
        ui, has_pdf = build_outputs(job_id, layout)
        with LOCK:
            job = JOBS[job_id]
            result = job["result"]
            result.update(sections=ui, has_pdf=has_pdf, version=result["version"] + 1)
            job.update(status="done", step="done", rebuild_error=None)
    except Exception as e:
        traceback.print_exc()
        _set(job_id, status="done", step="done", rebuild_error=f"{type(e).__name__}: {e}")


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


@app.post("/api/job/{job_id}/render")
def rerender(job_id: str, body: LayoutBody, background: BackgroundTasks):
    """Rebuild the CV with the section order / sidebar choice made by the user."""
    if not ID_RE.match(job_id) or job_id not in JOBS:
        return _error("Job not found", 404)
    with LOCK:
        job = JOBS[job_id]
        if job.get("status") != "done":
            return _error("The CV is not ready yet.", 409)
        n = len(job["result"]["sections"])
        indexes = sorted(x.index for x in body.layout)
        if indexes != list(range(n)) or any(x.column not in ("sidebar", "main") for x in body.layout):
            return _error("Invalid layout.")
        job.update(status="running", step="build")  # set now so the page waits for the new result
    layout = [(x.index, x.column) for x in body.layout]
    background.add_task(rebuild_job, job_id, layout)
    return {"job_id": job_id}


@app.get("/api/job/{job_id}")
def job(job_id: str):
    if not ID_RE.match(job_id) or job_id not in JOBS:
        return _error("Job not found", 404)
    with LOCK:
        return dict(JOBS[job_id])


@app.get("/api/job/{job_id}/file/{kind}")
def job_file(job_id: str, kind: str, download: int = 0, v: int = 0):
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
        headers={"Cache-Control": "no-store"},
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
  button,.btn{background:var(--navy);color:#fff;border:0;border-radius:9px;padding:12px 22px;font-size:15px;font-weight:600;cursor:pointer;text-decoration:none;display:inline-block}
  button:hover,.btn:hover{background:var(--navy2)}
  button:disabled{background:#b4bbd3;cursor:not-allowed}
  .btn.green{background:var(--green)} .btn.green:hover{background:#4c8f2e}
  .btn.ghost{background:#fff;color:var(--navy);border:1px solid var(--navy)} .btn.ghost:hover{background:#eef1fa}
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
  .actions{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
  .panes{display:grid;grid-template-columns:1fr 1fr;gap:20px}
  .pane h3{margin:0 0 10px;font-size:14px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted)}
  iframe{width:100%;height:780px;border:1px solid var(--line);border-radius:10px;background:#fff}
  .nopdf{padding:30px;text-align:center;color:var(--muted);border:1px dashed var(--line);border-radius:10px}
  /* layout editor */
  .edhead{display:flex;justify-content:space-between;gap:16px;flex-wrap:wrap;align-items:flex-start}
  .edhead h3{margin:0;color:var(--navy)}
  .cols{display:grid;grid-template-columns:1fr 1.4fr;gap:18px;margin-top:16px}
  .col h4{margin:0 0 8px;font-size:13px;text-transform:uppercase;letter-spacing:.06em}
  .col.side h4{color:var(--navy)} .col.main h4{color:#2f7a14}
  ul.dz{list-style:none;margin:0;padding:10px;min-height:90px;border-radius:12px;border:2px dashed var(--line);transition:.15s}
  .col.side ul.dz{background:#f0f3ff;border-color:#c5cdf0} .col.main ul.dz{background:#f4faf0;border-color:#cde5bf}
  ul.dz li{display:flex;align-items:center;gap:10px;background:#fff;border:1px solid var(--line);border-radius:9px;padding:9px 10px;margin-bottom:8px;cursor:grab;user-select:none}
  ul.dz li.dragging{opacity:.4}
  .grip{color:#9aa4c4;font-size:16px;letter-spacing:-3px}
  .t{flex:1;font-weight:600;color:var(--navy2);font-size:14px}
  .n{color:var(--muted);font-size:12px;white-space:nowrap}
  .ctl{display:flex;gap:4px}
  .mini{padding:3px 9px;font-size:13px;border-radius:6px;background:#eef1fa;color:var(--navy)} .mini:hover{background:#dde3f7}
  .hint{color:#9aa4c4;font-size:13px;text-align:center;padding:6px}
  #applyMsg{font-size:13px;color:var(--muted)} #applyMsg.bad{color:#a1281c}
  @media(max-width:900px){.panes,.cols{grid-template-columns:1fr}}
</style>
</head>
<body>
<header>
  <img src="/assets/dtc_logo.jpeg" alt="DTC">
  <div><h1>AI-Powered CV Transformation</h1><p>Upload any CV. Get it back in the company template, with the original sections and titles.</p></div>
</header>
<main>
  <section class="card" id="uploadCard">
    <div id="drop"><strong>Drop the original CV (PDF) here</strong>or click to choose a file
      <input type="file" id="file" accept="application/pdf,.pdf" hidden></div>
    <div id="fileinfo"></div>
    <div class="row"><button id="go" disabled>Transform CV</button></div>
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
      <div class="edhead">
        <div>
          <h3>Customize the layout</h3>
          <div class="meta">Choose which sections go in the <b>left sidebar</b> and set their order. Drag a section, or use the arrows. Titles are never changed.</div>
        </div>
        <div class="actions">
          <span id="applyMsg"></span>
          <button class="btn ghost" id="reset">Reset</button>
          <button id="apply">Apply layout</button>
        </div>
      </div>
      <div class="cols">
        <div class="col side"><h4>Left sidebar</h4><ul class="dz" id="ulSide"></ul></div>
        <div class="col main"><h4>Main column</h4><ul class="dz" id="ulMain"></ul></div>
      </div>
    </section>

    <section class="card">
      <div class="panes">
        <div class="pane"><h3>Original</h3><iframe id="frOrig"></iframe></div>
        <div class="pane"><h3>New CV</h3><iframe id="frNew" hidden></iframe>
          <div class="nopdf" id="noPdf" hidden>PDF preview is not available on this computer.<br>Download the Word file to see the result.<br><small>(Install Microsoft Word + <code>pip install docx2pdf</code> to enable the preview.)</small></div></div>
      </div>
    </section>
  </div>
</main>

<script>
const $ = s => document.querySelector(s);
const esc = t => String(t).replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
let chosen = null, ready = true, timer = null, currentId = null, defaultSections = [];
const ORDER = ["read","parse","build","pdf"];

fetch("/api/status").then(r=>r.json()).then(s=>{
  if(!s.has_key){ ready = false; showErr('Cohere API key not found. Add  api_key_cohere="..."  to the .env file and restart the server.'); }
  if(!s.has_template) showErr("Template file is missing: templates/DTC_CV_Template.docx");
  refresh();
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
  poll(j.job_id, true);
};

function mark(step){
  const i = ORDER.indexOf(step);
  document.querySelectorAll(".step").forEach(el=>{
    const k = ORDER.indexOf(el.dataset.s);
    el.className = "step" + (step==="done" || k < i ? " done" : k===i ? " active" : "");
    el.querySelector(".dot").textContent = el.className.includes("done") ? "✓" : k+1;
  });
}

function poll(id, initial){
  clearInterval(timer);
  timer = setInterval(async () => {
    const j = await (await fetch("/api/job/"+id)).json();
    if(j.status==="running"){ if(initial) mark(j.step); return; }
    clearInterval(timer);
    if(j.status==="error"){
      if(initial){ $("#progressCard").hidden = true; showErr(j.error); refresh(); } else applyDone(j.error);
    } else if(initial){ mark("done"); show(id, j.result); }
    else applyDone(j.rebuild_error, j.result);
  }, 700);
}

function setPreview(id, r){
  const pdf = r.has_pdf;
  $("#dlPdf").hidden = !pdf; $("#frNew").hidden = !pdf; $("#noPdf").hidden = pdf;
  if(pdf){ $("#dlPdf").href = `/api/job/${id}/file/pdf?download=1`; $("#frNew").src = `/api/job/${id}/file/pdf?v=${r.version}`; }
  $("#dlDocx").href = `/api/job/${id}/file/docx?download=1`;
}

function show(id, r){
  currentId = id;
  $("#rName").textContent = r.name || r.stem;
  $("#rTitle").textContent = r.job_title || "";
  $("#rMeta").textContent = `${r.pages_in} page(s) read via ${r.method} · ${r.sections.length} sections · ${r.seconds}s`;
  $("#frOrig").src = `/api/job/${id}/file/original`;
  setPreview(id, r);
  defaultSections = JSON.parse(JSON.stringify(r.sections));
  buildEditor(defaultSections);
  $("#applyMsg").textContent = ""; $("#apply").disabled = false;
  $("#result").hidden = false; $("#result").scrollIntoView({behavior:"smooth"});
}

/* ---------- layout editor (the DOM is the state) ---------- */
const ulSide = $("#ulSide"), ulMain = $("#ulMain");
let dragEl = null;

function liHtml(s){
  return `<li draggable="true" data-i="${s.index}">
    <span class="grip">⋮⋮</span><span class="t">${esc(s.title)}</span><span class="n">${s.items} item${s.items===1?"":"s"}</span>
    <span class="ctl"><button class="mini" data-a="up" title="Move up">↑</button><button class="mini" data-a="down" title="Move down">↓</button><button class="mini" data-a="move" title="Move to the other column">⇄</button></span></li>`;
}
function buildEditor(sections){
  ulSide.innerHTML = sections.filter(s=>s.column==="sidebar").map(liHtml).join("");
  ulMain.innerHTML = sections.filter(s=>s.column==="main").map(liHtml).join("");
}

[ulSide, ulMain].forEach(ul => {
  ul.addEventListener("dragstart", e => { dragEl = e.target.closest("li"); if(dragEl){ dragEl.classList.add("dragging"); e.dataTransfer.effectAllowed = "move"; e.dataTransfer.setData("text/plain", ""); } });
  ul.addEventListener("dragend", () => { if(dragEl) dragEl.classList.remove("dragging"); dragEl = null; });
  ul.addEventListener("dragover", e => {
    if(!dragEl) return; e.preventDefault();
    const others = [...ul.querySelectorAll("li:not(.dragging)")];
    const next = others.find(el => { const b = el.getBoundingClientRect(); return e.clientY < b.top + b.height/2; });
    if(next) ul.insertBefore(dragEl, next); else ul.appendChild(dragEl);
  });
  ul.addEventListener("click", e => {
    const b = e.target.closest("button[data-a]"); if(!b) return;
    const li = b.closest("li"), a = b.dataset.a;
    if(a==="up" && li.previousElementSibling) ul.insertBefore(li, li.previousElementSibling);
    if(a==="down" && li.nextElementSibling) ul.insertBefore(li.nextElementSibling, li);
    if(a==="move") (ul===ulSide ? ulMain : ulSide).appendChild(li);
  });
});

function currentLayout(){
  const pick = (ul, column) => [...ul.querySelectorAll("li")].map(li => ({index: +li.dataset.i, column}));
  return [...pick(ulSide, "sidebar"), ...pick(ulMain, "main")];
}

$("#reset").onclick = () => { buildEditor(defaultSections); $("#applyMsg").textContent = "Reset. Click Apply layout to rebuild."; $("#applyMsg").className = ""; };

$("#apply").onclick = async () => {
  $("#apply").disabled = true; $("#applyMsg").className = ""; $("#applyMsg").textContent = "Rebuilding…";
  const r = await fetch(`/api/job/${currentId}/render`, {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify({layout: currentLayout()})});
  const j = await r.json();
  if(!r.ok){ applyDone(j.error || "Could not apply the layout."); return; }
  poll(currentId, false);
};

function applyDone(err, r){
  $("#apply").disabled = false;
  if(err){ $("#applyMsg").className = "bad"; $("#applyMsg").textContent = err; return; }
  $("#applyMsg").className = ""; $("#applyMsg").textContent = "Layout applied ✓";
  if(r) setPreview(currentId, r);
}

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