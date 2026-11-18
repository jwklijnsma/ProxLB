#!/usr/bin/env bash
# =============================================================================
#  proxlb-external.sh - connect ProxLB running on ANOTHER machine (a VM
#                       outside the cluster, a physical server, a Docker host)
#                       to the ProxLB tab in the Proxmox VE web interface.
#
#  Run as root on one Proxmox VE node:
#
#    bash proxlb-external.sh                  # next free VMID, anchor "proxlb-anchor"
#    bash proxlb-external.sh --vmid 9000
#
#  What it does:
#    1. creates a tiny "anchor" VM: no disks, no network, never started. It
#       uses no storage or RAM; its Notes and tags are the mailbox between
#       the web interface and proxlb-agent. Tags: proxlb, plb_ignore_vm.
#       Protected against deletion; HA-managed (state stopped) so it moves to
#       a surviving node when its node fails.
#    2. creates proxlb@pve with the role ProxLB on / and ProxLBAgent
#       (VM.Config.Options) on the anchor only, plus an API token,
#    3. installs the web-interface plugin on all nodes,
#    4. writes /root/proxlb-external-<VMID>.tar (0600): API hosts, token and
#       the cluster CA - copy it to the ProxLB machine and run there:
#         install-agent.sh --bundle proxlb-external-<VMID>.tar
# =============================================================================
set -euo pipefail

VMID=""
NAME="proxlb-anchor"
PVE_USER="proxlb@pve"
GUI=1
GUI_ALL_NODES=1
HA="auto"
CMD="${PROXLB_EXTERNAL_CMD:-bash $0}"

usage() {
    cat <<EOF
Usage: $CMD [options]

  --vmid ID              VMID for the anchor VM (default: next free ID)
  --name NAME            anchor VM name (default: $NAME)
  --no-ha                do not make the anchor VM HA-managed
  --no-gui               do not install the web-interface plugin
  --gui-this-node-only   install the plugin only on this node
  --rotate-token         only create a new API token + bundle for an existing anchor VM
EOF
}

ROTATE=0
while [ $# -gt 0 ]; do
    case "$1" in
        --vmid) VMID="$2"; shift ;;
        --name) NAME="$2"; shift ;;
        --no-ha) HA=no ;;
        --no-gui) GUI=0 ;;
        --gui-this-node-only) GUI_ALL_NODES=0 ;;
        --rotate-token) ROTATE=1 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown option: $1" >&2; usage; exit 1 ;;
    esac
    shift
done

C_G=$'\e[32m'; C_Y=$'\e[33m'; C_R=$'\e[31m'; C_0=$'\e[0m'
info() { echo "${C_G}[ProxLB]${C_0} $*"; }
warn() { echo "${C_Y}[ProxLB] WARNING:${C_0} $*"; }
die()  { echo "${C_R}[ProxLB] ERROR:${C_0} $*" >&2; exit 1; }
json_get() { perl -MJSON -e 'my $d = decode_json(join("", <STDIN>)); '"$1"; }

[ "$(id -u)" -eq 0 ] || die "run as root on a Proxmox VE node"
command -v qm >/dev/null && command -v pveum >/dev/null || die "this is not a Proxmox VE node"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

guest_node() { # node of VMID $1 (any type), empty if it does not exist
    pvesh get /cluster/resources --type vm --output-format json | \
        PLB_ID="$1" json_get 'for (@$d) { print $_->{node} if $_->{vmid} == $ENV{PLB_ID} }'
}

# ------------------------------------------------------------- anchor VM ---
if [ "$ROTATE" -eq 1 ]; then
    [ -n "$VMID" ] || die "--rotate-token needs --vmid"
    [ -n "$(guest_node "$VMID")" ] || die "VM $VMID does not exist"
else
    [ -n "$VMID" ] || VMID=$(pvesh get /cluster/nextid)
    [ -z "$(guest_node "$VMID")" ] || die "ID $VMID is already in use"
    info "creating anchor VM $VMID ($NAME): no disks, no network, never started"
    qm create "$VMID" --name "$NAME" --memory 64 --cores 1 --ostype other \
        --onboot 0 --tags "proxlb;plb_ignore_vm" --protection 1 \
        --description "ProxLB anchor - status of ProxLB running on another machine. Do not start or delete."
fi

# -------------------------------------------------------------- API access ---
info "creating Proxmox user $PVE_USER, roles and API token"
ROLE_PRIVS="Datastore.Audit,Sys.Audit,VM.Audit,VM.Migrate"
if pveum role list --output-format json | json_get 'exit(!(grep { $_->{roleid} eq "ProxLB" } @$d))'; then
    pveum role modify ProxLB --privs "$ROLE_PRIVS"
else
    pveum role add ProxLB --privs "$ROLE_PRIVS"
fi
if ! pveum role list --output-format json | json_get 'exit(!(grep { $_->{roleid} eq "ProxLBAgent" } @$d))'; then
    pveum role add ProxLBAgent --privs "VM.Config.Options"
fi
if ! pveum user list --output-format json | PLB_USER="$PVE_USER" json_get 'exit(!(grep { $_->{userid} eq $ENV{PLB_USER} } @$d))'; then
    pveum user add "$PVE_USER" --comment "ProxLB service user (API token only)"
fi
pveum acl modify / --users "$PVE_USER" --roles ProxLB
pveum acl modify "/vms/$VMID" --users "$PVE_USER" --roles ProxLBAgent
TOKEN_NAME="external$VMID"
pveum user token remove "$PVE_USER" "$TOKEN_NAME" >/dev/null 2>&1 || true
TOKEN_SECRET=$(pveum user token add "$PVE_USER" "$TOKEN_NAME" --privsep 0 \
    --comment "external ProxLB, anchor VM $VMID" --output-format json | json_get 'print $d->{value}')
[ -n "$TOKEN_SECRET" ] || die "could not create API token"

API_HOSTS=$(pvesh get /cluster/status --output-format json | json_get '
    print join(",", map { $_->{ip} } grep { $_->{type} eq "node" && $_->{ip} } @$d)')
[ -n "$API_HOSTS" ] || API_HOSTS=$(hostname --ip-address | awk '{print $1}')
CLUSTER=$(pvesh get /cluster/status --output-format json | json_get '
    for (@$d) { print $_->{name} if $_->{type} eq "cluster" }')

# -------------------------------------------------------------------- HA ---
NODES=$(pvesh get /cluster/status --output-format json | json_get 'print scalar grep { $_->{type} eq "node" } @$d')
if [ "$ROTATE" -eq 0 ] && [ "$HA" != "no" ] && [ "${NODES:-1}" -ge 2 ]; then
    if ha-manager config 2>/dev/null | grep -q "^vm: *$VMID\$"; then
        ha-manager set "vm:$VMID" --state stopped
    else
        ha-manager add "vm:$VMID" --state stopped --comment "ProxLB anchor"
    fi
    info "anchor VM is HA-managed (state stopped): it moves to another node if its node fails"
    [ "$NODES" -eq 2 ] && warn "2-node cluster: HA needs a QDevice for quorum (pvecm qdevice setup <ip>)"
fi

# ------------------------------------------------------------ GUI plugin ---
if [ "$GUI" -eq 1 ]; then
    info "installing the ProxLB tab into the Proxmox web interface"
    GUI_ARGS=()
    [ "$GUI_ALL_NODES" -eq 1 ] && GUI_ARGS+=(--all-nodes)
    if [ -x /usr/sbin/proxlb-gui ]; then
        /usr/sbin/proxlb-gui --packaged "${GUI_ARGS[@]}" || warn "GUI plugin installation failed"
    elif [ -f "$SCRIPT_DIR/gui/install-gui.sh" ]; then
        bash "$SCRIPT_DIR/gui/install-gui.sh" "${GUI_ARGS[@]}" || warn "GUI plugin installation failed"
    else
        warn "GUI plugin not found - install the proxlb-pve package or run gui/install-gui.sh"
    fi
fi

# ---------------------------------------------------------------- bundle ---
BUNDLE="/root/proxlb-external-$VMID.tar"
TMP=$(mktemp -d)
umask 077
cat > "$TMP/connect.env" <<EOF
# ProxLB external connection - created by proxlb-external.sh on $(hostname) $(date -u +%FT%TZ)
# Contains an API token secret: keep it private, delete it after use.
PROXLB_VMID=$VMID
PROXLB_GUEST_TYPE=qemu
PROXLB_API_HOSTS=$API_HOSTS
PROXLB_API_USER=$PVE_USER
PROXLB_TOKEN_ID=$TOKEN_NAME
PROXLB_TOKEN_SECRET=$TOKEN_SECRET
PROXLB_CLUSTER=$CLUSTER
EOF
cp /etc/pve/pve-root-ca.pem "$TMP/pve-root-ca.pem"
tar -C "$TMP" -cf "$BUNDLE" connect.env pve-root-ca.pem
chmod 0600 "$BUNDLE"
rm -rf "$TMP"

cat <<EOF

${C_G}Anchor VM $VMID is ready.${C_0} Now connect the machine where ProxLB runs:

  1. copy the bundle (it holds the API token - keep it private):
       scp $BUNDLE root@<proxlb-machine>:/root/

  2. on the ProxLB machine, as root:
       git clone https://github.com/jwklijnsma/ProxLB.git
       bash ProxLB/contrib/proxmox/agent/install-agent.sh --bundle /root/proxlb-external-$VMID.tar
       rm /root/proxlb-external-$VMID.tar

  3. reload the web interface (Ctrl+F5): Datacenter -> ProxLB shows
     "ProxLB runs on <proxlb-machine>" within ~30 seconds.

  The ProxLB machine must reach the nodes on TCP 8006: $API_HOSTS
  New token later:  $CMD --rotate-token --vmid $VMID
EOF
