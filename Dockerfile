# Use an official Python runtime as the base image
FROM python:3.8-slim

# Set the working directory in the container
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget \
    unzip \
    gnupg \
    ca-certificates \
    fonts-liberation \
    libnss3 \
    lsb-release \
    jq \
    && rm -rf /var/lib/apt/lists/*

# Add Google's signing key and Chrome repository
RUN wget -q -O - https://dl.google.com/linux/linux_signing_key.pub | apt-key add - \
    && echo "deb [arch=amd64] http://dl.google.com/linux/chrome/deb/ stable main" > /etc/apt/sources.list.d/google-chrome.list

# Install Google Chrome
RUN apt-get update && apt-get install -y google-chrome-stable

# Install ChromeDriver using the new method
RUN GOOGLE_CHROME_VERSION=$(google-chrome --version | sed 's/Google Chrome //') && \
    GOOGLE_CHROME_MAJOR_VERSION=$(echo $GOOGLE_CHROME_VERSION | cut -d '.' -f 1) && \
    KNOWN_VERSIONS_URL="https://googlechromelabs.github.io/chrome-for-testing/known-good-versions-with-downloads.json" && \
    CHROMEDRIVER_URL=$(wget -qO- $KNOWN_VERSIONS_URL | \
        jq -r --arg major "$GOOGLE_CHROME_MAJOR_VERSION" \
            '.versions[] | select(.version | startswith($major + ".")) | .downloads.chromedriver[] | select(.platform == "linux64") | .url' | head -n1) && \
    wget -O /tmp/chromedriver.zip "$CHROMEDRIVER_URL" && \
    unzip /tmp/chromedriver.zip -d /usr/local/bin/ && \
    rm /tmp/chromedriver.zip

# Copy the application code into the container, excluding config.yaml
COPY . /app
RUN rm -f /app/config.yaml  # Ensure config.yaml is not included

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Expose the port the app runs on
EXPOSE 5000

# Define the command to run the application
CMD ["python", "app.py"]
