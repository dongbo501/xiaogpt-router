FROM python:3.11-slim@sha256:b7a9e5f6a93d6d4a31f129bbd4bdee69efccb6ded0fb230d78aafaf37af98377 AS edge-client
RUN python -m pip install --no-cache-dir --no-deps --target=/vendor edge-tts==7.3.1

FROM yihong0618/xiaogpt@sha256:2513ebea5a74b4c1e2fc5374ebbd8d05601dded39b23c3c3d7387bd7cbaf26c8
COPY --from=edge-client /vendor /opt/xiaogpt-vendor
COPY xiaogpt/ /app/xiaogpt/
COPY deploy/router/miaccount.py /app/.venv/lib/python3.14/site-packages/miservice/miaccount.py
ENV PYTHONPATH=/opt/xiaogpt-vendor
