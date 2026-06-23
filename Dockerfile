FROM python:3.12-slim

# Install system dependencies (curl, gnupg for Node, chromium, and dependencies for headless Chrome)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    gnupg \
    ca-certificates \
    chromium \
    && rm -rf /var/lib/apt/lists/*

# Install Node.js 18 and npm
RUN curl -fsSL https://deb.nodesource.com/setup_18.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# Install Lighthouse CLI globally
RUN npm install -g lighthouse

# Set environment variables for Chromium
ENV CHROME_PATH=/usr/bin/chromium
# We also disable sandbox for Docker run safety where user namespaces aren't set up
ENV LIGHTHOUSE_CHROMIUM_FLAGS="--no-sandbox --headless --disable-gpu"

# Install uv
RUN pip install --no-cache-dir uv

# Set working directory to /app
WORKDIR /app

# Copy dependency files
COPY pyproject.toml README.md uv.lock* ./

# Install python dependencies via uv sync
RUN uv sync --frozen

# Copy project files
COPY . .

# Copy .env.example to .env
RUN cp workflows_sequential/.env.example workflows_sequential/.env

# Expose port 8080
EXPOSE 8080

# Run the playground
CMD ["uv", "run", "agents-cli", "playground", "--host", "0.0.0.0", "--port", "8080"]