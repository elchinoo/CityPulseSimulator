FROM python:3.14-slim

WORKDIR /app

# The shared blocks (agentpg-kit), pinned to a release; then the app itself.
ARG KIT_URL=https://github.com/elchinoo/agentpg-kit/archive/refs/tags/v0.2.4.tar.gz
RUN pip install --no-cache-dir "agentpg-kit @ ${KIT_URL}"

COPY pyproject.toml README.md ./
COPY src ./src
COPY examples ./examples
RUN pip install --no-cache-dir .

# Bind to all interfaces so a reverse proxy / sandbox can reach the web app.
# The database connection (DATABASE_URL) and tokens are supplied at runtime, never baked in.
ENV HOST=0.0.0.0 \
    PORT=8000 \
    PYTHONUNBUFFERED=1

# psycopg-binary bundles its own OpenSSL, whose default CA path is not Debian's.
ENV SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt

RUN useradd --system --no-create-home app
USER app

EXPOSE 8000

# The web app answers /healthz. A headless container (`cpsim workloads run ...`, see docker-compose.live.yml) runs no web app: turn this off there.
HEALTHCHECK --start-period=20s CMD python -c "import urllib.request,os;urllib.request.urlopen('http://localhost:'+os.environ['PORT']+'/healthz')"

# Default: the web app and API (needs DATABASE_URL). The same image runs headless, with no Postgres, by overriding the command:
#   docker run --rm --env-file .env.live ghcr.io/<owner>/citypulsesimulator cpsim workloads run /app/examples/live/paybroker.yaml
CMD ["cpsim", "serve"]
