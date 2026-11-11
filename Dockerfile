FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# LightGBM needs the GNU OpenMP runtime at inference time.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY configs ./configs
COPY sql ./sql
COPY src ./src
COPY dashboard ./dashboard

EXPOSE 8000

# The trained model, warehouse and datasets are mounted at runtime:
#   docker run -p 8000:8000 -v "$PWD/artifacts:/app/artifacts" -v "$PWD/data:/app/data" wafer-yield-intelligence
CMD ["uvicorn", "src.serving.app:app", "--host", "0.0.0.0", "--port", "8000"]
