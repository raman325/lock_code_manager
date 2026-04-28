FROM mcr.microsoft.com/vscode/devcontainers/base:debian

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# System dependencies needed by HA and lock integrations
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        libudev-dev \
        libpcap-dev \
        libyaml-dev \
        libxml2 \
        git \
        cmake \
        autoconf \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# uv for fast Python/pip operations
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

USER vscode

# Install Python 3.14 via uv and create venv
ENV VIRTUAL_ENV="/home/vscode/.local/ha-venv"
RUN uv python install 3.14 \
    && uv venv --python 3.14 $VIRTUAL_ENV
ENV PATH="$VIRTUAL_ENV/bin:$PATH"

# Claude Code
RUN curl -fsSL https://claude.ai/install.sh | bash
ENV PATH="/home/vscode/.claude/local/bin:$PATH"

# Rust (needed for pyo3-ffi build deps and prek)
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
ENV PATH="/home/vscode/.cargo/bin:$PATH"

# Enable corepack so yarn runs without interactive prompts
RUN corepack enable

WORKDIR /workspaces

ENV SHELL=/bin/bash
