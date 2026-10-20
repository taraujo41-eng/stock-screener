# Antigravity Pair Programming Instructions

## Mandatory Automatic Deployment Rule
Whenever you make ANY code changes, bug fixes, features, configuration updates, or UI edits in this repository:

1. **Commit and push to GitHub (`main`) immediately**:
   ```bash
   git add <modified files>
   git commit -m "<descriptive message>"
   git push origin main
   ```

2. **Deploy immediately to the Oracle Cloud Server**:
   ```bash
   ssh -i ~/Downloads/ssh-key-2026-08-11.key -o StrictHostKeyChecking=no ubuntu@129.153.190.111 "cd /home/ubuntu/stock-scanner && git pull && docker compose up -d --build"
   ```

3. **Verify containers are healthy**:
   Confirm both `stock_scanner_web` and `stock_scanner_bot` are running (`Up`) with `docker ps`.
