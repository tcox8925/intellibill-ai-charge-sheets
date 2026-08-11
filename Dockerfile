# python:3.11-slim is Debian-based, which gives us apt-get for poppler-utils.
# (The Azure App Service "Python runtime" option is not Debian-accessible to us —
# this image is what replaces it.)
FROM python:3.11-slim

# poppler-utils -> pdftoppm, used by v1_computer_vision/render.py (locked-template
#   renderer contract; do not swap for another engine without recalibrating).
# libglib2.0-0/libsm6/libxext6/libxrender1/libgl1 -> runtime shared libs opencv-python
#   needs (libGL.so.1 etc.) that are normally present on a desktop OS but missing
#   from a minimal Debian image.
RUN apt-get update && apt-get install -y --no-install-recommends \
        poppler-utils \
        libglib2.0-0 \
        libsm6 \
        libxext6 \
        libxrender1 \
        libgl1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy requirements first so `docker build` can cache the pip install layer
# across rebuilds that only touch application code.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

CMD ["gunicorn", "api:app", "-k", "uvicorn.workers.UvicornWorker", "--bind", "0.0.0.0:8000", "--timeout", "600"]
