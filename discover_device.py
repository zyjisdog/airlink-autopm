"""Fetch your purifier's protocol parameters (mac / authCode / companyCode / deviceType).

The control protocol needs an authCode that only the vendor app knows, and the
cloud exposes it solely through the legacy api2 endpoint, which is signed with an
RSA private key embedded in the vendor APK. Point KEY_FILE at that key, extracted
from your own installed copy of the app:

    pip install pycryptodome
    python -c "import re,zipfile;d=b''.join(zipfile.ZipFile('352life.apk').read(n) for n in zipfile.ZipFile('352life.apk').namelist() if n.endswith('.dex'));open('privkey.hex','w').write(re.search(rb'308204BD[0-9A-F]{2400}',d).group().decode())"

Usage:  python discover_device.py            # uses privkey.hex next to this file
"""
import base64
import os
import sys

import requests
from Crypto.Hash import SHA
from Crypto.PublicKey import RSA
from Crypto.Signature import pkcs1_15

import c352

HERE = os.path.dirname(os.path.abspath(__file__))


def rsa_sign(params, key):
    plain = "&".join(f"{k}={params[k]}" for k in sorted(params))
    return base64.b64encode(pkcs1_15.new(key).sign(SHA.new(plain.encode()))).decode()


def main():
    key_file = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "privkey.hex")
    if not os.path.exists(key_file):
        raise SystemExit(f"key file not found: {key_file}\nsee the docstring for how to extract it")
    key = RSA.import_key(bytes.fromhex(open(key_file).read().strip()))

    cfg = c352.load_config(os.path.join(HERE, "config.json"))
    if not cfg.get("token"):
        raise SystemExit("run login.py first (config.json has no air_token)")

    p = {
        "username": cfg["phone"],
        "token": cfg["token"],          # the air token, not the access token
        "appType": "1",
        "appVersion": "5.9.5",
    }
    p["sign"] = rsa_sign(p, key)
    r = requests.post("https://wifi.352air.com/api2/device/getDeviceList",
                      data=p, timeout=20)
    r.raise_for_status()
    body = r.json()
    if not body.get("success"):
        raise SystemExit(f"api error: {body}")

    found = body.get("data") or []
    if not found:
        raise SystemExit("no devices on this account")

    for d in found:
        print(f"{d.get('deviceName')}  mac={d.get('macAddress')} "
              f"authCode={d.get('authCode')} companyCode={d.get('companyCode')} "
              f"deviceType={d.get('deviceType')}")

    target = found[0] if len(found) == 1 else next(
        (d for d in found if d.get("macAddress", "").lower().endswith(
            cfg["purifier_mac"].replace("-", "").replace(":", "").lower())), found[0])

    cfg.update({
        "purifier_mac": target["macAddress"],
        "auth_code": target["authCode"],
        "company_code": target["companyCode"],
        "device_type": target["deviceType"],
    })
    c352.save_config(cfg, os.path.join(HERE, "config.json"))
    print("\nconfig.json updated with the device parameters above")


if __name__ == "__main__":
    main()
