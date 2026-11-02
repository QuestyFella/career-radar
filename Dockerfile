# syntax=docker/dockerfile:1
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    CAREER_RADAR_DATA=/data \
    CAREER_RADAR_PORT=8765 \
    TZ=America/Toronto

RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --create-home --uid 10001 radar \
 && mkdir -p /data && chown radar:radar /data

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY career_radar ./career_radar
RUN pip install .

USER radar
VOLUME ["/data"]
EXPOSE 8765

HEALTHCHECK --interval=60s --timeout=10s --start-period=20s --retries=3 \
  CMD python -c "import os,urllib.request,base64; r=urllib.request.Request('http://127.0.0.1:%s/api/health' % os.environ.get('CAREER_RADAR_PORT','8765')); p=os.environ.get('CAREER_RADAR_PASSWORD'); p and r.add_header('Authorization','Basic '+base64.b64encode(('radar:'+p).encode()).decode()); urllib.request.urlopen(r, timeout=8)" || exit 1

CMD ["career-radar", "serve"]
