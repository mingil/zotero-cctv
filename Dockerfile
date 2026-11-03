# 1. Lightweight and stable Python 3.11 slim base image
FROM python:3.11-slim

# 2. Immediate unbuffered logging & timezone configuration (default: Asia/Seoul)
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Seoul

RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# 3. Set working directory inside container
WORKDIR /app

# 4. Install dependencies first to leverage Docker layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 5. Copy application source code and prepare persistent data directory
COPY cctv_bot.py .
RUN mkdir -p /app/data && chmod 777 /app/data

# 6. Built-in container healthcheck
HEALTHCHECK --interval=5m --timeout=10s --start-period=15s --retries=3 \
    CMD python cctv_bot.py --status > /dev/null || exit 1

# 7. Start the Zotero CCTV bot
CMD ["python", "cctv_bot.py"]
