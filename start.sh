#!/bin/bash
set -e

# Get port from environment or default to 8080
PORT=${PORT:-8080}

echo "Starting Scienthesis Backend on port $PORT..."

# Start uvicorn
exec uvicorn server:app --host 0.0.0.0 --port "$PORT"
