# syntax=docker/dockerfile:1
# Hasami server image: FastAPI + MuJoCo (OSMesa, headless) + SAM 2.1 on CPU, serving the built dashboard.
# Installs as CI does (CPU torch first, then the package with the tested constraints). No keys are baked in:
# MODEL_API_KEY (Muse planner, SAM 3.1) stays "Setup required" until it is set at runtime. The SAM 2.1
# hiera-tiny weights download from Hugging Face on first use into HF_HOME (a volume), not into the image.
FROM node:22-bookworm-slim AS dashboard
ENV CI=true COREPACK_ENABLE_DOWNLOAD_PROMPT=0
WORKDIR /dash
RUN corepack enable && corepack prepare pnpm@10.29.2 --activate
COPY packages/dashboard/package.json packages/dashboard/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile
COPY packages/dashboard ./
RUN pnpm build

FROM python:3.12-slim-bookworm AS run
ENV MUJOCO_GL=osmesa PYOPENGL_PLATFORM=osmesa PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    OMP_NUM_THREADS=2 MKL_NUM_THREADS=2 HF_HOME=/data/hf HASAMI_HOST=0.0.0.0 HASAMI_PORT=8420
RUN apt-get update && apt-get install -y --no-install-recommends libosmesa6 libgl1 curl && rm -rf /var/lib/apt/lists/* \
 && pip install uv==0.9.5
WORKDIR /app
COPY constraints.txt pyproject.toml README.md LICENSE ./
COPY packages ./packages
# torch's and torchvision's CPU wheels from the PyTorch index, their dependencies at the tested versions from PyPI.
# torchvision (transformers' Sam2ImageProcessor needs it) comes from the same CPU index as torch, as in CI.
RUN uv pip install --system --extra-index-url https://download.pytorch.org/whl/cpu --index-strategy unsafe-best-match \
      "torch==2.14.1+cpu" "torchvision==0.29.1+cpu" -c constraints.txt \
 && uv pip install --system -e . PyOpenGL -c constraints.txt
COPY scripts ./scripts
COPY deployments ./deployments
COPY --from=dashboard /dash/dist ./packages/dashboard/dist
RUN useradd --system --uid 10001 --home /data hasami && mkdir -p /data/hf \
 && chown -R hasami:hasami /data /app/deployments/runs
USER hasami
EXPOSE 8420
CMD ["python", "-m", "hasami_server"]
