FROM python:3.14-slim

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

# Bind to all interfaces so a reverse proxy / sandbox can reach the web app.
# Connection details and tokens are supplied at runtime, never baked in.
ENV HOST=0.0.0.0 \
    PORT=8000 \
    PYTHONUNBUFFERED=1 \
    CPSIM_DATA_DIR=/tmp/cpsim-data

# psycopg-binary bundles its own OpenSSL, whose default CA path is not Debian's.
ENV SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt

RUN useradd --system --no-create-home app && mkdir -p /tmp/cpsim-data && chown app /tmp/cpsim-data
USER app

EXPOSE 8000

HEALTHCHECK --start-period=20s CMD python -c "import urllib.request,os;urllib.request.urlopen('http://localhost:'+os.environ['PORT']+'/healthz')"

CMD ["cpsim", "serve"]
