FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends libreoffice-writer fonts-dejavu-core && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir . && useradd --create-home agent && mkdir -p /app/data && chown agent:agent /app/data
USER agent
ENV DATA_DIR=/app/data
EXPOSE 8000
CMD ["testcase-agent", "--host", "0.0.0.0", "--port", "8000"]
