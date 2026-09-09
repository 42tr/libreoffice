from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import FileResponse
import logging
import os
import shutil
import time
import uuid

from libreoffice_client import convert

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

app = FastAPI()
logger = logging.getLogger(__name__)

BASE_DIR = "/tmp/libreoffice"
os.makedirs(BASE_DIR, exist_ok=True)


@app.post("/convert")
async def convert_api(file: UploadFile = File(...), target_format: str = "pdf"):
    started_at = time.perf_counter()
    filename = file.filename
    if not filename:
        raise HTTPException(400, "未提供文件名")
    target_format = target_format.strip().lower()

    task_id = str(uuid.uuid4())
    work_dir = os.path.join(BASE_DIR, task_id)
    os.makedirs(work_dir, exist_ok=True)

    input_ext = os.path.splitext(filename)[1]
    input_path = os.path.join(work_dir, f"source{input_ext}")

    # 保存文件
    with open(input_path, "wb") as f:
        shutil.copyfileobj(file.file, f)
    logger.info("Received file for conversion: task_id=%s filename=%s target=%s", task_id, filename, target_format)

    output_file = os.path.splitext(filename)[0] + "." + target_format
    output_path = os.path.join(work_dir, f"result.{target_format}")

    try:
        convert(input_path, output_path, target_format)
    except ValueError as e:
        logger.warning(
            "Invalid conversion request: task_id=%s filename=%s target=%s error=%s",
            task_id,
            filename,
            target_format,
            e,
        )
        raise HTTPException(400, str(e))
    except Exception as e:
        logger.exception(
            "Conversion failed: task_id=%s filename=%s target=%s",
            task_id,
            filename,
            target_format,
        )
        raise HTTPException(500, f"转换失败: {str(e)}")

    if not os.path.exists(output_path):
        logger.error(
            "Conversion finished without output file: task_id=%s filename=%s target=%s",
            task_id,
            filename,
            target_format,
        )
        raise HTTPException(500, "输出文件不存在")

    logger.info(
        "Conversion completed: task_id=%s filename=%s target=%s elapsed=%.2fs size=%s",
        task_id,
        filename,
        target_format,
        time.perf_counter() - started_at,
        os.path.getsize(output_path),
    )

    return FileResponse(
        output_path,
        filename=output_file,
        media_type="application/octet-stream"
    )
