#!/bin/bash
echo "Starting Jupyter Lab..."
exec jupyter lab --ip=0.0.0.0 --port=8888 --no-browser --allow-root --ServerApp.token='' --ServerApp.password=''