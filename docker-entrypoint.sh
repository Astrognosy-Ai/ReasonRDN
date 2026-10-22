#!/bin/bash
set -e

# Start the local memory node in the background
echo "Starting ReasonRDN Private Node on port 8765..."
# Inside the container the node must listen on all interfaces so Docker can
# forward the port. docker-compose.yml publishes it on 127.0.0.1 only.
rdn-node --host 0.0.0.0 &
NODE_PID=$!

# Define cleanup function to terminate the node on exit
cleanup() {
    echo "Stopping ReasonRDN Private Node (PID $NODE_PID)..."
    kill "$NODE_PID" 2>/dev/null || true
}
trap cleanup EXIT SIGINT SIGTERM

# Start the dashboard in the foreground
echo "Starting ReasonRDN Dashboard on port 8501..."
# --network binds inside the container; docker-compose.yml publishes the port
# on 127.0.0.1 only, so the dashboard is still reachable from this computer only.
rdn-dash --network --port 8501
