# 1. Use bookworm to avoid Debian 'testing' repo signature errors
FROM python:3.11-slim-bookworm

# 2. Install essential build tools AND git so you can commit from Jupyter
RUN apt-get update && \
    apt-get install -y build-essential git && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 3. Copy requirements and install Python packages
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 4. Install Playwright browser and dependencies (Chromium only to keep the image smaller)
RUN playwright install --with-deps chromium

# 5. Copy the rest of your project files
COPY . .

# Default command to start JupyterLab (adjust if your compose file handles this)
CMD ["jupyter", "lab", "--ip=0.0.0.0", "--port=8888", "--no-browser", "--allow-root"]