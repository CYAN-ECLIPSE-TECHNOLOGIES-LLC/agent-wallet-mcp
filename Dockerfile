FROM python:3.12-slim
LABEL io.modelcontextprotocol.server.name="io.github.CYAN-ECLIPSE-TECHNOLOGIES-LLC/agent-wallet"
WORKDIR /app
# The v0.3.x server is a multi-module build (policy, state_machine,
# ledger_v2, families/, desk_verify, mandate_check, ...). Copy the
# full module set: the previous two-file COPY predates the rebuild
# and the image died at import (desk_verify) on the v0.3.2 tag run.
COPY *.py bazaar-manifest.json ./
COPY families/ ./families/
RUN useradd -r appuser
USER appuser
ENTRYPOINT ["python", "server.py"]
