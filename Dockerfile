FROM python:3.14-slim-trixie

RUN pip install requests inotify_simple websocket-client pyyaml

COPY app.py /app/app.py

CMD ["python", "/app/app.py"]
