FROM python:3.11-slim

WORKDIR /app

# python:*-slim ships with no fonts whatsoever. Without these, Pillow silently
# falls back to a tiny fixed-size bitmap face: every requested font size is
# ignored and anything outside basic Latin renders as a tofu box.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        fonts-dejavu-core \
        fonts-noto-color-emoji \
    && rm -rf /var/lib/apt/lists/*

# Copy dependency list first for better layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the bot code
COPY . .

# DISCORD_TOKEN is provided at runtime via environment variable (Portainer/Stack)
CMD ["python", "bot.py"]
