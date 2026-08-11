#!/bin/bash
# Oracle Cloud Ubuntu Setup Script for Stock Scanner
set -e

echo "🚀 Starting Oracle Cloud Instance Setup..."

# 1. Update system packages
sudo apt update && sudo apt upgrade -y
sudo apt install -y curl git ufw iptables-persistent docker.io docker-compose-v2

# 2. Add current user to Docker group
sudo usermod -aG docker $USER

# 3. Configure firewall (Oracle Ubuntu instances use iptables by default)
echo "🔓 Configuring firewall rules for HTTP (80), HTTPS (443), and App (5000)..."
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 80 -j ACCEPT
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 443 -j ACCEPT
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 5000 -j ACCEPT
sudo netfilter-persistent save

# 4. Enable Docker service
sudo systemctl enable docker
sudo systemctl start docker

echo ""
echo "✅ Oracle Cloud Instance configured successfully!"
echo "➡️ Log out and log back in (or run 'newgrp docker') for Docker permissions to apply."
echo "➡️ Next: Run 'docker compose up -d --build' inside your project directory."
