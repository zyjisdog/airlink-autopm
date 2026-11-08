"""One-time SMS login: writes access/refresh/air tokens into config.json.

Usage:  python login.py 13100000000
"""
import getpass
import json
import os
import sys

import c352

HERE = os.path.dirname(os.path.abspath(__file__))
CFG_PATH = os.path.join(HERE, "config.json")


def main():
    phone = sys.argv[1] if len(sys.argv) > 1 else input("phone: ").strip()
    api = c352.Api()
    print("sending code:", api.send_code(phone))
    code = getpass.getpass("sms code: ").strip()

    r = api.login(phone, code)
    data = r.get("data") or {}
    if not data.get("air_token"):
        raise SystemExit(f"login failed: {r}")

    cfg = json.load(open(CFG_PATH, encoding="utf-8")) if os.path.exists(CFG_PATH) else {}
    cfg.update({
        "phone": phone,
        "token": data["air_token"],
        "access_token": data.get("access_token", ""),
        "refresh_token": data.get("refresh_token", ""),
        "expires_at": data.get("expires_at", ""),
    })
    c352.save_config(cfg, CFG_PATH)
    print("saved. expires_at =", cfg["expires_at"])


if __name__ == "__main__":
    main()
