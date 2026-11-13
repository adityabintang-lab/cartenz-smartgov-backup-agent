FROM python:3.11-slim

ARG APT_MIRROR=https://deb.debian.org
ARG PIP_INDEX_URL=https://pypi.org/simple

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# Klien MariaDB, age (enkripsi), curl (healthcheck)
RUN set -eux; \
    if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
        sed -i "s|https\\?://deb.debian.org|${APT_MIRROR}|g" /etc/apt/sources.list.d/debian.sources; \
    elif [ -f /etc/apt/sources.list ]; then \
        sed -i "s|https\\?://deb.debian.org|${APT_MIRROR}|g" /etc/apt/sources.list; \
    fi; \
    printf 'Acquire::Retries "5";\nAcquire::http::Timeout "30";\nAcquire::https::Timeout "30";\nAcquire::ForceIPv4 "true";\n' \
        > /etc/apt/apt.conf.d/80smartgov-network; \
    attempt=1; \
    until apt-get update; do \
        if [ "$attempt" -ge 5 ]; then \
            echo >&2 "ERROR: apt-get update gagal setelah 5 percobaan. Periksa DNS/proxy/firewall Docker atau ganti APT_MIRROR build arg."; \
            exit 1; \
        fi; \
        sleep "$((attempt * 5))"; \
        attempt="$((attempt + 1))"; \
    done; \
    apt-get install -y --no-install-recommends \
        mariadb-client \
        age \
        curl \
        ca-certificates; \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Non-root user
RUN groupadd -g 1000 smartgov && \
    useradd -u 1000 -g smartgov -m -s /usr/sbin/nologin smartgov

# Dependensi Python (versi dipin; diinstal sebelum COPY kode agar layer ter-cache)
COPY requirements.txt /app/requirements.txt
RUN python -m pip install --retries 5 --timeout 30 --index-url "${PIP_INDEX_URL}" -r /app/requirements.txt

COPY ui_agent.py /app/ui_agent.py
COPY assets/ /app/assets/
COPY scripts/ /app/scripts/
COPY entrypoint.sh /app/entrypoint.sh

RUN mkdir -p /app/data /app/backups /app/logs /app/secrets /app/smartgov_app && \
    chmod 700 /app/secrets && \
    chmod +x /app/entrypoint.sh /app/scripts/*.sh && \
    chown -R smartgov:smartgov /app

USER smartgov

EXPOSE 25200

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD curl -fsS http://127.0.0.1:25200/health || exit 1

ENTRYPOINT ["/app/entrypoint.sh"]
