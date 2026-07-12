# bot-bottle Firecracker network pool — declarative NixOS module.
#
# The one-time privileged setup the Firecracker backend needs: a pool of
# user-owned point-to-point TAP devices plus a fail-closed nftables table
# that confines every microVM to its own sidecar.
#
# NON-INVASIVE BY DESIGN. It does NOT flip `networking.nftables.enable`
# (which would switch your whole host firewall backend) or
# `systemd.network.enable` (which would hand your interfaces to
# systemd-networkd). Instead a single systemd oneshot service brings the
# pool up on boot — the declarative equivalent of
# `sudo ./scripts/firecracker-netpool.sh up`. The `inet <tableName>` table
# is independent (its own hooks at priority -10), so it coexists with an
# iptables `networking.firewall`, Docker, ufw, firewalld, etc.
#
# The oneshot is only restarted when this config changes, so a
# `nixos-rebuild switch` doesn't tear down TAPs out from under running
# VMs unless you actually changed the pool.
#
# The option defaults MUST match the backend constants in
# bot_bottle/backend/firecracker/netpool.py (pool size, IP base, iface
# prefix, table name). The backend reads the same values at launch from
# the BOT_BOTTLE_FC_* env vars; if you override an option here, override
# the matching env var for the CLI too (or set writeEnvFile = true below
# to have this module emit them). Keep the two sides in lockstep.
{ config, lib, pkgs, ... }:

let
  cfg = config.services.bot-bottle-firecracker;

  # --- IPv4 <-> int (full 32-bit carry math) -------------------------
  toOctets = s: map lib.toInt (lib.splitString "." s);
  ipToInt = s:
    let o = toOctets s; in
    (lib.elemAt o 0) * 16777216
    + (lib.elemAt o 1) * 65536
    + (lib.elemAt o 2) * 256
    + (lib.elemAt o 3);
  intToIp = n:
    let
      b0 = n / 16777216;      r0 = n - b0 * 16777216;
      b1 = r0 / 65536;        r1 = r0 - b1 * 65536;
      b2 = r1 / 256;
      b3 = r1 - b2 * 256;
    in "${toString b0}.${toString b1}.${toString b2}.${toString b3}";

  baseInt = ipToInt cfg.ipBase;

  # Slot i: host = base + 2i (the VM's gateway), on its own /31.
  slots = lib.genList (i: {
    iface = "${cfg.ifacePrefix}${toString i}";
    hostIp = intToIp (baseInt + 2 * i);
  }) cfg.poolSize;

  ip = "${pkgs.iproute2}/bin/ip";
  nft = "${pkgs.nftables}/bin/nft";

  # Idempotent ruleset: (create-if-absent → delete → recreate) is atomic
  # within one `nft -f`, so re-running `up` always lands a clean table.
  nftFile = pkgs.writeText "bot-bottle-fc.nft" ''
    table inet ${cfg.tableName}
    delete table inet ${cfg.tableName}
    table inet ${cfg.tableName} {
      chain forward {
        type filter hook forward priority -10; policy accept;
        iifname != "${cfg.ifacePrefix}*" return
        ct state established,related accept
        ct status dnat accept
        drop
      }
      chain input {
        type filter hook input priority -10; policy accept;
        iifname != "${cfg.ifacePrefix}*" return
        ct state established,related accept
        drop
      }
    }
  '';

  # A non-owner user can't open a user-owned TAP, but any member of a
  # TAP's owning GROUP can (the kernel checks owner-uid OR owning-group).
  # So `group` lets an interactive user and a CI-runner user share one
  # pool; `owner` is the single-user default.
  tapOwn = if cfg.group != null then "group ${cfg.group}" else "user ${cfg.owner}";

  upScript = pkgs.writeShellScript "bot-bottle-fc-netpool-up" ''
    set -eu
    ${lib.concatMapStringsSep "\n" (s: ''
      ${ip} link show ${s.iface} >/dev/null 2>&1 || ${ip} tuntap add dev ${s.iface} mode tap ${tapOwn}
      ${ip} addr replace ${s.hostIp}/31 dev ${s.iface}
      ${ip} link set ${s.iface} up
    '') slots}
    ${nft} -f ${nftFile}
  '';

  downScript = pkgs.writeShellScript "bot-bottle-fc-netpool-down" ''
    ${nft} delete table inet ${cfg.tableName} 2>/dev/null || true
    ${lib.concatMapStringsSep "\n" (s: ''
      ${ip} link show ${s.iface} >/dev/null 2>&1 && ${ip} tuntap del dev ${s.iface} mode tap || true
    '') slots}
  '';
in
{
  options.services.bot-bottle-firecracker = {
    enable = lib.mkEnableOption "the bot-bottle Firecracker network pool (TAP devices + isolation nftables table)";

    poolSize = lib.mkOption {
      type = lib.types.ints.positive;
      default = 8;
      description = "Number of pool slots (concurrent bottles). Must match BOT_BOTTLE_FC_POOL_SIZE.";
    };

    ipBase = lib.mkOption {
      type = lib.types.str;
      default = "10.243.0.0";
      description = ''
        Base IPv4 of the /31 pool; slot i uses host = base + 2i. Must
        be /31-aligned (even final address) and must match
        BOT_BOTTLE_FC_IP_BASE. Default is an obscure RFC-1918 /16 that
        dodges docker/libvirt/k8s/LAN and, deliberately, Tailscale's
        100.64.0.0/10 CGNAT range.
      '';
    };

    ifacePrefix = lib.mkOption {
      type = lib.types.str;
      default = "bbfc";
      description = "TAP interface name prefix. Must match BOT_BOTTLE_FC_IFACE_PREFIX.";
    };

    owner = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      example = "alice";
      description = ''
        User that owns the TAP devices (single-user hosts), so
        `./cli.py start` opens them without root. Set exactly one of
        `owner` or `group`.
      '';
    };

    group = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      example = "firecracker";
      description = ''
        Group that owns the TAP devices instead of a single user. Any
        member of this group can open the pool, so an interactive user
        and a CI-runner user can share one pool. Set exactly one of
        `owner` or `group`.
      '';
    };

    tableName = lib.mkOption {
      type = lib.types.str;
      default = "bot_bottle_fc";
      description = "nftables table name for the isolation boundary. Must match netpool.NFT_TABLE.";
    };

    writeEnvFile = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = ''
        When true, write /etc/bot-bottle/firecracker.env with the
        matching BOT_BOTTLE_FC_* values, so the host pool and the CLI
        launcher can't drift. Source it before running `./cli.py`.
      '';
    };
  };

  config = lib.mkIf cfg.enable {
    assertions = [
      {
        assertion = lib.mod baseInt 2 == 0;
        message = "services.bot-bottle-firecracker.ipBase must be /31-aligned (even final octet); got ${cfg.ipBase}.";
      }
      {
        assertion = (cfg.owner != null) != (cfg.group != null);
        message = "services.bot-bottle-firecracker: set exactly one of `owner` or `group`.";
      }
    ];

    # VM->sidecar traffic is DNAT'd and forwarded, so forwarding must be on.
    boot.kernel.sysctl."net.ipv4.ip_forward" = 1;

    # One oneshot brings up the whole pool (TAPs + independent nft table).
    # No networking.nftables.enable / systemd.network.enable — see header.
    systemd.services."bot-bottle-firecracker-netpool" = {
      description = "bot-bottle Firecracker TAP pool + nft isolation table";
      wantedBy = [ "multi-user.target" ];
      after = [ "network-pre.target" ];
      serviceConfig = {
        Type = "oneshot";
        RemainAfterExit = true;
        ExecStart = upScript;
        ExecStop = downScript;
      };
    };

    environment.etc."bot-bottle/firecracker.env" = lib.mkIf cfg.writeEnvFile {
      text = ''
        BOT_BOTTLE_FC_POOL_SIZE=${toString cfg.poolSize}
        BOT_BOTTLE_FC_IP_BASE=${cfg.ipBase}
        BOT_BOTTLE_FC_IFACE_PREFIX=${cfg.ifacePrefix}
      '';
    };
  };
}
