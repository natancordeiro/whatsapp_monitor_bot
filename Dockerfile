FROM python:3.12-slim

WORKDIR /app

# Dependências
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Código-fonte
COPY . .

# Pasta para dados persistentes (banco SQLite e log)
RUN mkdir -p /data
ENV DB_PATH=/data/bot.db
ENV LOG_PATH=/data/bot.log

EXPOSE 5000

CMD ["python", "-u", "bot-prd.py"]
