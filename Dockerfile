FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 DATA_DIR=/data
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
# When this code was built into an image, and which revision it is (deploy.sh passes a fingerprint of the code).
# Both come after the code, so an unchanged checkout keeps its cached layers and its build time.
RUN date -u +%Y-%m-%dT%H:%M:%SZ > /srv/build_time
ARG REVISION=dev
ENV APP_REVISION=$REVISION

RUN useradd --uid 10001 --no-create-home appuser && mkdir -p /data && chown appuser /data
USER appuser
VOLUME /data
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4).status == 200 else 1)"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
