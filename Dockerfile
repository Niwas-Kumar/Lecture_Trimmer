FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY *.py ./
COPY lecture_trimmer/ ./lecture_trimmer/
COPY desktop_runtime/ ./desktop_runtime/
COPY vendor/ ./vendor/
COPY assets/ ./assets/
ENV GRADIO_ANALYTICS_ENABLED=False
EXPOSE 7860
CMD ["python", "app.py", "--auth", "google-sheet", "--host", "0.0.0.0", "--port", "7860", "--no-browser"]
