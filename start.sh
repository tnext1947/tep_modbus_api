#!/bin/bash
# Start server in background
python server_sim.py &

# Wait for server to be ready before starting client
sleep 3

# Start client as main process (PID 1 — receives Docker signals)
exec python cilent.py
