FROM docker.1ms.run/debian:bookworm-20260406-slim

ARG DEBIAN_FRONTEND=noninteractive
ARG APT_MIRROR=http://mirrors.aliyun.com

COPY apt-fast /usr/local/sbin/apt-fast

# Switch Debian APT to Aliyun mirror and bootstrap vendored apt-fast.
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
    done; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates aria2; \
    chmod +x /usr/local/sbin/apt-fast; \
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
    rm -rf /var/lib/apt/lists/*

# Install runtime dependencies
RUN apt-fast update && apt-fast install -y \
    python3 \
    python3-pip \
    # Timezone data
    tzdata \
    # sqlite3
    sqlite3 \
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
    fonts-noto \
    fonts-noto-cjk \
    fonts-liberation2 \
    fonts-crosextra-carlito \
    fonts-crosextra-caladea \
    fonts-wqy-zenhei \
    fontconfig \
    # Tools for downloading and installing LibreOffice from tarball
    tar \
    && fc-cache -f \
    && rm -rf /var/lib/apt/lists/*

# Download LibreOffice from The Document Foundation archive for the target architecture.
ARG TARGETARCH
RUN set -eux; \
    case "${TARGETARCH}" in \
        amd64) \
            LO_TARBALL='LibreOffice_25.8.4.2_Linux_x86-64_deb.tar.gz'; \
            LO_URL="https://downloadarchive.documentfoundation.org/libreoffice/old/25.8.4.2/deb/x86_64/${LO_TARBALL}"; \
            LO_SHA256='4d7b3e7ed0d48452c7bf99372e1c2169881668f8907887ad48dd4baba5729457' ;; \
        arm64) \
            LO_TARBALL='LibreOffice_26.2.4.2_Linux_aarch64_deb.tar.gz'; \
            LO_URL="https://downloadarchive.documentfoundation.org/libreoffice/old/26.2.4.2/deb/aarch64/${LO_TARBALL}"; \
            LO_SHA256='038d9d6c9045094f90d26b443c5f76f7ef09ffd8f81de6a2b89c09a37a7bc6b9' ;; \
        *) echo "Unsupported target architecture: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    aria2c --allow-overwrite=true --file-allocation=none --max-connection-per-server=8 --out=/tmp/lo.tar.gz "${LO_URL}"; \
    echo "${LO_SHA256}  /tmp/lo.tar.gz" | sha256sum -c -; \
    LO_DIR="$(tar -tzf /tmp/lo.tar.gz | head -1 | cut -d/ -f1)"; \
    tar -xzf /tmp/lo.tar.gz -C /tmp; \
    dpkg -i /tmp/${LO_DIR}/DEBS/*.deb; \
    rm -rf /tmp/lo.tar.gz /tmp/${LO_DIR}

# Ensure `soffice` is available on PATH (version dir differs per arch)
RUN ln -sf "$(ls -d /opt/libreoffice*/program/soffice | head -1)" /usr/bin/soffice

# Configure timezone
ENV TZ=Asia/Shanghai
RUN ln -snf /usr/share/zoneinfo/${TZ} /etc/localtime && echo ${TZ} > /etc/timezone

WORKDIR /app

COPY requirements.txt .
RUN pip3 install --break-system-packages -r requirements.txt

COPY . .

RUN chmod +x start.sh

CMD ["./start.sh"]
