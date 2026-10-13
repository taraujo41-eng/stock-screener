# Deployment Rule

Whenever you make any changes to this repository:
1. Commit the changes and push to GitHub:
   ```bash
   git add <modified files>
   git commit -m "<descriptive message>"
   git push origin main
   ```
2. Deploy the changes to the Oracle Cloud Server:
   ```bash
   ssh -i ~/Downloads/ssh-key-2026-08-11.key -o StrictHostKeyChecking=no ubuntu@129.153.190.111 "cd /home/ubuntu/stock-scanner && git pull && docker compose up -d --build"
   ```
3. Confirm that containers (`stock_scanner_web` and `stock_scanner_bot`) restarted successfully using `docker ps`.
