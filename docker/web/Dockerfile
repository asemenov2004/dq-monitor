FROM python:3.12-slim
WORKDIR /opt/dq-monitor
RUN groupadd -r dq && useradd -r -g dq -m dq
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir ".[web]" && pip check
USER dq
CMD ["uvicorn", "dq_monitor.web.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
