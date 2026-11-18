#!/usr/bin/env bash
# Install / remove the ProxLB plugin for the Proxmox VE web interface.
#
#   install-gui.sh [--all-nodes] [--js FILE]   install (or update)
#   install-gui.sh --uninstall [--all-nodes]   remove
#   install-gui.sh --reapply                   re-patch index.html.tpl (apt hook)
#
# The plugin adds "Datacenter -> ProxLB". It must be installed on every node
# whose web interface you use; --all-nodes does that over the cluster's root SSH.
# A pve-manager update overwrites index.html.tpl, so an apt hook re-applies it.
set -euo pipefail

PVE_JS_DIR=/usr/share/pve-manager/js
PVE_TPL=/usr/share/pve-manager/index.html.tpl
SHARE_DIR=/usr/local/share/proxlb-gui
SELF=/usr/local/sbin/proxlb-gui
APT_HOOK=/etc/apt/apt.conf.d/99proxlb-gui
JS_NAME=proxlb-gui.js
MARK='<!-- proxlb-gui -->'

ACTION=install
ALL_NODES=0
JS_SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$JS_NAME"

while [ $# -gt 0 ]; do
    case "$1" in
        --uninstall) ACTION=uninstall ;;
        --reapply)   ACTION=reapply ;;
        --all-nodes) ALL_NODES=1 ;;
        --js)        JS_SRC="$2"; shift ;;
        -h|--help)   sed -n '2,12p' "$0"; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 1 ;;
    esac
    shift
done

msg() { echo "[proxlb-gui] $*"; }

[ "$(id -u)" -eq 0 ] || { echo "run as root on a Proxmox VE node" >&2; exit 1; }
[ -f "$PVE_TPL" ] || { echo "$PVE_TPL not found - is this a Proxmox VE node?" >&2; exit 1; }

patch_tpl() {
    local ver
    ver=$(sha256sum "$SHARE_DIR/$JS_NAME" | cut -c1-10)
    install -m 0644 "$SHARE_DIR/$JS_NAME" "$PVE_JS_DIR/$JS_NAME"
    # drop an old line, then add ours right after pvemanagerlib.js
    sed -i "\#${MARK}#d" "$PVE_TPL"
    if ! grep -q 'pvemanagerlib.js' "$PVE_TPL"; then
        msg "WARNING: pvemanagerlib.js not referenced in $PVE_TPL - not patched"
        return 1
    fi
    sed -i "\#pvemanagerlib.js#a\\    <script type=\"text/javascript\" src=\"/pve2/js/${JS_NAME}?ver=${ver}\"></script>${MARK}" "$PVE_TPL"
}

restart_proxy() {
    # pveproxy indexes /pve2/js at start, so a restart is needed for a new file
    systemctl restart pveproxy.service
}

unpatch_tpl() {
    sed -i "\#${MARK}#d" "$PVE_TPL"
    rm -f "$PVE_JS_DIR/$JS_NAME"
}

other_nodes() {
    # IPs of all other online cluster nodes (empty on a single node)
    pvesh get /cluster/status --output-format json 2>/dev/null | perl -MJSON -e '
        my $d = eval { decode_json(join("", <STDIN>)) } || [];
        for (@$d) { print "$_->{ip}\n" if $_->{type} eq "node" && !$_->{local} && $_->{online} && $_->{ip} }'
}

case "$ACTION" in
    install)
        [ -f "$JS_SRC" ] || { echo "plugin file $JS_SRC not found" >&2; exit 1; }
        mkdir -p "$SHARE_DIR"
        if [ "$(readlink -f "$JS_SRC")" != "$(readlink -f "$SHARE_DIR/$JS_NAME")" ]; then
            install -m 0644 "$JS_SRC" "$SHARE_DIR/$JS_NAME"
        fi
        if [ "$(readlink -f "$0")" != "$SELF" ]; then
            install -m 0755 "$0" "$SELF"
        fi
        cat > "$APT_HOOK" <<EOF
// Re-apply the ProxLB GUI plugin after pve-manager updates
DPkg::Post-Invoke { "if [ -x $SELF ] && ! grep -q 'proxlb-gui' $PVE_TPL 2>/dev/null; then $SELF --reapply || true; fi"; };
EOF
        patch_tpl
        restart_proxy
        msg "installed on $(hostname) - reload the web interface (Ctrl+F5) and open Datacenter -> ProxLB"
        if [ "$ALL_NODES" -eq 1 ]; then
            for ip in $(other_nodes); do
                msg "installing on $ip"
                scp -q -o BatchMode=yes "$SHARE_DIR/$JS_NAME" "root@$ip:/tmp/$JS_NAME" &&
                scp -q -o BatchMode=yes "$SELF" "root@$ip:/tmp/proxlb-gui-install.sh" &&
                ssh -o BatchMode=yes "root@$ip" "bash /tmp/proxlb-gui-install.sh --js /tmp/$JS_NAME && rm -f /tmp/$JS_NAME /tmp/proxlb-gui-install.sh" ||
                msg "WARNING: failed on $ip"
            done
        fi
        ;;
    reapply)
        [ -f "$SHARE_DIR/$JS_NAME" ] || exit 0
        patch_tpl && restart_proxy && msg "re-applied after package update"
        ;;
    uninstall)
        unpatch_tpl
        rm -f "$APT_HOOK" "$SHARE_DIR/$JS_NAME"
        rmdir "$SHARE_DIR" 2>/dev/null || true
        restart_proxy
        msg "removed from $(hostname)"
        if [ "$ALL_NODES" -eq 1 ]; then
            for ip in $(other_nodes); do
                msg "removing on $ip"
                ssh -o BatchMode=yes "root@$ip" "[ -x $SELF ] && $SELF --uninstall" || msg "WARNING: failed on $ip"
            done
        fi
        rm -f "$SELF"
        ;;
esac
