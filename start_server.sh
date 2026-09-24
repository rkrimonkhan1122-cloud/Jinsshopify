
#!/bin/bash
# Railway start script for the Shopify checker Flask app.
# Railway injects the PORT environment variable; fall back to 5000 for local runs.
export PORT=${PORT:-5000}

echo "============================================"
echo " Starting Shopify Checker on port: $PORT"
echo " Working directory: $(pwd)"
echo " Python: $(python --version 2>&1)"
echo "============================================"

# Gunicorn serves the Flask `app` object from app.py.
# - sync worker + threads: the app builds a fresh asyncio event loop per
#   request inside the /shopify handler, so sync workers are the safe choice.
# - long timeout: card-processing + captcha handling can take a while.
exec gunicorn app:app \
    --bind "0.0.0.0:${PORT}" \
    --workers 1 \
    --threads 8 \
    --timeout 300 \
    --graceful-timeout 30 \
    --keep-alive 5 \
    --access-logfile - \
    --error-logfile - \
    --log-level info
