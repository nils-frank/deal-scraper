# Build for linux/arm64 only (every homelab node is arm64):
#   docker buildx build --platform linux/arm64 -t deal-scraper:local --load .
#
# Bundles llama.cpp + a small quantized model INTO the image so the matcher can
# invoke it as a conditional subprocess with no standing service - see
# DESIGN.md "Decisions" (LLM escalation tier).

FROM debian:bookworm-slim AS llama-build
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential cmake git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /build
RUN git clone --depth 1 https://github.com/ggml-org/llama.cpp.git .
# llama-completion is a standalone one-shot/interactive CLI tool (tools/completion) that
# does not pull in the server/web-UI build path, unlike llama-cli which now links against
# llama-server-impl. Exactly what a one-shot subprocess call needs.
RUN cmake -B build \
      -DCMAKE_BUILD_TYPE=Release \
      -DBUILD_SHARED_LIBS=OFF \
      -DGGML_NATIVE=OFF \
      -DLLAMA_CURL=OFF \
      -DLLAMA_BUILD_TESTS=OFF \
    && cmake --build build --target llama-completion -j"$(nproc)"

FROM debian:bookworm-slim AS model-fetch
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*
# Qwen2.5-1.5B-Instruct, Q4_K_M: ~1.05GiB, within the "1-2B Q4" budget row in
# DESIGN.md ("Fine, irrelevant latency for a weekly batch
# escalation step"). Started at 0.5B (the doc's other suggested size) and tested it
# live against real Marktguru offer titles before building this image: at 0.5B the
# model answered "yes" to almost everything regardless of prompt content (e.g. matched
# "Nutella" against a bicycle-helmet offer) even with few-shot examples - useless for
# this task. 1.5B correctly discriminated on every real test case but one. Official
# Qwen-published GGUF.
RUN curl -fL -o /model.gguf \
    "https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF/resolve/main/qwen2.5-1.5b-instruct-q4_k_m.gguf"

FROM python:3.12-slim-bookworm
WORKDIR /app

# libgomp1: llama.cpp's OpenMP-parallelized CPU backend links it dynamically even though
# BUILD_SHARED_LIBS=OFF static-links llama.cpp's own libraries.
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --from=llama-build /build/build/bin/llama-completion /opt/llama/llama-completion
COPY --from=model-fetch /model.gguf /opt/llama/model.gguf
ENV LLAMA_CLI_BIN=/opt/llama/llama-completion
ENV LLAMA_MODEL_PATH=/opt/llama/model.gguf

COPY src/ /app/

RUN useradd -u 1000 -m scraper && mkdir -p /data && chown scraper:scraper /data
USER scraper

ENTRYPOINT ["python3", "/app/main.py"]
