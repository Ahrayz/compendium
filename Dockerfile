FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .

# Cloud Run injects PORT; default to 8080 for local `docker run`.
ENV PORT=8080
# Exec form needs an explicit shell: without one there is no variable expansion
# and uvicorn receives the literal string "${PORT}", exiting with code 2.
CMD ["sh", "-c", "exec uvicorn compendium.main:app --host 0.0.0.0 --port ${PORT:-8080}"]
