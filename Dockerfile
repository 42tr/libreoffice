FROM docker.1ms.run/debian:bookworm-20260406-slim AS base

ARG APT_MIRROR=http://mirrors.aliyun.com

# Switch Debian APT to Aliyun mirror.
RUN set -eux; \
    for sources in /etc/apt/sources.list.d/debian.sources /etc/apt/sources.list; do \
        if [ -f "${sources}" ]; then \
            sed -i \
                -e "s|http://deb.debian.org/debian|${APT_MIRROR}/debian|g" \
                -e "s|https://deb.debian.org/debian|${APT_MIRROR}/debian|g" \
                -e "s|http://deb.debian.org/debian-security|${APT_MIRROR}/debian-security|g" \
                -e "s|https://deb.debian.org/debian-security|${APT_MIRROR}/debian-security|g" \
                -e "s|http://security.debian.org/debian-security|${APT_MIRROR}/debian-security|g" \
                -e "s|https://security.debian.org/debian-security|${APT_MIRROR}/debian-security|g" \
                "${sources}"; \
        fi; \
    done

# Download LibreOffice from The Document Foundation archive for the target architecture.
# A separate stage keeps the download cached and the tarball out of the final image.
FROM base AS libreoffice

ARG DEBIAN_FRONTEND=noninteractive
ARG TARGETARCH
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates aria2; \
    LO_VERSION='26.2.4.2'; \
    case "${TARGETARCH}" in \
        amd64) \
            LO_ARCH='x86_64'; LO_TARBALL="LibreOffice_${LO_VERSION}_Linux_x86-64_deb.tar.gz"; \
            LO_SHA256='810ef197e190d7804a60e0016052c46ff33792303a200fddda9d5216a64b9900' ;; \
        arm64) \
            LO_ARCH='aarch64'; LO_TARBALL="LibreOffice_${LO_VERSION}_Linux_aarch64_deb.tar.gz"; \
            LO_SHA256='038d9d6c9045094f90d26b443c5f76f7ef09ffd8f81de6a2b89c09a37a7bc6b9' ;; \
        *) echo "Unsupported target architecture: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    aria2c --allow-overwrite=true --file-allocation=none --max-connection-per-server=8 \
        --dir=/tmp --out=lo.tar.gz \
        "https://downloadarchive.documentfoundation.org/libreoffice/old/${LO_VERSION}/deb/${LO_ARCH}/${LO_TARBALL}"; \
    echo "${LO_SHA256}  /tmp/lo.tar.gz" | sha256sum -c -; \
    tar -xzf /tmp/lo.tar.gz -C /tmp; \
    mkdir /lo; \
    mv /tmp/LibreOffice_*/DEBS/*.deb /lo/

FROM base

ARG DEBIAN_FRONTEND=noninteractive
ARG APT_MIRROR=http://mirrors.aliyun.com

# Install runtime dependencies and LibreOffice. apt-fast, aria2 and the LibreOffice
# packages are only mounted or kept for this step.
RUN --mount=type=bind,source=apt-fast,target=/mnt/apt-fast \
    --mount=type=bind,from=libreoffice,source=/lo,target=/mnt/lo \
    set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates aria2; \
    printf '%s\n' \
        '_APTMGR=apt-get' \
        'DOWNLOADBEFORE=true' \
        '_MAXNUM=8' \
        '_MAXCONPERSRV=8' \
        '_SPLITCON=8' \
        "MIRRORS=(" \
        "  '${APT_MIRROR}/debian,http://mirrors.tuna.tsinghua.edu.cn/debian,http://mirrors.ustc.edu.cn/debian'" \
        "  '${APT_MIRROR}/debian-security,http://mirrors.tuna.tsinghua.edu.cn/debian-security,http://mirrors.ustc.edu.cn/debian-security'" \
        ')' \
        > /etc/apt-fast.conf; \
    bash /mnt/apt-fast install -y --no-install-recommends \
        python3 \
        python3-pip \
        # Render PDF pages to PNG/JPEG
        poppler-utils \
        # Timezone data
        tzdata \
        # LibreOffice runtime libraries
        libxinerama1 \
        libx11-6 \
        libxext6 \
        libxrender1 \
        libxrandr2 \
        libxcb1 \
        libxau6 \
        libxdmcp6 \
        libxfixes3 \
        libxcomposite1 \
        libxdamage1 \
        libxshmfence1 \
        libfontconfig1 \
        libfreetype6 \
        libcairo2 \
        libglib2.0-0 \
        libcups2 \
        libnss3 \
        libsm6 \
        libice6 \
        libgl1 \
        fonts-dejavu \
        fonts-noto-core \
        fonts-noto-cjk \
        fonts-liberation2 \
        fonts-crosextra-carlito \
        fonts-crosextra-caladea \
        fonts-wqy-zenhei \
        fontconfig; \
    dpkg -i /mnt/lo/*.deb; \
    # Ensure `soffice` is available on PATH (version dir differs per release)
    ln -sf "$(ls -d /opt/libreoffice*/program/soffice | head -1)" /usr/bin/soffice; \
    fc-cache -f; \
    apt-get purge -y --auto-remove aria2; \
    apt-get clean; \
    rm -rf /etc/apt-fast.conf /var/lib/apt/lists/* /var/cache/apt/archives/apt-fast

# Configure timezone
ENV TZ=Asia/Shanghai
RUN ln -snf /usr/share/zoneinfo/${TZ} /etc/localtime && echo ${TZ} > /etc/timezone

# LibreOffice parses untrusted documents; do not run it as root.
RUN useradd --system --uid 10001 --create-home --home-dir /home/app app

WORKDIR /app

# Override with a nearby mirror if needed, e.g. https://mirrors.aliyun.com/pypi/simple/
ARG PIP_INDEX_URL=https://pypi.org/simple
COPY requirements.txt .
RUN pip3 install --no-cache-dir --break-system-packages -r requirements.txt

COPY main.py libreoffice_client.py ./
COPY --chmod=755 start.sh ./

USER app

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python3", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).close()"]

CMD ["./start.sh"]
