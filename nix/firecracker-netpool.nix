# bot-bottle Firecracker network pool — declarative NixOS module.
#
# The one-time privileged setup the Firecracker backend needs: a pool of
# user-owned point-to-point TAP devices plus a fail-closed nftables table
# that confines every microVM to its own sidecar. Enabling this module is
# the NixOS equivalent of `sudo ./scripts/firecracker-netpool.sh up` — but
# declarative, so the taps/table survive `nixos-rebuild` and are only
# reconfigured when this config changes (running VMs aren't dropped).
#
# The option defaults MUST match the backend constants in
# bot_bottle/backend/firecracker/netpool.py (pool size, IP base, iface
# prefix, table name). The backend reads the same values at launch from
# the BOT_BOTTLE_FC_* env vars; if you override an option here, override
# the matching env var for the CLI too (or set writeEnvFile = true below
# to have this module emit them). Keep the two sides in lockstep.
{ config, lib, ... }:

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

  mkAttr = f: lib.listToAttrs (map (s: lib.nameValuePair "10-${s.iface}" (f s)) slots);
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
      type = lib.types.str;
      example = "alice";
      description = "User that owns the TAP devices, so `./cli.py start` opens them without root.";
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
    ];

    # VM->sidecar traffic is DNAT'd and forwarded, so forwarding must be on.
    boot.kernel.sysctl."net.ipv4.ip_forward" = 1;

    systemd.network.enable = true;
    systemd.network.netdevs = mkAttr (s: {
      netdevConfig = { Name = s.iface; Kind = "tap"; };
      tapConfig = { User = cfg.owner; };
    });
    systemd.network.networks = mkAttr (s: {
      matchConfig.Name = s.iface;
      address = [ "${s.hostIp}/31" ];
      networkConfig.ConfigureWithoutCarrier = true;
    });

    # Independent, fail-closed table — coexists with Docker/ufw/firewalld
    # rather than replacing the ruleset. A VM (traffic from a
    # ${cfg.ifacePrefix}* TAP) may reach only its own sidecar (DNAT'd from
    # the host TAP IP); everything else is dropped.
    networking.nftables.enable = true;
    networking.nftables.tables.${cfg.tableName} = {
      family = "inet";
      content = ''
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
      '';
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
