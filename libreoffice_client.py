import logging
import math
import os
import queue
import re
import shutil
import signal
import subprocess
import tempfile
import time

from PIL import Image
from pathlib import Path


CONVERT_TO_MAP = {
    "pdf": "pdf",
    "docx": "docx:MS Word 2007 XML",
    "xlsx": "xlsx:Calc MS Excel 2007 XML",
    "pptx": "pptx:Impress MS PowerPoint 2007 XML",
}

IMAGE_FORMATS = {"png", "jpg", "jpeg"}
SUPPORTED_FORMATS = (*CONVERT_TO_MAP, "png", "jpg", "jpeg")

DEFAULT_DPI = 150
MIN_DPI = 50
MAX_DPI = 600
MAX_IMAGE_PIXELS = 80_000_000
MAX_JPEG_SIDE = 65500
JPEG_QUALITY = 90

logger = logging.getLogger(__name__)


def positive_int_env(name: str, default: int) -> int:
    value = int(os.getenv(name, str(default)))
    if value < 1:
        raise ValueError(f"{name} 必须为正整数")
    return value


CLI_TIMEOUT_SECONDS = positive_int_env("LIBREOFFICE_CLI_TIMEOUT", 180)
TASK_TIMEOUT_SECONDS = positive_int_env("LIBREOFFICE_TASK_TIMEOUT", 300)
MAX_CONCURRENCY = positive_int_env("LIBREOFFICE_MAX_CONCURRENCY", 4)
QUEUE_TIMEOUT_SECONDS = positive_int_env("LIBREOFFICE_QUEUE_TIMEOUT", 60)
CLI_PROFILE_DIR = Path(
    os.getenv("LIBREOFFICE_CLI_PROFILE_DIR", "/tmp/libreoffice/cli-profile")
).resolve()


class ConversionBusyError(RuntimeError):
    pass


def make_slots(size: int) -> queue.LifoQueue:
    """Slot ids double as profile ids, so a profile never serves two jobs at once.

    LIFO keeps light traffic on the same few, already initialised profiles.
    """
    slots = queue.LifoQueue()
    for slot in reversed(range(size)):
        slots.put(slot)
    return slots


conversion_slots = make_slots(MAX_CONCURRENCY)


def _run_command(command: list[str], deadline: float | None = None,
                 env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    timeout = CLI_TIMEOUT_SECONDS
    if deadline is not None:
        timeout = min(timeout, deadline - time.monotonic())
        if timeout <= 0:
            raise subprocess.TimeoutExpired(command, 0)
    # soffice may launch children; kill the whole session before reusing a slot.
    with subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, errors="replace", start_new_session=True,
        env=None if env is None else {**os.environ, **env},
    ) as process:
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.communicate()
            raise
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def validate_options(fmt: str, page: int | None = None, dpi: int | None = None) -> str:
    """Return the normalised format; raise ValueError before any work starts."""
    fmt = fmt.strip().lower()
    if fmt not in SUPPORTED_FORMATS:
        raise ValueError(f"不支持的目标格式: {fmt}")
    if fmt not in IMAGE_FORMATS and (page is not None or dpi is not None):
        raise ValueError("page 和 dpi 参数仅支持 PNG/JPG/JPEG 图片转换")
    if page is not None and (not _is_int(page) or page < 1):
        raise ValueError("page 必须为从 1 开始的正整数")
    if dpi is not None and (not _is_int(dpi) or not MIN_DPI <= dpi <= MAX_DPI):
        raise ValueError(f"dpi 必须为 {MIN_DPI}-{MAX_DPI} 的整数")
    return fmt


def _format_command_error(result: subprocess.CompletedProcess[str], tool: str = "LibreOffice") -> str:
    stdout = result.stdout.strip()
    stderr = result.stderr.strip()
    parts = [f"{tool} 退出码 {result.returncode}"]
    if stdout:
        parts.append(stdout)
    if stderr:
        parts.append(stderr)
    return " | ".join(parts)


def _convert_document(source: Path, output_dir: Path, profile: Path, fmt: str,
                      deadline: float) -> Path:
    try:
        result = _run_command([
            "soffice", "--headless", "--nologo", "--nodefault", "--norestore",
            f"-env:UserInstallation={profile.as_uri()}",
            "--convert-to", CONVERT_TO_MAP[fmt],
            "--outdir", str(output_dir), str(source),
        ], deadline)
        if result.returncode != 0:
            raise RuntimeError(_format_command_error(result))
    except BaseException:
        # A killed or failed soffice may leave a locked or half-written profile.
        shutil.rmtree(profile, ignore_errors=True)
        raise
    generated = output_dir / f"{source.stem}.{fmt}"
    if not generated.is_file():
        raise RuntimeError("LibreOffice 未生成输出文件 | " + _format_command_error(result))
    return generated


def _check_canvas(sizes: list[tuple[int, int]], fmt: str) -> None:
    width = max(size[0] for size in sizes)
    height = sum(size[1] for size in sizes)
    if width * height > MAX_IMAGE_PIXELS:
        raise ValueError("图片超过 8000 万像素，请降低 dpi 或使用 page 参数分单页转换")
    if fmt != "png" and max(width, height) > MAX_JPEG_SIDE:
        raise ValueError("JPEG 边长不能超过 65500 像素，请使用 PNG、降低 dpi 或使用 page 参数")


def _save_image(image: Image.Image, target: Path, fmt: str, dpi: int) -> None:
    if fmt == "png":
        image.save(target, format="PNG", dpi=(dpi, dpi))
    else:
        image.convert("RGB").save(target, format="JPEG", quality=JPEG_QUALITY, dpi=(dpi, dpi))


def _stitch_images(pages: list[Path], target: Path, fmt: str, dpi: int = DEFAULT_DPI) -> None:
    sizes = []
    for path in pages:
        with Image.open(path) as image:
            sizes.append(image.size)
    _check_canvas(sizes, fmt)
    if len(pages) == 1:
        with Image.open(pages[0]) as image:
            _save_image(image, target, fmt, dpi)
        return
    with Image.new("RGB", (max(w for w, _ in sizes), sum(h for _, h in sizes)), "white") as combined:
        y = 0
        for path in pages:
            with Image.open(path) as image:
                combined.paste(image, (0, y))
                y += image.height
        _save_image(combined, target, fmt, dpi)


def _page_sizes(pdf: Path, deadline: float) -> tuple[int, dict[int, tuple[float, float]]]:
    """Return the page count and each page's point size after rotation."""
    # pdfinfo clamps the last page to the document length.
    info = _run_command(["pdfinfo", "-f", "1", "-l", "1000000", str(pdf)],
                        deadline, env={"LC_ALL": "C"})
    if info.returncode != 0:
        raise RuntimeError(_format_command_error(info, "pdfinfo"))
    match = re.search(r"^Pages:\s+(\d+)", info.stdout, re.MULTILINE)
    if match is None:
        raise RuntimeError("无法读取 PDF 页数")
    rotations = {int(n): int(r) for n, r in re.findall(
        r"^Page\s+(\d+)\s+rot:\s+(\d+)", info.stdout, re.MULTILINE)}
    sizes = {}
    for n, w, h in re.findall(r"^Page\s+(\d+)\s+size:\s+([\d.]+) x ([\d.]+)",
                              info.stdout, re.MULTILINE):
        n, w, h = int(n), float(w), float(h)
        sizes[n] = (h, w) if rotations.get(n, 0) % 180 == 90 else (w, h)
    return int(match.group(1)), sizes


def _render_images(pdf: Path, output_dir: Path, target: Path, fmt: str,
                   page: int | None, dpi: int, deadline: float) -> Path:
    count, sizes = _page_sizes(pdf, deadline)
    if page is not None and page > count:
        raise ValueError(f"页码超出范围：文档共 {count} 页，请求第 {page} 页")
    points = [size for n, size in sizes.items() if page is None or n == page]
    if points:
        # Reject oversized output before rendering; the stitch step re-checks exact sizes.
        _check_canvas([(math.ceil(w * dpi / 72), math.ceil(h * dpi / 72))
                       for w, h in points], fmt)
    options = [] if page is None else ["-f", str(page), "-l", str(page)]
    # Always render lossless pages so JPEG output is only encoded once.
    result = _run_command([
        "pdftoppm", "-r", str(dpi), "-png", *options, str(pdf), str(output_dir / "page"),
    ], deadline)
    if result.returncode != 0:
        raise RuntimeError(_format_command_error(result, "pdftoppm"))
    pages = sorted(output_dir.glob("page-*.png"),
                   key=lambda path: int(path.stem.split("-")[-1]))
    if not pages:
        raise RuntimeError("pdftoppm 未生成图片")
    if len(pages) == 1 and fmt == "png":
        shutil.move(str(pages[0]), str(target))
    else:
        _stitch_images(pages, target, fmt, dpi)
    return target


def convert(input_path: str, output_path: str, fmt: str = "pdf",
            page: int | None = None, dpi: int | None = None) -> Path:
    """Return an output file; image pages are stacked vertically unless selected."""
    fmt = validate_options(fmt, page, dpi)
    source = Path(input_path).resolve()
    target = Path(output_path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)

    if fmt == "pdf" and source.suffix.lower() == ".pdf":
        # LibreOffice would re-import the PDF into Draw and lose fidelity.
        if source != target:
            shutil.copyfile(source, target)
        return target

    try:
        slot = conversion_slots.get(timeout=QUEUE_TIMEOUT_SECONDS)
    except queue.Empty:
        raise ConversionBusyError("转换服务繁忙，请稍后重试") from None
    try:
        start = time.perf_counter()
        deadline = time.monotonic() + TASK_TIMEOUT_SECONDS
        CLI_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        # Profiles are reused per slot to skip first-start initialisation; the pid
        # keeps API workers apart.
        profile = CLI_PROFILE_DIR / f"profile-{os.getpid()}-{slot}"
        with tempfile.TemporaryDirectory(prefix="job-", dir=CLI_PROFILE_DIR) as job:
            conversion_dir = Path(job)
            if fmt in IMAGE_FORMATS:
                pdf = source if source.suffix.lower() == ".pdf" else _convert_document(
                    source, conversion_dir, profile, "pdf", deadline
                )
                target = _render_images(pdf, conversion_dir, target, fmt, page,
                                        dpi or DEFAULT_DPI, deadline)
            else:
                generated_output = _convert_document(source, conversion_dir, profile, fmt, deadline)
                shutil.move(str(generated_output), str(target))
    finally:
        conversion_slots.put(slot)

    logger.info(
        "Converted via CLI in %.2fs: %s -> %s",
        time.perf_counter() - start,
        source.name,
        target.name,
    )
    return target
