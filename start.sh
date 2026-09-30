#!/bin/bash
set -e

WORK_DIR="${LIBREOFFICE_WORK_DIR:-/tmp/libreoffice}"
CLI_PROFILE_DIR="${LIBREOFFICE_CLI_PROFILE_DIR:-/tmp/libreoffice/cli-profile}"
mkdir -p "${WORK_DIR}" "${CLI_PROFILE_DIR}"

# Remove leftovers from a previous crash; only names this service creates are touched.
find "${WORK_DIR}" -mindepth 1 -maxdepth 1 -name 'task-*' -exec rm -rf {} +
find "${CLI_PROFILE_DIR}" -mindepth 1 -maxdepth 1 \( -name 'job-*' -o -name 'profile-*' \) -exec rm -rf {} +

echo "启动 FastAPI..."

exec uvicorn main:app --host 0.0.0.0 --port 8000
