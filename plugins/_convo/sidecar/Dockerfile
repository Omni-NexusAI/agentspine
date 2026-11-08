FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY sidecar/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt && useradd --uid 10001 --create-home convo
COPY helpers/convo_contract.py /app/convo/helpers/convo_contract.py
COPY sidecar /app/convo/sidecar
USER 10001
EXPOSE 8090
CMD ["python", "-m", "uvicorn", "convo.sidecar.app:app", "--host", "0.0.0.0", "--port", "8090", "--no-access-log"]
