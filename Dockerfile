FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PYTHONUTF8=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY agent_sav agent_sav
COPY interface interface

RUN useradd --system --no-create-home sav
USER sav

EXPOSE 8000
CMD ["python", "-m", "agent_sav", "web", "--public", "--host", "0.0.0.0", "--port", "8000"]
