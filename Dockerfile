# python:3.14-slim, pinned by digest (Dependabot docker updates it)
FROM python:3.14-slim@sha256:cd89c30eda26ee911a4611b18680086e0ea42e6f5f8cbe653d07fa7958c9cec8

WORKDIR /app

# Dependencies are installed from hash-locked files (requirements/relock.sh regenerates them). The shared blocks (agentpg-kit)
# are in the lock too, pinned to a commit-addressed archive by sha256. Nothing is resolved at build time.
COPY requirements/build.txt requirements/runtime.txt ./requirements/
RUN pip install --no-cache-dir --require-hashes -r requirements/build.txt \
 && pip install --no-cache-dir --require-hashes --no-deps --no-build-isolation -r requirements/runtime.txt

# Then the app itself (its dependencies are already installed).

COPY pyproject.toml README.md ./
COPY src ./src
COPY examples ./examples
RUN pip install --no-cache-dir --no-deps --no-build-isolation .

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
