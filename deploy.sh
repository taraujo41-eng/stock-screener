#!/usr/bin/env bash
set -e

echo "=========================================="
echo "🚀 1. Pushing commits to GitHub (main)..."
echo "=========================================="
git push origin main

echo ""
echo "=========================================="
echo "☁️ 2. Pulling and rebuilding on Oracle Cloud..."
echo "=========================================="
ssh -i ~/Downloads/ssh-key-2026-08-11.key -o StrictHostKeyChecking=no ubuntu@129.153.190.111 "cd /home/ubuntu/stock-scanner && git pull && docker compose up -d --build"

echo ""
echo "=========================================="
echo "🔍 3. Verifying Docker containers status..."
echo "=========================================="
ssh -i ~/Downloads/ssh-key-2026-08-11.key -o StrictHostKeyChecking=no ubuntu@129.153.190.111 "docker ps"

echo ""
echo "✅ DEPLOYMENT COMPLETE AND VERIFIED!"
