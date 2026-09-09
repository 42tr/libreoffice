import logging
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path


CONVERT_TO_MAP = {
    "pdf": "pdf",
    "docx": "docx:MS Word 2007 XML",
    "xlsx": "xlsx:Calc MS Excel 2007 XML",
    "pptx": "pptx:Impress MS PowerPoint 2007 XML",
}

lock = threading.Lock()
logger = logging.getLogger(__name__)
CLI_TIMEOUT_SECONDS = int(os.getenv("LIBREOFFICE_CLI_TIMEOUT", "180"))
CLI_PROFILE_DIR = Path(
    os.getenv("LIBREOFFICE_CLI_PROFILE_DIR", "/tmp/libreoffice/cli-profile")
).resolve()


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
    source = Path(input_path).resolve()
    target = Path(output_path).resolve()
    output_dir = target.parent
    output_dir.mkdir(parents=True, exist_ok=True)

    if source.suffix.lower() == f".{fmt}" and source == target:
        return

    generated_output = output_dir / f"{source.stem}.{fmt}"
    if generated_output.exists() and generated_output != source:
        generated_output.unlink()
    if target.exists() and target != source:
        target.unlink()

    CLI_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    profile_uri = CLI_PROFILE_DIR.as_uri()

    with lock:
        start = time.perf_counter()
        result = subprocess.run(
            [
                "soffice",
                "--headless",
                "--nologo",
                "--nodefault",
                "--norestore",
                "--nolockcheck",
                f"-env:UserInstallation={profile_uri}",
                "--convert-to",
                _get_convert_to_arg(fmt),
                "--outdir",
                str(output_dir),
                str(source),
            ],
            capture_output=True,
            text=True,
            timeout=CLI_TIMEOUT_SECONDS,
        )

    if result.returncode != 0:
        raise RuntimeError(_format_command_error(result))

    if not generated_output.exists():
        raise RuntimeError(
            "LibreOffice 未生成输出文件"
            + (f": {result.stdout.strip()}" if result.stdout.strip() else "")
        )

    if generated_output != target:
        shutil.move(str(generated_output), str(target))

    logger.info(
        "Converted via CLI in %.2fs: %s -> %s",
        time.perf_counter() - start,
        source.name,
        target.name,
    )
