"""PM2.5 linkage: Bohu air monitor -> 352 X83 purifier.

Rules (all thresholds configurable in config.json):
  effective = max(bohu pm25, purifier pm25)      # dual sensor
  effective >  150  -> gear 6 (max)
  effective >= 100  -> gear 4
  effective <  125 and currently gear 6 -> gear 4
  effective <  80  and currently gear 4 -> gear 2
  otherwise         -> hold

Notes:
  * upshift is immediate; downshift needs both sensors low
  * a gear must have run MIN_HOLD seconds before it may downshift
  * the purifier is never powered on or off by this script
"""
import datetime
import json
import logging
import os
import threading
import time

import requests

import c352

HERE = os.path.dirname(os.path.abspath(__file__))
CFG = c352.load_config(os.path.join(HERE, "config.json"))

BOHU_URL = "http://www.bohu-tech.com/iot/api/latest"
T = CFG.get("thresholds", {})
UP4 = T.get("up_4", 100)        # >= 100 -> gear 4
UPMAX = T.get("up_max", 150)    # > 150  -> gear 6
DOWN4 = T.get("down_4", 125)    # < 125  -> leave gear 6 for 4
DOWN2 = T.get("down_2", 80)     # < 80   -> leave gear 4 for 2
INTERVAL = CFG.get("interval_s", 60)
MIN_HOLD = CFG.get("min_hold_s", 300)
G2, G4, GMAX = 1, 3, 5          # 0-based indices into GEARS -> values 2/4/6
MODE_MANUAL = 2                 # MODES[] index

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(os.path.join(HERE, "autopm.log"), encoding="utf-8"),
              logging.StreamHandler()])
log = logging.getLogger("autopm")


def bohu_pm25():
    # trust_env=False: a system-wide proxy must not hijack this plain HTTP call
    s = requests.Session()
    s.trust_env = False
    try:
        r = s.get(BOHU_URL, params={"device_id": CFG["bohu_device_id"]},
                  headers={"Cookie": f"uid={CFG['bohu_uid']}"}, timeout=15)
        r.raise_for_status()
        j = r.json()
    finally:
        s.close()
    pm = j["pm25"]
    ts = j.get("ts", "")
    if ts:
        try:
            t = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
            age = (datetime.datetime.now(datetime.timezone.utc) - t).total_seconds()
            if age > 600:
                raise ValueError(f"stale Bohu data {ts} (age {age:.0f}s)")
        except ValueError:
            raise
        except Exception:
            pass
    return pm


def refresh_tokens():
    """Rotate tokens. Non-fatal: refresh_token is single-use, so a consumed one
    must not block control while the stored air_token is still alive."""
    try:
        r = c352.Api(CFG.get("access_token")).refresh(CFG["refresh_token"])
        data = r.get("data") or {}
        if not data.get("air_token"):
            log.warning("token refresh returned no air_token: %s", r)
            return False
        CFG["token"] = data["air_token"]
        CFG["access_token"] = data.get("access_token", CFG["access_token"])
        CFG["refresh_token"] = data.get("refresh_token", CFG["refresh_token"])
        CFG["expires_at"] = data.get("expires_at", CFG.get("expires_at", ""))
        c352.save_config(CFG, os.path.join(HERE, "config.json"))
        log.info("tokens refreshed, expires_at=%s", CFG["expires_at"])
        return True
    except Exception as e:
        log.warning("token refresh failed (%r), using stored air_token", e)
        return False


def build_client():
    dev = c352.Device(
        mac=int(CFG["purifier_mac"].replace("-", "").replace(":", ""), 16),
        auth_code=int(CFG["auth_code"], 16),
        company_code=int(CFG["company_code"], 16),
        device_type=int(CFG["device_type"], 16))
    refresh_tokens()
    cli = c352.Tcp352(CFG["token"], dev)
    try:
        cli.connect()
    except c352.ProtocolError as e:
        if "join refused" not in str(e):
            raise
        log.warning("join refused, refreshing token and retrying")
        refresh_tokens()
        dev.idx = 0
        cli = c352.Tcp352(CFG["token"], dev)
        cli.connect()
    return cli


class Runner:
    def __init__(self):
        self.cli = None
        self.level = None   # 0=gear2, 1=gear4, 2=gear6; None until synced
        self.level_since = 0.0
        self.fail_count = 0

    def ensure(self):
        if self.cli is None:
            self.cli = build_client()
            threading.Thread(target=self.cli.recv_loop, daemon=True).start()
            self.cli.got_state.wait(10)

    def control(self, pm_bohu, pm_dev):
        d = self.cli.dev
        if self.level is None:
            self.level = {2: 0, 4: 1, 6: 2}.get(d.gear, 0)
            self.level_since = time.time()
        cur = self.level
        # dual sensor: max() means either sensor alone can upshift,
        # while a downshift requires both sensors to be low
        eff = max(pm_bohu, pm_dev)
        target = cur
        if eff > UPMAX:
            target = 2
        elif eff >= UP4:
            target = max(target, 1)
        if target == cur:
            if cur == 2 and eff < DOWN4:
                target = 1
            elif cur == 1 and eff < DOWN2:
                target = 0
            if target < cur and time.time() - self.level_since < MIN_HOLD:
                target = cur
        if target != cur:
            names = ("gear2", "gear4", "gear6")
            src = "bohu" if pm_bohu >= pm_dev else "purifier"
            log.info("pm25 bohu=%s purifier=%s (driven by %s): %s -> %s",
                     pm_bohu, pm_dev, src, names[cur], names[target])
            self.cli.set_mode(MODE_MANUAL)   # keep gear sticky; never touches power
            self.cli.set_speed((G2, G4, GMAX)[target])
            self.cli.query_state()
            self.level = target
            self.level_since = time.time()

    def run(self):
        log.info("autopm start: >=%s->g4 >%s->g6 <%s->g4 <%s->g2 hold=%ss interval=%ss",
                 UP4, UPMAX, DOWN4, DOWN2, MIN_HOLD, INTERVAL)
        while True:
            try:
                pm = bohu_pm25()
                self.ensure()
                d = self.cli.dev
                # poll every cycle: after a power cycle the device stops pushing
                # events, and a stale reading would corrupt the dual-sensor decision
                self.cli.query_state()
                time.sleep(2)
                age = time.time() - (d.last_state_ts or 0)
                if age > 180:
                    log.warning("device state stale (%.0fs) -> reconnecting", age)
                    self.cli.close()
                    self.cli = None
                    self.level = None
                    self.ensure()
                    d = self.cli.dev
                    time.sleep(2)
                pm_dev = d.pm25 if d.pm25 is not None else 0
                self.control(pm, pm_dev)
                log.info("pm25 bohu=%s purifier=%s eff=%s | purifier on=%s gear=%s mode=%s level=%s",
                         pm, pm_dev, max(pm, pm_dev), d.on, d.gear, d.mode, self.level)
                self.fail_count = 0
            except Exception as e:
                self.fail_count += 1
                log.error("cycle failed (%d): %r", self.fail_count, e)
                try:
                    if self.cli:
                        self.cli.close()
                except Exception:
                    pass
                self.cli = None
                self.level = None
            time.sleep(INTERVAL)


if __name__ == "__main__":
    Runner().run()
