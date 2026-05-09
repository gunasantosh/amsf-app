#!/bin/bash

# Run the AMSF FastAPI application using uvicorn with 2 workers for optimal performance on the home server.
# This uses 'uv run' to execute uvicorn from the managed environment.

echo "Starting AMSF Application..."
uv run uvicorn main:app --host 0.0.0.0 --port 8000 --workers 2

# To run this in the background, you can use:
# nohup ./run.sh > amsf.log 2>&1 &
