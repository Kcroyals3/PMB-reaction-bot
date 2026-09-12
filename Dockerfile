FROM python:3.11-slim

WORKDIR /app

# Copy dependency list first for better layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the bot code
COPY . .

# DISCORD_TOKEN is provided at runtime via environment variable (Portainer/Stack)
CMD ["python", "bot.py"]
