# workbench-sandbox:py311: built on the staging machine, shipped with `docker save`, pinned by digest.
# The container runs with --network none, so every package must be present at build time.
FROM python:3.11-slim-bookworm

RUN pip install --no-cache-dir \
        numpy==2.1.3 pandas==2.2.3 scipy==1.14.1 matplotlib==3.9.2 \
        openpyxl==3.1.5 python-docx==1.1.2 python-pptx==1.0.2 sympy==1.13.3 pint==0.24.4 \
    && useradd --uid 1000 --create-home sandbox

ENV PYTHONDONTWRITEBYTECODE=1 \
    MPLBACKEND=Agg \
    HF_HUB_OFFLINE=1 \
    DO_NOT_TRACK=1

USER 1000:1000
WORKDIR /workspace
