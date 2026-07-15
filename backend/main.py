"""
backend/main.py
FastAPI application — exposes endpoints for batch PDF upload,
async processing, and Excel download.

Endpoints
---------
POST /upload          — accept multiple PDF files, kick off extraction job
GET  /status/{job_id} — poll job progress
GET  /download/{job_id} — stream the finished Excel file
GET  /               — serves the frontend UI
"""

import asyncio
import logging
import os
import shutil
import tempfile
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from backend.excel_writer import write_excel
from backend.pdf_to_images import pdf_to_base64_images
from backend.watsonx_extractor import extract_from_pdf_images

# Load .env for local dev only — never override vars already set by the platform
_env_path = Path(__file__).parent.parent / ".env"
load_dotenv(dotenv_path=_env_path, override=False)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)


def _check_env() -> None:
    """Log env var presence at startup so misconfiguration is immediately visible."""
    for var in ("WATSONX_API_KEY", "WATSONX_PROJECT_ID", "WATSONX_MODEL_ID", "WATSONX_URL"):
        val = os.environ.get(var, "")
        masked = (val[:4] + "****") if len(val) > 4 else ("(not set)" if not val else "****")
        log.info("ENV %s = %s", var, masked)

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(title="Debit Memo Extractor", version="1.0.0")


@app.on_event("startup")
async def _startup() -> None:
    _check_env()


@app.get("/healthz", include_in_schema=False)
async def healthz():
    """Returns env var presence — helps confirm Railway vars are injected."""
    status = {}
    for var in ("WATSONX_API_KEY", "WATSONX_PROJECT_ID", "WATSONX_MODEL_ID", "WATSONX_URL"):
        val = os.environ.get(var, "")
        status[var] = (val[:4] + "****") if len(val) > 4 else ("(not set)" if not val else "****")
    return {"status": "ok", "env": status}

# CORS — allow all origins so the UI works whether opened via file:// or any
# dev server. The API only processes local files, so this is acceptable.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

# Serve the frontend at / so no cross-origin issues when opened via the API
_FRONTEND_DIR = Path(__file__).parent.parent / "frontend"
if _FRONTEND_DIR.is_dir():
    app.mount("/ui", StaticFiles(directory=str(_FRONTEND_DIR), html=True), name="frontend")

# ---------------------------------------------------------------------------
# In-memory job store  { job_id: { status, output_path, error } }
# ---------------------------------------------------------------------------
JOBS: dict[str, dict] = {}

# Shared thread pool — max 5 concurrent extraction jobs
_EXECUTOR = ThreadPoolExecutor(max_workers=5)

# Persistent dir for finished Excel files only (PDFs use a tempdir per job)
OUTPUT_DIR = Path(__file__).parent / "output"
OUTPUT_DIR.mkdir(exist_ok=True)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
class JobStatus(BaseModel):
    job_id: str
    status: Literal["pending", "processing", "done", "error"]
    total: int = 0
    processed: int = 0
    error: str | None = None


# ---------------------------------------------------------------------------
# Background processing (runs synchronously in a thread via FastAPI's
# run_in_threadpool so the event loop stays free)
# ---------------------------------------------------------------------------
def _process_job(job_id: str, pdf_paths: list[Path], tmp_dir: str) -> None:
    """
    Extract all PDFs, write Excel to OUTPUT_DIR, then delete the temp dir
    that held the uploaded PDFs regardless of success or failure.
    """
    job = JOBS[job_id]
    job["total"] = len(pdf_paths)
    results: list[tuple[str, dict]] = []

    try:
        for pdf_path in pdf_paths:
            log.info("[job %s] processing %s", job_id, pdf_path.name)
            try:
                images = pdf_to_base64_images(str(pdf_path), dpi=150)
                fields = extract_from_pdf_images(images)
                log.info("[job %s] ✓ %s — fields: %s", job_id, pdf_path.name,
                         {k: v for k, v in fields.items() if k != "line_items"})
                results.append((str(pdf_path), fields))
                job.setdefault("file_logs", []).append(
                    {"file": pdf_path.name, "status": "ok",
                     "fields": {k: v for k, v in fields.items() if k != "line_items"}}
                )
            except Exception as exc:  # noqa: BLE001
                log.error("[job %s] ✗ %s — %s", job_id, pdf_path.name, exc, exc_info=True)
                results.append((str(pdf_path), {"_extraction_error": str(exc), "line_items": []}))
                job.setdefault("file_logs", []).append(
                    {"file": pdf_path.name, "status": "error", "error": str(exc)}
                )

            job["processed"] += 1

        output_path = OUTPUT_DIR / f"{job_id}.xlsx"
        write_excel(results, str(output_path))
        job["output_path"] = str(output_path)
        job["status"] = "done"
        log.info("[job %s] complete → %s", job_id, output_path)

    except Exception as exc:  # noqa: BLE001
        log.error("[job %s] fatal error: %s", job_id, exc, exc_info=True)
        job["status"] = "error"
        job["error"] = str(exc)

    finally:
        # Always wipe the temp dir — PDFs are no longer needed after extraction
        shutil.rmtree(tmp_dir, ignore_errors=True)
        log.info("[job %s] temp dir deleted: %s", job_id, tmp_dir)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@app.post("/upload", response_model=JobStatus, status_code=202)
async def upload_pdfs(files: list[UploadFile] = File(...)):
    """Accept one or more PDF files and start extraction in the background."""
    if not files:
        raise HTTPException(status_code=400, detail="No files uploaded.")

    for f in files:
        if not f.filename or not f.filename.lower().endswith(".pdf"):
            raise HTTPException(status_code=400, detail=f"'{f.filename}' is not a PDF.")

    job_id = str(uuid.uuid4())

    # Register job before touching the filesystem
    JOBS[job_id] = {"status": "processing", "total": len(files), "processed": 0, "output_path": None, "error": None}

    try:
        # Temp dir holds uploaded PDFs — deleted by _process_job when done
        tmp_dir = tempfile.mkdtemp(prefix=f"debit_memo_{job_id}_")

        # Save uploaded files — explicitly close each upload handle when done
        pdf_paths: list[Path] = []
        for upload in files:
            dest = Path(tmp_dir) / (upload.filename or f"{uuid.uuid4()}.pdf")
            try:
                with dest.open("wb") as fh:
                    shutil.copyfileobj(upload.file, fh)
            finally:
                await upload.close()
            pdf_paths.append(dest)

    except Exception as exc:
        # Clean up temp dir and job entry on upload failure
        shutil.rmtree(tmp_dir, ignore_errors=True)
        del JOBS[job_id]
        raise HTTPException(status_code=500, detail=f"Failed to save uploaded files: {exc}") from exc

    # Submit to the bounded thread pool so the event loop stays free
    loop = asyncio.get_running_loop()
    loop.run_in_executor(_EXECUTOR, _process_job, job_id, pdf_paths, tmp_dir)

    return JobStatus(job_id=job_id, status="processing", total=len(pdf_paths), processed=0)


@app.get("/status/{job_id}", response_model=JobStatus)
async def get_status(job_id: str):
    """Poll extraction progress for a job."""
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    return JobStatus(
        job_id=job_id,
        status=job["status"],
        total=job.get("total", 0),
        processed=job.get("processed", 0),
        error=job.get("error"),
    )


@app.get("/download/{job_id}")
async def download_excel(job_id: str):
    """Download the finished Excel file."""
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    if job["status"] != "done":
        raise HTTPException(status_code=425, detail="Extraction not complete yet.")
    output_path = job.get("output_path")
    if not output_path or not Path(output_path).exists():
        raise HTTPException(status_code=500, detail="Output file missing.")
    return FileResponse(
        path=output_path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename="debit_memos.xlsx",
        background=_cleanup_after_download(output_path),
    )


def _cleanup_after_download(path: str):
    """BackgroundTask that removes the Excel file after it has been streamed."""
    from starlette.background import BackgroundTask
    def _delete():
        try:
            Path(path).unlink(missing_ok=True)
            log.info("cleaned up downloaded file: %s", path)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not delete %s: %s", path, exc)
    return BackgroundTask(_delete)


@app.get("/logs/{job_id}")
async def get_logs(job_id: str):
    """Return per-file extraction results — useful for debugging empty rows."""
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    return {
        "job_id": job_id,
        "status": job["status"],
        "error": job.get("error"),
        "file_logs": job.get("file_logs", []),
    }
