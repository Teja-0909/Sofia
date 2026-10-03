# Build only with ./browser_worker as context: no app code, .env, DB or credentials.
FROM mcr.microsoft.com/playwright/python:v1.63.0-noble
USER root
RUN apt-get update && apt-get install -y --no-install-recommends iptables util-linux \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 reader && useradd --uid 10001 --gid 10001 --no-create-home reader
WORKDIR /opt/reader
COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY __init__.py policy.py proxy.py reader.py server.py ./browser_worker/
COPY entrypoint.sh /usr/local/bin/reader-entrypoint
RUN chmod 0555 /usr/local/bin/reader-entrypoint
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
ENTRYPOINT ["/usr/local/bin/reader-entrypoint"]
