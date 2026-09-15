import logging
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from pathlib import Path


CONVERT_TO_MAP = {
    "pdf": "pdf",
    "docx": "docx:MS Word 2007 XML",
    "xlsx": "xlsx:Calc MS Excel 2007 XML",
    "pptx": "pptx:Impress MS PowerPoint 2007 XML",
}

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


def _format_command_error(result: subprocess.CompletedProcess[str]) -> str:
    stdout = result.stdout.strip()
    stderr = result.stderr.strip()
    parts = [f"LibreOffice 退出码 {result.returncode}"]
    if stdout:
        parts.append(stdout)
    if stderr:
        parts.append(stderr)
    return " | ".join(parts)


def convert(input_path: str, output_path: str, fmt: str = "pdf") -> None:
    fmt = fmt.lower()
    convert_to_arg = _get_convert_to_arg(fmt)
    source = Path(input_path).resolve()
    target = Path(output_path).resolve()
    output_dir = target.parent
    output_dir.mkdir(parents=True, exist_ok=True)

    if source.suffix.lower() == f".{fmt}" and source == target:
        return

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
            generated_output = conversion_dir / f"{source.stem}.{fmt}"
            result = _run_command([
                "soffice",
                "--headless",
                "--nologo",
                "--nodefault",
                "--norestore",
                f"-env:UserInstallation={profile_uri}",
                "--convert-to",
                convert_to_arg,
                "--outdir",
                str(conversion_dir),
                str(source),
            ])
            if result.returncode != 0:
                raise RuntimeError(_format_command_error(result))
            if not generated_output.is_file():
                raise RuntimeError("LibreOffice 未生成输出文件 | " + _format_command_error(result))
            shutil.move(str(generated_output), str(target))
    finally:
        conversion_slots.release()

    logger.info(
        "Converted via CLI in %.2fs: %s -> %s",
        time.perf_counter() - start,
        source.name,
        target.name,
    )
