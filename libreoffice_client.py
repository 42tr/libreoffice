import logging
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import re

from PIL import Image
from pathlib import Path


CONVERT_TO_MAP = {
    "pdf": "pdf",
    "docx": "docx:MS Word 2007 XML",
    "xlsx": "xlsx:Calc MS Excel 2007 XML",
    "pptx": "pptx:Impress MS PowerPoint 2007 XML",
}

IMAGE_FORMATS = {"png", "jpg", "jpeg"}

logger = logging.getLogger(__name__)
CLI_TIMEOUT_SECONDS = int(os.getenv("LIBREOFFICE_CLI_TIMEOUT", "180"))
MAX_CONCURRENCY = int(os.getenv("LIBREOFFICE_MAX_CONCURRENCY", "4"))
QUEUE_TIMEOUT_SECONDS = int(os.getenv("LIBREOFFICE_QUEUE_TIMEOUT", "60"))
if min(CLI_TIMEOUT_SECONDS, MAX_CONCURRENCY, QUEUE_TIMEOUT_SECONDS) < 1:
    raise ValueError("LibreOffice 超时和并发配置必须为正整数")
conversion_slots = threading.BoundedSemaphore(MAX_CONCURRENCY)
CLI_PROFILE_DIR = Path(
    os.getenv("LIBREOFFICE_CLI_PROFILE_DIR", "/tmp/libreoffice/cli-profile")
).resolve()


class ConversionBusyError(RuntimeError):
    pass


def _run_command(command: list[str]) -> subprocess.CompletedProcess[str]:
    # soffice may launch children; kill the whole session before reusing a slot.
    with subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, start_new_session=True,
        env={**os.environ, "LC_ALL": "C"} if command[0] == "pdfinfo" else None,
    ) as process:
        try:
            stdout, stderr = process.communicate(timeout=CLI_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.communicate()
            raise
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def _get_convert_to_arg(fmt: str) -> str:
    convert_to_arg = CONVERT_TO_MAP.get(fmt.lower())
    if convert_to_arg is None:
        raise ValueError(f"不支持的目标格式: {fmt}")
    return convert_to_arg


def _format_command_error(result: subprocess.CompletedProcess[str], tool: str = "LibreOffice") -> str:
    stdout = result.stdout.strip()
    stderr = result.stderr.strip()
    parts = [f"{tool} 退出码 {result.returncode}"]
    if stdout:
        parts.append(stdout)
    if stderr:
        parts.append(stderr)
    return " | ".join(parts)


def _convert_document(source: Path, output_dir: Path, profile_uri: str, fmt: str) -> Path:
    result = _run_command([
        "soffice", "--headless", "--nologo", "--nodefault", "--norestore",
        f"-env:UserInstallation={profile_uri}",
        "--convert-to", _get_convert_to_arg(fmt),
        "--outdir", str(output_dir), str(source),
    ])
    if result.returncode != 0:
        raise RuntimeError(_format_command_error(result))
    generated = output_dir / f"{source.stem}.{fmt}"
    if not generated.is_file():
        raise RuntimeError("LibreOffice 未生成输出文件 | " + _format_command_error(result))
    return generated


def _stitch_images(pages: list[Path], target: Path, fmt: str) -> None:
    sizes = []
    for path in pages:
        with Image.open(path) as image:
            sizes.append(image.size)
    width = max(size[0] for size in sizes)
    height = sum(size[1] for size in sizes)
    if width * height > 80_000_000:
        raise ValueError("拼接图片超过 8000 万像素，请使用 page 参数分单页转换")
    if fmt != "png" and max(width, height) > 65500:
        raise ValueError("JPEG 边长不能超过 65500 像素，请使用 PNG 或 page 参数")
    with Image.new("RGB", (width, height), "white") as combined:
        y = 0
        for path in pages:
            with Image.open(path) as image:
                combined.paste(image, (0, y))
                y += image.height
        combined.save(target, format="PNG" if fmt == "png" else "JPEG", dpi=(150, 150))


def _render_images(pdf: Path, output_dir: Path, target: Path, fmt: str,
                   page: int | None = None) -> Path:
    options = []
    if page is not None:
        info = _run_command(["pdfinfo", str(pdf)])
        if info.returncode != 0:
            raise RuntimeError(_format_command_error(info, "pdfinfo"))
        match = re.search(r"^Pages:\s+(\d+)", info.stdout, re.MULTILINE)
        if match is None:
            raise RuntimeError("无法读取 PDF 页数")
        count = int(match.group(1))
        if page > count:
            raise ValueError(f"页码超出范围：文档共 {count} 页，请求第 {page} 页")
        options = ["-f", str(page), "-l", str(page)]
    extension = "png" if fmt == "png" else "jpg"
    result = _run_command([
        "pdftoppm", "-r", "150", "-png" if fmt == "png" else "-jpeg",
        *options, str(pdf), str(output_dir / "page"),
    ])
    if result.returncode != 0:
        raise RuntimeError(_format_command_error(result, "pdftoppm"))
    pages = sorted(output_dir.glob(f"page-*.{extension}"),
                   key=lambda path: int(path.stem.split("-")[-1]))
    if not pages:
        raise RuntimeError("pdftoppm 未生成图片")
    if len(pages) == 1:
        shutil.move(str(pages[0]), str(target))
    else:
        _stitch_images(pages, target, fmt)
    return target


def convert(input_path: str, output_path: str, fmt: str = "pdf",
            page: int | None = None) -> Path:
    """Return an output file; image pages are stacked vertically unless selected."""
    fmt = fmt.lower()
    if page is not None:
        if isinstance(page, bool) or not isinstance(page, int) or page < 1:
            raise ValueError("page 必须为从 1 开始的正整数")
        if fmt not in IMAGE_FORMATS:
            raise ValueError("page 参数仅支持 PNG/JPG/JPEG 图片转换")
    if fmt not in IMAGE_FORMATS:
        _get_convert_to_arg(fmt)
    source = Path(input_path).resolve()
    target = Path(output_path).resolve()
    output_dir = target.parent
    output_dir.mkdir(parents=True, exist_ok=True)

    if source.suffix.lower() == f".{fmt}" and source == target:
        return target

    if not conversion_slots.acquire(timeout=QUEUE_TIMEOUT_SECONDS):
        raise ConversionBusyError("转换服务繁忙，请稍后重试")
    try:
        start = time.perf_counter()
        CLI_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        # Each invocation needs its own profile, including across API workers.
        with tempfile.TemporaryDirectory(prefix="job-", dir=CLI_PROFILE_DIR) as job:
            profile_uri = (Path(job) / "profile").as_uri()
            conversion_dir = Path(job) / "output"
            conversion_dir.mkdir()
            if fmt in IMAGE_FORMATS:
                pdf = source if source.suffix.lower() == ".pdf" else _convert_document(
                    source, conversion_dir, profile_uri, "pdf"
                )
                target = _render_images(pdf, conversion_dir, target, fmt, page)
            else:
                generated_output = _convert_document(source, conversion_dir, profile_uri, fmt)
                shutil.move(str(generated_output), str(target))
    finally:
        conversion_slots.release()

    logger.info(
        "Converted via CLI in %.2fs: %s -> %s",
        time.perf_counter() - start,
        source.name,
        target.name,
    )
    return target
