#!/usr/bin/env python3
"""
proxlb-agent - runs ProxLB inside a Proxmox LXC and publishes its status
to the Proxmox VE web interface.

How it works
------------
* The agent is the scheduler: it runs ProxLB as a one-shot process
  (``python -m proxlb -j``) every ``service.schedule`` of the ProxLB config.
* After every run it writes a compact status (last run, planned/performed
  migrations, node usage) into the *Notes* (description) of its own
  container through the Proxmox API. The ProxLB GUI plugin reads it back
  with the logged-in user's normal session - no extra ports, no CORS.
* The GUI sends commands by adding tags to the container:
    proxlb-cmd-dryrun   run a dry-run (preview) now
    proxlb-cmd-run      run a real balancing now
    proxlb-active       scheduled runs balance for real (otherwise dry-run)
  The agent removes the ``proxlb-cmd-*`` tags when it picks them up.
* Local triggers also work: ``touch /var/lib/proxlb/trigger-dryrun`` or
  ``/var/lib/proxlb/trigger-run`` (see ``proxlb-ctl``).
"""

import argparse
import json
import logging
import os
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, cast

import proxmoxer
import yaml

Json = dict[str, Any]

AGENT_VERSION = "1.0.0"
STATUS_MARKER = "<!--proxlb-status:"
TAG_CMD_DRYRUN = "proxlb-cmd-dryrun"
TAG_CMD_RUN = "proxlb-cmd-run"
TAG_ACTIVE = "proxlb-active"
DESCRIPTION_LIMIT = 7800          # PVE limits the description to 8 KiB
HEARTBEAT_SECONDS = 300
MAX_MOVES = 80

log = logging.getLogger("proxlb-agent")


# --------------------------------------------------------------------------
# Helpers (pure functions, unit-tested in tests/test_contrib_agent.py)
# --------------------------------------------------------------------------
def now_ts() -> int:
    return int(time.time())


def split_tags(tags: Optional[str]) -> list[str]:
    if not tags:
        return []
    for sep in (",", " "):
        tags = tags.replace(sep, ";")
    return [t for t in tags.split(";") if t]


def schedule_seconds(cfg: Json) -> int:
    sched = (cfg.get("service") or {}).get("schedule") or {}
    interval = int(sched.get("interval", 12))
    fmt = str(sched.get("format", "hours")).lower()
    seconds = interval * 60 if fmt.startswith("min") else interval * 3600
    return max(seconds, 300)


def oneshot_config(cfg: Json) -> Json:
    """Return a copy of the ProxLB config suitable for a single run."""
    out = cast(Json, json.loads(json.dumps(cfg)))
    service = out.setdefault("service", {})
    service["daemon"] = False
    service["enable_ha"] = False
    service["delay"] = {"enable": False, "time": 1, "format": "hours"}
    return out


def extract_json(stdout: str) -> tuple[Optional[Json], list[str]]:
    """ProxLB prints its log lines and the JSON (-j) to stdout. Split them."""
    lines = stdout.splitlines()
    for idx, line in enumerate(lines):
        if line.strip() == "{":
            text = "\n".join(lines[idx:])
            try:
                data, end = json.JSONDecoder().raw_decode(text)
            except ValueError:
                continue
            rest = text[end:].splitlines()
            return data, lines[:idx] + rest
    return None, lines


def build_status(data: Optional[Json], log_lines: list[str], *, dry_run: bool,
                 trigger: str, started: int, finished: int, rc: int,
                 cfg: Json) -> Json:
    balancing = cfg.get("balancing") or {}
    status: dict[str, Any] = {
        "v": 1,
        "agent": AGENT_VERSION,
        "state": "idle",
        "dry_run": dry_run,
        "trigger": trigger,
        "started": started,
        "finished": finished,
        "duration": finished - started,
        "method": balancing.get("method", "memory"),
        "mode": balancing.get("mode", "used"),
        "balanciness": balancing.get("balanciness", 10),
        "balancing_enabled": bool(balancing.get("enable", False)),
        "nodes": {},
        "moves": [],
        "moves_total": 0,
        "guests": 0,
        "ignored": 0,
        "ok": False,
        "error": None,
    }
    errors = [ln for ln in log_lines if " - CRITICAL - " in ln or " - ERROR - " in ln]
    if data is None:
        status["error"] = (errors[-1].split(" - ", 3)[-1] if errors
                           else f"ProxLB returned no data (exit code {rc})")
        return status

    for name, node in sorted((data.get("nodes") or {}).items()):
        def pct(metric: str, _node: Json = node) -> float:
            return round(float((_node.get(metric) or {}).get("used_percent", 0.0)), 1)
        status["nodes"][name] = [pct("memory"), pct("cpu"), pct("disk"),
                                 1 if node.get("maintenance") else 0]

    moves = []
    guests = data.get("guests") or {}
    for guest in guests.values():
        if guest.get("ignore"):
            status["ignored"] += 1
        if guest.get("node_current") != guest.get("node_target"):
            moves.append([guest.get("id"), guest.get("name"), guest.get("type"),
                          guest.get("node_current"), guest.get("node_target")])
    moves.sort(key=lambda m: (str(m[3]), int(m[0] or 0)))
    status["guests"] = len(guests)
    status["moves_total"] = len(moves)
    status["moves"] = moves[:MAX_MOVES]
    status["ok"] = rc == 0 and not errors
    if errors:
        status["error"] = errors[-1].split(" - ", 3)[-1]
    return status


def fmt_time(ts: Optional[int]) -> str:
    if not ts:
        return "-"
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def render_description(status: Json) -> str:
    """Markdown for the container Notes + machine readable JSON comment."""
    def build(st: Json) -> str:
        mode = "dry-run (preview)" if st.get("dry_run") else "balancing"
        if st.get("state") == "running":
            result = "running ..."
        elif not st.get("finished"):
            result = "no run yet"
        elif st.get("ok"):
            verb = "planned" if st.get("dry_run") else "performed"
            result = f"OK - {st.get('moves_total', 0)} migration(s) {verb}"
        else:
            result = f"FAILED - {st.get('error') or 'see journal'}"
        auto = "on" if st.get("auto") else "off (scheduled runs are dry-run only)"
        md = [
            "**ProxLB** - managed by proxlb-agent. This note is overwritten automatically.",
            "",
            "| | |",
            "|---|---|",
            f"| Last run | {fmt_time(st.get('finished') or st.get('started'))} ({mode}, {st.get('trigger', '-')}) |",
            f"| Result | {result} |",
            f"| Automatic balancing | {auto} |",
            f"| Next scheduled run | {fmt_time(st.get('next_run'))} |",
            f"| Method | {st.get('method')} / {st.get('mode')} (balanciness {st.get('balanciness')}) |",
            "",
            "Open **Datacenter → ProxLB** for the full view.",
            "",
        ]
        raw = json.dumps(st, separators=(",", ":"), ensure_ascii=False)
        raw = raw.replace(">", "\\u003e")  # never close the HTML comment early
        return "\n".join(md) + STATUS_MARKER + raw + "-->"

    st = dict(status)
    text = build(st)
    while len(text.encode("utf-8")) > DESCRIPTION_LIMIT and st.get("moves"):
        st["moves"] = st["moves"][: max(len(st["moves"]) // 2, 0)]
        text = build(st)
    if len(text.encode("utf-8")) > DESCRIPTION_LIMIT:
        st["nodes"] = {}
        text = build(st)
    return text


# --------------------------------------------------------------------------
# Proxmox API access
# --------------------------------------------------------------------------
class Pve:
    def __init__(self, cfg: Json) -> None:
        self.api_cfg = cfg.get("proxmox_api") or {}
        self.api: Any = None

    def connect(self) -> Any:
        if self.api is not None:
            return self.api
        hosts = self.api_cfg.get("hosts") or []
        last_err: Optional[Exception] = None
        for host in hosts:
            host_s = str(host)
            port = 8006
            if host_s.startswith("["):  # [v6]:port
                h, _, p = host_s[1:].partition("]")
                host_s, port = h, int(p.lstrip(":") or 8006)
            elif host_s.count(":") == 1:
                host_s, p = host_s.split(":")
                port = int(p)
            kwargs: dict[str, Any] = {
                "port": port,
                "verify_ssl": bool(self.api_cfg.get("ssl_verification", True)),
                "timeout": int(self.api_cfg.get("timeout", 10)),
            }
            if self.api_cfg.get("token_id"):
                kwargs.update(user=self.api_cfg["user"],
                              token_name=self.api_cfg["token_id"],
                              token_value=self.api_cfg["token_secret"])
            else:
                kwargs.update(user=self.api_cfg["user"],
                              password=self.api_cfg.get("pass") or self.api_cfg.get("password"))
            try:
                api = proxmoxer.ProxmoxAPI(host_s, **kwargs)
                api.version.get()
                self.api = api
                return api
            except Exception as err:  # noqa: BLE001 - try the next host
                last_err = err
                log.warning("Proxmox API %s not usable: %s", host, err)
        raise RuntimeError(f"no Proxmox API host reachable: {last_err}")

    def reset(self) -> None:
        self.api = None

    def find_ct(self, ctid: int) -> tuple[str, list[str]]:
        api = self.connect()
        for res in api.cluster.resources.get(type="vm"):
            if res.get("type") == "lxc" and int(res.get("vmid")) == ctid:
                return res["node"], split_tags(res.get("tags"))
        raise RuntimeError(f"container {ctid} not found in cluster resources")

    def set_tags(self, node: str, ctid: int, tags: list[str]) -> None:
        cfg = self.connect().nodes(node).lxc(ctid).config
        if tags:
            cfg.put(tags=";".join(tags))
        else:
            cfg.put(delete="tags")

    def set_description(self, node: str, ctid: int, text: str) -> None:
        self.connect().nodes(node).lxc(ctid).config.put(description=text)


# --------------------------------------------------------------------------
# Agent
# --------------------------------------------------------------------------
class Agent:
    def __init__(self, agent_cfg: Json) -> None:
        self.ctid = int(agent_cfg["ctid"])
        self.config_path = Path(agent_cfg.get("proxlb_config", "/etc/proxlb/proxlb.yaml"))
        self.python = agent_cfg.get("python", sys.executable)
        self.poll = int(agent_cfg.get("poll_seconds", 10))
        self.run_timeout = int(agent_cfg.get("run_timeout_seconds", 4 * 3600))
        self.state_dir = Path(agent_cfg.get("state_dir", "/var/lib/proxlb"))
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.cfg = self.load_cfg()
        self.pve = Pve(self.cfg)
        self.status: Json = self.load_last_status()
        self.next_run = now_ts() + schedule_seconds(self.cfg)
        self.last_publish = 0
        self.published_text = ""
        self.stop = False
        self.startup_run_done = False

    # -- persistence -------------------------------------------------------
    def load_cfg(self) -> Json:
        with open(self.config_path, encoding="utf-8") as fh:
            return cast(Json, yaml.safe_load(fh) or {})

    def load_last_status(self) -> Json:
        try:
            return cast(Json, json.loads((self.state_dir / "status.json").read_text()))
        except (OSError, ValueError):
            return {"v": 1, "agent": AGENT_VERSION, "state": "idle", "finished": 0}

    def save_status(self) -> None:
        tmp = self.state_dir / "status.json.tmp"
        tmp.write_text(json.dumps(self.status, indent=2))
        tmp.replace(self.state_dir / "status.json")

    # -- publishing ---------------------------------------------------------
    def publish(self, node: str, auto: bool, force: bool = False) -> None:
        self.status["auto"] = auto
        self.status["next_run"] = self.next_run
        self.status["hb"] = now_ts()
        self.status["ctid"] = self.ctid
        text = render_description(self.status)
        # Compare without the heartbeat to avoid needless writes.
        body = text.split(STATUS_MARKER)[0]
        changed = body != self.published_text.split(STATUS_MARKER)[0]
        if not (force or changed or now_ts() - self.last_publish >= HEARTBEAT_SECONDS):
            return
        try:
            self.pve.set_description(node, self.ctid, text)
            self.published_text = text
            self.last_publish = now_ts()
        except Exception as err:  # noqa: BLE001
            log.error("could not publish status to container notes: %s", err)
            self.pve.reset()

    # -- running ProxLB -----------------------------------------------------
    def run_proxlb(self, dry_run: bool, trigger: str, node: str, auto: bool) -> None:
        self.cfg = self.load_cfg()
        started = now_ts()
        self.status.update(state="running", started=started, dry_run=dry_run, trigger=trigger)
        self.publish(node, auto, force=True)
        log.info("starting ProxLB run (dry_run=%s, trigger=%s)", dry_run, trigger)

        fd, tmp_cfg = tempfile.mkstemp(prefix="proxlb-oneshot-", suffix=".yaml")
        try:
            with os.fdopen(fd, "w") as fh:
                yaml.safe_dump(oneshot_config(self.cfg), fh)
            cmd = [self.python, "-m", "proxlb", "-c", tmp_cfg, "-j"]
            if dry_run:
                cmd.append("-d")
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True,
                                      timeout=self.run_timeout, check=False)
                stdout, rc = proc.stdout, proc.returncode
                stderr = proc.stderr
            except subprocess.TimeoutExpired as err:
                stdout = err.stdout.decode() if isinstance(err.stdout, bytes) else (err.stdout or "")
                stderr, rc = "run timed out", -1
        finally:
            os.unlink(tmp_cfg)

        data, log_lines = extract_json(stdout)
        if stderr.strip():
            log_lines += [f"x - ProxLB - ERROR - {ln}" for ln in stderr.strip().splitlines()[-5:]]
        for line in log_lines:
            print(line, flush=True)  # into the journal of proxlb-agent

        (self.state_dir / "last-run.log").write_text("\n".join(log_lines) + "\n")
        if data is not None:
            (self.state_dir / "last-run.json").write_text(json.dumps(data, indent=2))

        self.status = build_status(data, log_lines, dry_run=dry_run, trigger=trigger,
                                   started=started, finished=now_ts(), rc=rc, cfg=self.cfg)
        self.save_status()
        log.info("ProxLB run finished: ok=%s moves=%s", self.status["ok"], self.status["moves_total"])

    # -- main loop ------------------------------------------------------------
    def take_local_trigger(self) -> Optional[str]:
        for name in ("run", "dryrun"):
            path = self.state_dir / f"trigger-{name}"
            if path.exists():
                try:
                    path.unlink()
                except OSError:
                    pass
                return name
        return None

    def tick(self) -> None:
        try:
            node, tags = self.pve.find_ct(self.ctid)
        except Exception as err:  # noqa: BLE001
            log.error("Proxmox API not usable: %s", err)
            self.pve.reset()
            return
        auto = TAG_ACTIVE in tags

        command = None
        if TAG_CMD_RUN in tags:
            command = "run"
        elif TAG_CMD_DRYRUN in tags:
            command = "dryrun"
        if command:
            try:
                self.pve.set_tags(node, self.ctid,
                                  [t for t in tags if t not in (TAG_CMD_RUN, TAG_CMD_DRYRUN)])
            except Exception as err:  # noqa: BLE001
                log.error("could not remove command tag, ignoring command: %s", err)
                return
            trigger = "gui"
        else:
            command = self.take_local_trigger()
            trigger = "cli"

        ran = True
        if command:
            self.run_proxlb(command == "dryrun", trigger, node, auto)
        elif not self.startup_run_done:
            # First run after start is always a preview so the GUI has data,
            # without surprising migrations after a reboot.
            self.run_proxlb(True, "startup", node, auto)
        elif now_ts() >= self.next_run:
            self.run_proxlb(not auto, "schedule", node, auto)
            self.next_run = now_ts() + schedule_seconds(self.cfg)
        else:
            ran = False
        self.startup_run_done = True
        self.publish(node, auto, force=ran)

    def loop(self) -> None:
        signal.signal(signal.SIGTERM, self._sigterm)
        signal.signal(signal.SIGINT, self._sigterm)
        log.info("proxlb-agent %s started for CT %s, schedule every %ss",
                 AGENT_VERSION, self.ctid, schedule_seconds(self.cfg))
        while not self.stop:
            self.tick()
            for _ in range(self.poll):
                if self.stop:
                    break
                time.sleep(1)

    def _sigterm(self, *_: Any) -> None:
        self.stop = True


def main() -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").strip().split("\n")[0])
    parser.add_argument("-c", "--config", default="/etc/proxlb/agent.yaml")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    with open(args.config, encoding="utf-8") as fh:
        agent_cfg = yaml.safe_load(fh) or {}
    Agent(agent_cfg).loop()


if __name__ == "__main__":
    main()
