FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# Klien MariaDB, age (enkripsi), curl (healthcheck)
RUN apt-get update && apt-get install -y --no-install-recommends \
        mariadb-client \
        age \
        curl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Non-root user
RUN groupadd -g 1000 smartgov && \
    useradd -u 1000 -g smartgov -m -s /usr/sbin/nologin smartgov

# Dependensi Python (versi dipin; diinstal sebelum COPY kode agar layer ter-cache)
COPY requirements.txt /app/requirements.txt
RUN pip install -r /app/requirements.txt

COPY ui_agent.py /app/ui_agent.py
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
