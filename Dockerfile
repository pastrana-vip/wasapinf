FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Dependencias necesarias para ODBC
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        curl \
        ca-certificates \
        unixodbc \
        unixodbc-dev \
    && rm -rf /var/lib/apt/lists/*

# Descargar e instalar Microsoft ODBC Driver 18 directamente
RUN curl -fSL \
    https://packages.microsoft.com/debian/12/prod/pool/main/m/msodbcsql18/msodbcsql18_18.5.1.1-1_amd64.deb \
    -o /tmp/msodbcsql18.deb \
    && ACCEPT_EULA=Y dpkg -i /tmp/msodbcsql18.deb || apt-get update && apt-get install -f -y \
    && rm -f /tmp/msodbcsql18.deb

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["gunicorn", "-w", "4", "-k", "uvicorn.workers.UvicornWorker", "main:app", "--bind", "0.0.0.0:10000"]