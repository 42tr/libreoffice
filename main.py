from contextlib import contextmanager
from fastapi import FastAPI, UploadFile, File, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from starlette.background import BackgroundTask
import logging
import os
from pathlib import Path
import shutil
import subprocess
import time
import uuid

from libreoffice_client import (
    DEFAULT_DPI, MAX_DPI, MIN_DPI, SUPPORTED_FORMATS,
    ConversionBusyError, convert, positive_int_env, validate_options,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

logger = logging.getLogger(__name__)

BASE_DIR = os.getenv("LIBREOFFICE_WORK_DIR", "/tmp/libreoffice")
os.makedirs(BASE_DIR, exist_ok=True)
MAX_UPLOAD_MB = positive_int_env("LIBREOFFICE_MAX_UPLOAD_MB", 100)

MEDIA_TYPES = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}


class UploadLimitMiddleware:
    """Reject oversized request bodies before Starlette spools them to disk."""

    def __init__(self, app, max_bytes: int):
        self.app = app
        self.max_bytes = max_bytes
        self.message = f"请求体超过 {max_bytes // (1024 * 1024)} MB 上限"

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        length = dict(scope["headers"]).get(b"content-length", b"")
        if length.isdigit() and int(length) > self.max_bytes:
            await JSONResponse({"detail": self.message}, 413)(scope, receive, send)
            return
        received = 0

        async def limited_receive():
            nonlocal received
            message = await receive()
            received += len(message.get("body", b""))
            if received > self.max_bytes:
                # FastAPI re-raises HTTPException from body parsing unchanged.
                raise HTTPException(413, self.message)
            return message

        await self.app(scope, limited_receive, send)


app = FastAPI()
app.add_middleware(UploadLimitMiddleware, max_bytes=MAX_UPLOAD_MB * 1024 * 1024)


@contextmanager
def _remove_on_error(path: str):
    try:
        yield
    except BaseException:
        shutil.rmtree(path, ignore_errors=True)
        raise


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/convert")
def convert_api(
    file: UploadFile = File(...),
    target_format: str = Query(
        "pdf", description="目标格式，不区分大小写：" + "、".join(SUPPORTED_FORMATS)),
    page: int | None = Query(None, ge=1, description="图片页码，从 1 开始；不传则纵向拼接所有页"),
    dpi: int | None = Query(None, ge=MIN_DPI, le=MAX_DPI,
                            description=f"图片分辨率，默认 {DEFAULT_DPI}"),
):
    # FastAPI runs synchronous endpoints in its thread pool, keeping the event
    # loop free while copying files, waiting for a slot, and running soffice.
    started_at = time.perf_counter()
    # Some clients send a full client-side path; only the base name is meaningful.
    filename = (file.filename or "").replace("\\", "/").rsplit("/", 1)[-1]
    if not filename:
        raise HTTPException(400, "未提供文件名")
    try:
        target_format = validate_options(target_format, page, dpi)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    task_id = uuid.uuid4().hex
    work_dir = os.path.join(BASE_DIR, f"task-{task_id}")
    os.makedirs(work_dir)

    with _remove_on_error(work_dir):
        try:
            stem, input_ext = os.path.splitext(filename)
            input_path = os.path.join(work_dir, f"source{input_ext}")
            with open(input_path, "wb") as f:
                shutil.copyfileobj(file.file, f)
            logger.info(
                "Received file for conversion: task_id=%s filename=%s target=%s",
                task_id, filename, target_format,
            )
            output_path = str(convert(
                input_path, os.path.join(work_dir, f"result.{target_format}"),
                target_format, page=page, dpi=dpi,
            ))
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        except ConversionBusyError as e:
            raise HTTPException(503, str(e), headers={"Retry-After": "5"}) from e
        except subprocess.TimeoutExpired as e:
            logger.warning("Conversion timed out: task_id=%s", task_id)
            raise HTTPException(504, "转换超时") from e
        except Exception as e:
            logger.exception(
                "Conversion failed: task_id=%s filename=%s target=%s",
                task_id, filename, target_format,
            )
            raise HTTPException(500, f"转换失败，任务 ID: {task_id}") from e

        output_ext = Path(output_path).suffix.lower()
        logger.info(
            "Conversion completed: task_id=%s filename=%s target=%s elapsed=%.2fs size=%s",
            task_id, filename, target_format,
            time.perf_counter() - started_at, os.path.getsize(output_path),
        )
        return FileResponse(
            output_path,
            filename=stem + output_ext,
            media_type=MEDIA_TYPES.get(output_ext, "application/octet-stream"),
            background=BackgroundTask(shutil.rmtree, work_dir, ignore_errors=True),
        )
