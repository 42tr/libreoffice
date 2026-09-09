#!/bin/bash
set -e

CLI_PROFILE_DIR="${LIBREOFFICE_CLI_PROFILE_DIR:-/tmp/libreoffice/cli-profile}"
mkdir -p "${CLI_PROFILE_DIR}"

echo "启动 FastAPI..."

exec uvicorn main:app --host 0.0.0.0 --port 8000
