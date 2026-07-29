# Sandbox image for mathmodel-agent code execution.
# Build:  docker build -t mathmodel-sandbox:latest mathmodel/sandbox
FROM python:3.12-slim

# System libs some scientific wheels need at runtime.
RUN apt-get update && apt-get install -y --no-install-recommends \
        coreutils \
    && rm -rf /var/lib/apt/lists/*

# Scientific + modeling stack. Pinning is loose here; tighten for reproducibility.
RUN pip install --no-cache-dir \
        numpy \
        scipy \
        pandas \
        scikit-learn \
        statsmodels \
        cvxpy \
        networkx \
        sympy \
        matplotlib \
        openpyxl

# Non-root user; /work is the bind-mounted run workdir.
RUN useradd -m runner
USER runner
WORKDIR /work
