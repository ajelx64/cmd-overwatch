# Claude Overwatch — Start the dashboard server
# Open http://localhost:8765 in your browser after running this

uvicorn server:app --host 0.0.0.0 --port 8765 --reload
