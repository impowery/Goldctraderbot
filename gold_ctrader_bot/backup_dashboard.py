#!/usr/bin/env python3
"""Backup dashboard HTML + trade log to GitHub repo.
Runs daily via cron. Commits to gold_ctrader_bot/dashboard_backup/ folder.
"""
import os
import sys
import shutil
import subprocess
from datetime import datetime, timezone, timedelta

# Paths
DASHBOARD_SRC = "/root/bots/report_latest.html"
TRADE_LOG_SRC = "/root/bots/trades_gold_ctrader.jsonl"
STATE_SRC = "/root/bots/ctrader_bot_state.json"

REPO_DIR = "/root/Goldctraderbot"
BACKUP_DIR = f"{REPO_DIR}/gold_ctrader_bot/dashboard_backup"

# Git config
GIT_USER = "impowery-bot"
GIT_EMAIL = "impowery@users.noreply.github.com"
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")


def log(msg):
    ts = datetime.now(timezone(timedelta(hours=3))).strftime("%Y-%m-%d %H:%M:%S MSK")
    print(f"[{ts}] {msg}")


def main():
    log("Starting dashboard backup...")

    # Create backup dir
    os.makedirs(BACKUP_DIR, exist_ok=True)

    # Copy files
    today = datetime.now(timezone(timedelta(hours=3))).strftime("%Y-%m-%d")

    if os.path.exists(DASHBOARD_SRC):
        dst = f"{BACKUP_DIR}/report_latest.html"
        shutil.copy2(DASHBOARD_SRC, dst)
        log(f"Copied dashboard: {dst}")
    else:
        log(f"WARNING: dashboard not found at {DASHBOARD_SRC}")

    if os.path.exists(TRADE_LOG_SRC):
        dst = f"{BACKUP_DIR}/trades_gold_ctrader.jsonl"
        shutil.copy2(TRADE_LOG_SRC, dst)
        log(f"Copied trade log: {dst}")

    if os.path.exists(STATE_SRC):
        dst = f"{BACKUP_DIR}/ctrader_bot_state.json"
        shutil.copy2(STATE_SRC, dst)
        log(f"Copied state: {dst}")

    # Git commit + push
    os.chdir(REPO_DIR)

    # Configure git
    subprocess.run(["git", "config", "user.name", GIT_USER], check=True)
    subprocess.run(["git", "config", "user.email", GIT_EMAIL], check=True)

    # Set remote with token
    remote_url = f"https://{GITHUB_TOKEN}@github.com/impowery/Goldctraderbot.git"
    subprocess.run(["git", "remote", "set-url", "origin", remote_url], check=True)

    # Add files
    subprocess.run(["git", "add", "-A"], check=True)

    # Check if there are changes
    result = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True)
    if not result.stdout.strip():
        log("No changes to commit")
        return

    # Commit
    commit_msg = f"backup: dashboard + trades {today}"
    subprocess.run(["git", "commit", "-m", commit_msg], check=True)
    log(f"Committed: {commit_msg}")

    # Push
    result = subprocess.run(["git", "push", "origin", "main"], capture_output=True, text=True)
    if result.returncode == 0:
        log("Pushed to GitHub OK")
    else:
        log(f"Push failed: {result.stderr}")

    # Reset remote URL (remove token for security)
    subprocess.run(["git", "remote", "set-url", "origin", "https://github.com/impowery/Goldctraderbot.git"], check=True)

    log("Backup complete!")


if __name__ == "__main__":
    main()
