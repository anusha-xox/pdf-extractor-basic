# ---------------------------------------------------------------------------
# Stage 1 — build: install Python dependencies
# ---------------------------------------------------------------------------
FROM registry.redhat.io/ubi9/python-312-minimal:latest AS builder

WORKDIR /app

# Copy and install deps into a prefix we can later copy cleanly
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ---------------------------------------------------------------------------
# Stage 2 — runtime: minimal image, non-root user
# ---------------------------------------------------------------------------
FROM registry.redhat.io/ubi9/python-312-minimal:latest

# Non-root user required by Code Engine (uid 1001)
USER 1001

WORKDIR /app

# Pull installed packages from builder stage
COPY --from=builder /install /usr/local

# Copy application source
COPY --chown=1001:1001 backend/  ./backend/
COPY --chown=1001:1001 frontend/ ./frontend/
COPY --chown=1001:1001 requirements.txt .

# Code Engine injects secrets as env vars — no .env file needed at runtime
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/backend

# Code Engine listens on port 8080 by default
EXPOSE 8080

# Code Engine routes external traffic through its own ingress proxy,
# so binding to 0.0.0.0 inside the container is safe here.
CMD ["python", "-m", "uvicorn", "backend.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8080", \
     "--workers", "1"]
