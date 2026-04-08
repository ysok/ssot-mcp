# ssot-mcp: MCP server (Streamable HTTP) for Cursor — clone + index git repos.
# Base: CentOS Stream 9
#
# Build:   podman build -f Containerfile -t ssot-mcp .
# Run:     podman run ... -p 8081:8081 -e SSOT_UI_PORT=8081 \
#            -e SSOT_UI_SECRET=... -e SSOT_UI_PASSWORD=... ssot-mcp
#          (-e NAME with no =value passes NAME from the host; --env-file also works)
#          MCP: http://localhost:8765/mcp   Admin UI: http://localhost:8081/
#
# Cursor: add MCP server URL  http://localhost:8765/mcp  (Streamable HTTP)

FROM quay.io/centos/centos:stream9

# MCP (PyPI) requires Python >= 3.10; Stream 9 default python3 is 3.9.
RUN dnf -y update && \
    dnf -y install python3.11 git && \
    dnf clean all && \
    python3.11 -m venv /opt/venv && \
    /opt/venv/bin/pip install --upgrade pip

# SSOT_UI_PORT must match DEFAULT_UI_PORT in src/ssot_mcp/ui/config.py
ENV PATH="/opt/venv/bin:$PATH" \
    SSOT_DATA_DIR=/data \
    FASTMCP_HOST=0.0.0.0 \
    FASTMCP_PORT=8765 \
    SSOT_UI_HOST=0.0.0.0 \
    SSOT_UI_PORT=8081 \
    SSOT_MCP_TRANSPORT=streamable-http \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml README.md .
COPY src ./src
RUN pip install --no-cache-dir '.[semantic,ui]'

EXPOSE 8765 8081
VOLUME ["/data"]

CMD ["python", "-m", "ssot_mcp.runtime"]
