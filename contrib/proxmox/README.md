# ProxLB as an LXC container + Proxmox web-interface plugin

This directory deploys ProxLB in its own **LXC container** on Proxmox VE and
adds a **ProxLB** tab to the Proxmox web interface (*Datacenter → ProxLB*), so
you can see at a glance how balanced the cluster is and what ProxLB plans to
do or has done.

```
┌──────────── Proxmox VE web interface (any node) ────────────┐
│ Datacenter → ProxLB                                         │
│  [Preview (dry-run)] [Rebalance now] [Automatic: off]       │
│  Status   CT 120 running · last run OK, 3 migrations planned│
│  Nodes    pve1  CPU ███░░ 41%   Mem ██████░ 78%   out 3     │
│           pve2  CPU █░░░░ 12%   Mem ██░░░░░ 31%   in  3     │
│  Planned  101 web01  VM  pve1 → pve2  ...                   │
└────────────────▲───────────────────────────┬────────────────┘
   reads Notes   │                           │ adds tags
   of the CT     │      Proxmox API          │ proxlb-cmd-*
┌────────────────┴───────────────────────────▼────────────────┐
│ LXC "proxlb" (tag proxlb, plb_ignore_vm)                    │
│   proxlb-agent  → runs  python -m proxlb -j [-d]            │
│                 → writes the status into the CT Notes       │
└─────────────────────────────────────────────────────────────┘
```

No extra ports, reverse proxy, CORS or certificates are needed. The GUI only
uses the normal Proxmox API with the logged-in user's session.

## Install

On **one** Proxmox VE node, as root:

```bash
git clone https://github.com/jwklijnsma/ProxLB.git
cd ProxLB/contrib/proxmox
bash proxlb-lxc.sh                                   # DHCP on vmbr0
# or
bash proxlb-lxc.sh --ip 192.168.1.50/24 --gw 192.168.1.1 --storage local-zfs
```

Then reload the web interface (Ctrl+F5) and open **Datacenter → ProxLB**.

The script:

1. creates the user `proxlb@pve`, the role `ProxLB` (`Datastore.Audit, Sys.Audit,
   VM.Audit, VM.Migrate`) on `/`, and the role `ProxLBAgent` (`VM.Config.Options`)
   only on the ProxLB container itself. Each container gets its own API token,
   `proxlb@pve!lxc<CTID>`,
2. creates an unprivileged Debian 13 (or 12) container tagged `proxlb` and
   `plb_ignore_vm`, so ProxLB never migrates itself,
3. installs ProxLB and `proxlb-agent` in `/opt/proxlb/venv`,
4. writes `/etc/proxlb/proxlb.yaml` with every cluster node as API host and the
   cluster CA as trusted CA, so TLS verification stays on,
5. installs the GUI plugin on **all** cluster nodes over the cluster's root SSH.

Useful options (`bash proxlb-lxc.sh --help` lists them all):

| Option | Default | |
|---|---|---|
| `--ctid` | next free | container ID |
| `--ip` / `--gw` / `--bridge` / `--vlan` | `dhcp` / - / `vmbr0` / - | network |
| `--storage` | `local-lvm` | rootfs storage |
| `--interval` / `--interval-format` | `1` / `hours` | how often ProxLB runs |
| `--method` | `memory` | `memory`, `cpu` or `disk` |
| `--balanciness` | `5` | allowed spread between nodes in % |
| `--active` | off | balance for real on schedule right away |
| `--no-gui` / `--gui-this-node-only` | | GUI plugin installation |
| `--upgrade CTID` | | update ProxLB, agent and GUI of an existing CT |

## Safe by default

Scheduled runs are **dry-runs** (preview only) until you switch on
**Automatic balancing** in the GUI, or install with `--active`. The first run
after the agent starts is always a dry-run, so a reboot never triggers
migrations on its own. **Rebalance now** always asks for confirmation.

## Using the GUI

* **Preview (dry-run)**: ProxLB calculates and shows the migrations it would make.
* **Rebalance now**: one real balancing run.
* **Automatic balancing on/off**: scheduled runs migrate, or only preview.
* **Nodes**: live CPU and memory per node, memory at the last run, maintenance
  state, and how many guests move out of or into each node.
* **Balance**: the live spread between the nodes for the configured method,
  compared with `balanciness`.

The tab appears for users with `Sys.Audit` on `/`. Buttons need
`VM.Config.Options` on the ProxLB container. Behind the scenes they add a tag
(`proxlb-cmd-dryrun`, `proxlb-cmd-run`, `proxlb-active`) that the agent
picks up within ~10 seconds.

The container's own **Notes** also show a short summary.

## Inside the container

```bash
pct enter <CTID>
proxlb-ctl dryrun     # queue a preview
proxlb-ctl run        # queue a real balancing run
proxlb-ctl status     # last status (JSON)
proxlb-ctl last       # log of the last run
proxlb-ctl logs       # follow the agent journal
proxlb-ctl config     # edit /etc/proxlb/proxlb.yaml (picked up on the next run)
proxlb-ctl update     # git pull + reinstall (when installed from git)
```

All ProxLB options (affinity, pools, maintenance nodes, PSI, solver, ...) work
as documented in [docs/03_configuration.md](../../docs/03_configuration.md).
`service.schedule` sets the run interval. `service.daemon` and
`service.enable_ha` are ignored because the agent schedules the runs.

## GUI plugin only / remove

```bash
bash gui/install-gui.sh --all-nodes          # install / update
bash gui/install-gui.sh --uninstall --all-nodes
```

The plugin is `/usr/share/pve-manager/js/proxlb-gui.js`, loaded by one line in
`/usr/share/pve-manager/index.html.tpl`. A `pve-manager` update overwrites that
template, so an apt hook (`/etc/apt/apt.conf.d/99proxlb-gui`) adds the line back
automatically.

## Notes

* Migrating guests with **local disks** is off in the generated config. To turn on
  `with_local_disks`, also grant `Datastore.AllocateSpace` to `proxlb@pve`.
* If you restricted tags (*Datacenter → Options → Tag Style → User Allow*),
  allow the `proxlb-*` tags for the users who should press the buttons.
* Removing everything: `pct destroy <CTID>`, `pveum user delete proxlb@pve`,
  `pveum role delete ProxLB`, `pveum role delete ProxLBAgent`, and
  `bash gui/install-gui.sh --uninstall --all-nodes`.
