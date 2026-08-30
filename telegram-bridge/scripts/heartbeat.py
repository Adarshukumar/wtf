#!/usr/bin/env python3
"""One-shot heartbeat / server-health report to your Telegram.

Run it from cron every N minutes, or from a systemd timer:

    */5 * * * *  cd /opt/telegram-bridge && .venv/bin/python scripts/heartbeat.py

It reports uptime, load average and (if psutil is installed) CPU, RAM and
disk usage — then hands the message to push.py, which uses the relay with
automatic direct-send fallback.
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from push import notify  # noqa: E402


def _loadavg() -> str:
    try:
        return " ".join(f"{x:.2f}" for x in os.getloadavg())
    except (AttributeError, OSError):
        return "n/a"


def _psutil_block() -> str:
    try:
        import psutil
    except ImportError:
        return ""

    cpu = psutil.cpu_percent(interval=0.3)
    mem = psutil.virtual_memory().percent
    disk = psutil.disk_usage("/").percent
    return (
        f"CPU: <code>{cpu}%</code> | "
        f"RAM: <code>{mem}%</code> | "
        f"Disk: <code>{disk}%</code>\n"
    )


def main() -> int:
    host = os.uname().nodename if hasattr(os, "uname") else "unknown"
    with open("/proc/uptime") as fh:
        uptime_s = float(fh.read().split()[0])
    h, rem = divmod(int(uptime_s), 3600)
    m, s = divmod(rem, 60)

    text = (
        "🫀 <b>Server heartbeat</b>\n"
        f"Host: <code>{host}</code>\n"
        f"Uptime: <code>{h}h {m}m {s}s</code>\n"
        f"Load: <code>{_loadavg()}</code>\n"
        + _psutil_block()
        + f"Sent at: <code>{time.strftime('%Y-%m-%d %H:%M:%S')}</code>"
    )
    return 0 if notify(text, parse_mode="HTML") else 1


if __name__ == "__main__":
    sys.exit(main())
