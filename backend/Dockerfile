FROM python:3.12-slim

WORKDIR /app

# requirements.txt includes requirements-base.txt (-r), so both must be present.
COPY requirements.txt requirements-base.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
