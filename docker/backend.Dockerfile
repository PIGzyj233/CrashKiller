# Stage 1: Build Mozilla Rust dump_syms and rust-minidump minidump-stackwalk
FROM rust:1-bookworm AS rust-builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    pkg-config \
    libssl-dev \
    zlib1g-dev \
    cmake \
    clang \
    && rm -rf /var/lib/apt/lists/*

RUN cargo install dump_syms
RUN cargo install minidump-stackwalk

# Stage 2: Python 3.12 runtime with native symbol & minidump binaries
FROM python:3.12-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    cabextract \
    libssl3 \
    util-linux \
    zlib1g \
    && rm -rf /var/lib/apt/lists/*

COPY --from=rust-builder /usr/local/cargo/bin/dump_syms /usr/local/bin/dump_syms
COPY --from=rust-builder /usr/local/cargo/bin/minidump-stackwalk /usr/local/bin/minidump-stackwalk

RUN dump_syms --version && minidump-stackwalk --version

WORKDIR /app/backend

COPY backend/pyproject.toml /app/backend/pyproject.toml
COPY backend/app /app/backend/app
COPY backend/alembic.ini /app/backend/alembic.ini
COPY backend/alembic /app/backend/alembic
COPY backend/scripts /app/backend/scripts

RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir .

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
