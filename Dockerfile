FROM python:3.12-slim
LABEL io.modelcontextprotocol.server.name="io.github.CYAN-ECLIPSE-TECHNOLOGIES-LLC/agent-wallet"
WORKDIR /app
COPY server.py bazaar-manifest.json ./
RUN useradd -r appuser
USER appuser
ENTRYPOINT ["python", "server.py"]
