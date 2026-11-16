FROM python:3.12-slim

# git: pip requirements that point at git URLs. tini: reaps bot child processes.
# ffmpeg/libopus: Discord voice. Add more with --build-arg EXTRA_APT="pkg1 pkg2".
ARG EXTRA_APT=""
RUN apt-get update \
 && apt-get install -y --no-install-recommends git tini ffmpeg libopus0 ${EXTRA_APT} \
 && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    BOTDOCK_DATA=/data \
    BOTDOCK_HOST=0.0.0.0 \
    BOTDOCK_PORT=8080 \
    HOME=/data/home \
    PUID=568 \
    PGID=568

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY botdock ./botdock
COPY deploy/entrypoint.sh /entrypoint.sh
RUN chmod 755 /entrypoint.sh

VOLUME /data
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:%s/api/health'%os.environ.get('BOTDOCK_PORT','8080'),timeout=4)" || exit 1

ENTRYPOINT ["/usr/bin/tini", "-g", "--", "/entrypoint.sh"]
CMD ["python", "-m", "botdock"]
