FROM python:3.12-slim

WORKDIR /app

COPY services/ai-advisor/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 파이프라인 스크립트
COPY services/ai-advisor/run.py .

# Gemini 정적 컨텍스트 (system.md)
COPY scripts/context/ context/

ENTRYPOINT ["python3", "run.py"]
