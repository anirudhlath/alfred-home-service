FROM python:3.13-slim

WORKDIR /app

COPY pyproject.toml /app/pyproject.toml
RUN pip install --no-cache-dir ".[alfred]"

COPY app/ /app/app/
COPY alfred_ext/ /app/alfred_ext/

CMD ["python", "-m", "alfred_ext.register"]
