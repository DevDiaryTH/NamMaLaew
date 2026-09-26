FROM python:3.12-slim

# Install system deps: ffmpeg for frame extraction, nodejs+npm for Claude Code CLI
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg nodejs npm && \
    rm -rf /var/lib/apt/lists/*

# Install Claude Code CLI globally
RUN npm install -g @anthropic-ai/claude-code

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY wlm/ wlm/
COPY monitor.py .
# web/ is built by the dashboard worker; COPY with a trailing slash handles an empty dir gracefully.
COPY web/ web/

# Create user wlm with a home directory so the claude CLI can write its config there
RUN useradd -m -u 1001 wlm && \
    mkdir -p /app/data /app/snapshots /app/logs && \
    chown -R wlm:wlm /app

ENV HOME=/home/wlm
ENV DISABLE_AUTOUPDATER=1

USER wlm
