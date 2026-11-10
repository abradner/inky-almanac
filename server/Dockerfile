FROM python:3.13-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends fonts-dejavu-core fonts-dejavu-extra tzdata \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY almanac ./almanac
# Signed over-the-air release for the frame (scripts/sign_ota.py); may be empty.
COPY ota ./ota

RUN useradd --system --uid 10001 almanac && mkdir -p /var/cache/almanac && chown almanac /var/cache/almanac
USER almanac
ENV PYTHONUNBUFFERED=1 TZ=Australia/Sydney
EXPOSE 8077
HEALTHCHECK --interval=5m --timeout=10s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8077/healthz', timeout=5)"
CMD ["python", "-m", "almanac.app"]
