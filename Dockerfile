
FROM python:3.12-slim

WORKDIR /app

# Install FFmpeg and Deno installation tools
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       ffmpeg \
       curl \
       ca-certificates \
       unzip \
    && rm -rf /var/lib/apt/lists/*

# Install Deno JavaScript runtime
RUN curl -fsSL https://deno.land/install.sh | sh

# Add Deno to PATH
ENV PATH="/root/.deno/bin:${PATH}"

# Copy and install Python dependencies
COPY requirements.txt .

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# Copy bot files
COPY . .

CMD ["python", "bot.py"]
