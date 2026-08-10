# Railway cloud processor image (tunnel-direct architecture)
# - msodbcsql18: SQL Server driver for tunnel-direct PMS fetching (Protel/Pylon)
# - cloudflared:  on-demand Access TCP clients (db/tunnel.py)
# Railway auto-detects this Dockerfile and uses it instead of nixpacks.

FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl gnupg2 \
    && curl -sSL https://packages.microsoft.com/keys/microsoft.asc \
       | gpg --dearmor -o /usr/share/keyrings/microsoft-prod.gpg \
    && echo "deb [arch=amd64,arm64 signed-by=/usr/share/keyrings/microsoft-prod.gpg] \
       https://packages.microsoft.com/debian/12/prod bookworm main" \
       > /etc/apt/sources.list.d/mssql-release.list \
    && apt-get update \
    && ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql18 unixodbc \
    && curl -fsSL -o /usr/local/bin/cloudflared \
       https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 \
    && chmod +x /usr/local/bin/cloudflared \
    && apt-get purge -y gnupg2 \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Old on-prem Protel SQL Servers speak TLS 1.0 in the TDS prelogin. A base
# image refresh raised Debian/OpenSSL's floor to TLS 1.2 and broke the tunnel
# fetch (incident 2026-08-10: "handshake before login" ODBC 08001). Relax the
# floor — transport security comes from the Cloudflare tunnel, not TDS TLS.
RUN sed -i 's/^MinProtocol *=.*/MinProtocol = TLSv1.0/' /etc/ssl/openssl.cnf \
    && sed -i 's/^CipherString *=.*/CipherString = DEFAULT@SECLEVEL=0/' /etc/ssl/openssl.cnf

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Phase A: FastAPI owns the HTTP surface; scheduler + poller start in its
# lifespan. EXACTLY 1 worker — N workers = N schedulers = duplicate briefings.
# Rollback path: switch back to `python railway_main.py` (file unchanged).
CMD uvicorn api:app --host 0.0.0.0 --port ${PORT:-8080} --workers 1
