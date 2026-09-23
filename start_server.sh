16
#!/bin/bash
# Start the Flask dev server for testing in a detached session
pkill -f "app:app" 2>/dev/null
pkill -f "app.py" 2>/dev/null
sleep 1

cd /home/z/my-project/download
PORT=5555 setsid python app.py > /tmp/flask.log 2>&1 < /dev/null &
echo "Started PID=$!"
sleep 4
echo "--- log ---"
tail -10 /tmp/flask.log
echo "--- health ---"
curl -s http://127.0.0.1:5555/health
echo ""
