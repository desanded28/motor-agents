# Uses the official Playwright image — ships Chromium + OS deps pre-installed.
FROM mcr.microsoft.com/playwright/python:v1.48.0-jammy

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PORT=8001
EXPOSE 8001

# Default: start the web dashboard. Override CMD to run a single agent:
#   docker run ... python agent_hunter.py --no-llm "M340i"
CMD ["python", "web/app.py"]
