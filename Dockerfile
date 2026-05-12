FROM python:3.10-slim

RUN apt-get update && apt-get install -y \
    nmap \
    net-tools \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server_sim.py cilent.py start.sh ./
RUN chmod +x start.sh

EXPOSE 5000

CMD ["./start.sh"]
