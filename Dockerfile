# Panoptic worker + webhook + Search API container image.
#
# Workers are pure-Python HTTP clients; the GPU work lives in
# panoptic-vllm and panoptic-retrieval (separate repos). So no CUDA,
# no torch, no huge base image — just python:3.12-slim + a handful of
# pip deps matching pyproject.toml.
#
# Code is bind-mounted at /app by docker-compose.yml. The empty stub
# dirs created below are only there so `pip install -e .` can find
# them at build time; they get overlaid by the real source at run time.

FROM python:3.12-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml ./
RUN mkdir -p shared services scripts \
    && touch shared/__init__.py services/__init__.py scripts/__init__.py \
    && pip install --no-cache-dir -e .

ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1
