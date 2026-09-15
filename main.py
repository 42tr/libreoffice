from fastapi import FastAPI, UploadFile, File, HTTPException, Query
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask
import logging
import os
from pathlib import Path
import shutil
import subprocess
import time
import uuid

from libreoffice_client import ConversionBusyError, convert

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

app = FastAPI()
logger = logging.getLogger(__name__)

BASE_DIR = "/tmp/libreoffice"
os.makedirs(BASE_DIR, exist_ok=True)


@app.post("/convert")
def convert_api(
    file: UploadFile = File(...), target_format: str = "pdf",
    page: int | None = Query(None, ge=1, description="图片页码，从 1 开始；不传则纵向拼接所有页"),
):
    # FastAPI runs synchronous endpoints in its thread pool, keeping the event
    # loop free while copying files, waiting for a slot, and running soffice.
    started_at = time.perf_counter()
    filename = file.filename
    if not filename:
        raise HTTPException(400, "未提供文件名")
    target_format = target_format.strip().lower()

    task_id = str(uuid.uuid4())
    work_dir = os.path.join(BASE_DIR, task_id)
    os.makedirs(work_dir, exist_ok=True)

    try:
        input_ext = os.path.splitext(filename)[1]
        input_path = os.path.join(work_dir, f"source{input_ext}")
        with open(input_path, "wb") as f:
            shutil.copyfileobj(file.file, f)
        logger.info(
            "Received file for conversion: task_id=%s filename=%s target=%s",
            task_id, filename, target_format,
        )
        output_path = os.path.join(work_dir, f"result.{target_format}")
        output_path = str(convert(input_path, output_path, target_format, page=page))
        output_ext = Path(output_path).suffix.lower()
        output_file = os.path.splitext(filename)[0] + output_ext
        if not os.path.isfile(output_path):
            raise RuntimeError("输出文件不存在")

        logger.info(
            "Conversion completed: task_id=%s filename=%s target=%s elapsed=%.2fs size=%s",
            task_id, filename, target_format,
            time.perf_counter() - started_at, os.path.getsize(output_path),
        )
        response = FileResponse(
            output_path,
            filename=output_file,
            media_type={".png": "image/png", ".jpg": "image/jpeg",
                        ".jpeg": "image/jpeg"}.get(
                            output_ext, "application/octet-stream"),
            background=BackgroundTask(shutil.rmtree, work_dir, ignore_errors=True),
        )
    except Exception as e:
        shutil.rmtree(work_dir, ignore_errors=True)
        if isinstance(e, ValueError):
            raise HTTPException(400, str(e)) from e
        if isinstance(e, ConversionBusyError):
            raise HTTPException(503, str(e), headers={"Retry-After": "5"}) from e
        if isinstance(e, subprocess.TimeoutExpired):
            logger.warning("Conversion timed out: task_id=%s", task_id)
            raise HTTPException(504, "转换超时") from e
        logger.exception(
            "Conversion failed: task_id=%s filename=%s target=%s",
            task_id, filename, target_format,
        )
        raise HTTPException(500, f"转换失败: {str(e)}") from e
    return response
