FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY relay.py .
# A plataforma (Fly/Railway) define a porta pela variavel PORT.
ENV PORT=8080
EXPOSE 8080
CMD ["python", "relay.py"]
