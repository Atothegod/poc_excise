#!/bin/bash
echo "Waiting for Prefect Server to start..."
sleep 10
echo "Starting Prefect Worker..."
exec prefect worker start --pool "default-pool"