# bot-bottle Firecracker network pool — declarative NixOS module.
#
# The one-time privileged setup the Firecracker backend needs: a pool of
# user- (or group-) owned point-to-point TAP devices plus a fail-closed
# nftables table that confines every microVM to its own gateway.
#
# NON-INVASIVE BY DESIGN. It does NOT flip `networking.nftables.enable`
# (which would switch your whole host firewall backend) or
# `systemd.network.enable` (which would hand your interfaces to
# systemd-networkd). Instead a single systemd oneshot brings the pool up
# on boot by running the SAME bring-up script as every other install
# path (`scripts/firecracker-netpool.sh`) — so the TAP/nft logic lives in
# exactly one place. The `inet <tableName>` table is independent (its own
# hooks at priority -10), so it coexists with an iptables
# `networking.firewall`, Docker, ufw, firewalld, etc.
#
# The oneshot is only restarted when this config changes, and the shared
# script is non-destructive (it never tears down an existing TAP), so a
# `nixos-rebuild switch` won't cut TAPs out from under running VMs.
#
# Single source of the pool defaults: bot_bottle/backend/firecracker/
# netpool.defaults.env. This module readFile-parses it for the option
# defaults below, then passes the resolved values back to the script as
# Environment=, so the host pool and the CLI launcher can't drift.
{ config, lib, pkgs, ... }:

let
  cfg = config.services.bot-bottle-firecracker;

  # --- shared single-source defaults ---------------------------------
  # Parse the KEY=VALUE defaults file (the same one netpool.py and the
  # shell script read). Pure eval — just readFile, no import-from-
  # derivation — so it works for both flake and channel consumers.
  readDefaults = file:
    let
      lines = lib.splitString "\n" (builtins.readFile file);
      keep = l: l != "" && !(lib.hasPrefix "#" l) && lib.hasInfix "=" l;
      toPair = l:
        let parts = lib.splitString "=" l;
        in lib.nameValuePair (lib.head parts)
             (lib.concatStringsSep "=" (lib.tail parts));
    in lib.listToAttrs (map toPair (lib.filter keep lines));

  defaults = readDefaults ../bot_bottle/backend/firecracker/netpool.defaults.env;

  # The one bring-up implementation, shared with the sudo/systemd paths.
  netpoolScript = ../scripts/firecracker-netpool.sh;

  # /31 alignment == an even final octet (only bit 0 matters for base+2i).
  lastOctet = lib.toInt (lib.last (lib.splitString "." cfg.ipBase));

  # The script needs ip/nft/sysctl + the usual coreutils, plus iptables
  # for the orchestrator link's DOCKER-USER accept (best-effort; skipped
  # when Docker is absent). It gets every pool value via the unit's
  # Environment=, so it never reads the shared defaults file (which isn't
  # beside it once copied to the store).
  runtimePath = with pkgs; [ iproute2 nftables iptables procps coreutils gnused ];

  ownEnv =
    if cfg.group != null
    then { BOT_BOTTLE_FC_GROUP = cfg.group; }
    else { BOT_BOTTLE_FC_OWNER = cfg.owner; };

  unitEnv = {
    BOT_BOTTLE_FC_POOL_SIZE = toString cfg.poolSize;
    BOT_BOTTLE_FC_IP_BASE = cfg.ipBase;
    BOT_BOTTLE_FC_IFACE_PREFIX = cfg.ifacePrefix;
    BOT_BOTTLE_FC_NFT_TABLE = cfg.tableName;
    BOT_BOTTLE_FC_ORCH_IFACE = cfg.orchIface;
    BOT_BOTTLE_FC_GW_IFACE = cfg.gwIface;
  } // ownEnv;
in
{
  options.services.bot-bottle-firecracker = {
    enable = lib.mkEnableOption "the bot-bottle Firecracker network pool (TAP devices + isolation nftables table)";

    poolSize = lib.mkOption {
      type = lib.types.ints.positive;
      default = lib.toInt defaults.BOT_BOTTLE_FC_POOL_SIZE;
      defaultText = lib.literalMD "the shared `netpool.defaults.env` value";
      description = "Number of pool slots (concurrent bottles). Must match BOT_BOTTLE_FC_POOL_SIZE.";
    };

    ipBase = lib.mkOption {
      type = lib.types.str;
      default = defaults.BOT_BOTTLE_FC_IP_BASE;
      defaultText = lib.literalMD "the shared `netpool.defaults.env` value";
      description = ''
        Base IPv4 of the /31 pool; slot i uses host = base + 2i. Must
        be /31-aligned (even final octet) and must match
        BOT_BOTTLE_FC_IP_BASE. The shared default is an obscure RFC-1918
        /16 that dodges docker/libvirt/k8s/LAN and, deliberately,
        Tailscale's 100.64.0.0/10 CGNAT range.
      '';
    };

    ifacePrefix = lib.mkOption {
      type = lib.types.str;
      default = defaults.BOT_BOTTLE_FC_IFACE_PREFIX;
      defaultText = lib.literalMD "the shared `netpool.defaults.env` value";
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
      default = defaults.BOT_BOTTLE_FC_NFT_TABLE;
      defaultText = lib.literalMD "the shared `netpool.defaults.env` value";
      description = "nftables table name for the isolation boundary. Must match netpool.NFT_TABLE.";
    };

    orchIface = lib.mkOption {
      type = lib.types.str;
      default = defaults.BOT_BOTTLE_FC_ORCH_IFACE;
      defaultText = lib.literalMD "the shared `netpool.defaults.env` value";
      description = ''
        TAP name for the orchestrator VM's dedicated link. Unlike the
        isolated bbfc* agent pool, this link is NAT'd to the internet
        (the orchestrator is trusted infra that builds agent images
        in-VM). Must match BOT_BOTTLE_FC_ORCH_IFACE / netpool.ORCH_IFACE.
      '';
    };

    gwIface = lib.mkOption {
      type = lib.types.str;
      default = defaults.BOT_BOTTLE_FC_GW_IFACE;
      defaultText = lib.literalMD "the shared `netpool.defaults.env` value";
      description = ''
        TAP name for the gateway (data-plane) VM's dedicated link — the
        second infra link, split from the orchestrator per PRD 0070. Like
        the orchestrator link it is NAT'd to the internet (the gateway
        forwards agent egress upstream); agents DNAT here, never to the
        orchestrator, so a breached agent has no L3 route to the control
        plane. Must match BOT_BOTTLE_FC_GW_IFACE / netpool.GW_IFACE.
      '';
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
        assertion = lib.mod lastOctet 2 == 0;
        message = "services.bot-bottle-firecracker.ipBase must be /31-aligned (even final octet); got ${cfg.ipBase}.";
      }
      {
        assertion = (cfg.owner != null) != (cfg.group != null);
        message = "services.bot-bottle-firecracker: set exactly one of `owner` or `group`.";
      }
    ];

    # VM->gateway traffic is DNAT'd and forwarded, so forwarding must be on.
    boot.kernel.sysctl."net.ipv4.ip_forward" = 1;

    # The orchestrator VM reaches the host controller on the host end of its
    # own TAP (PRD 0092; contract.hostctl.GUEST_PORT). Opened on that one
    # interface only; agent VMs' input stays dropped by the isolation table.
    networking.firewall.interfaces.${cfg.orchIface}.allowedTCPPorts = [ 8098 ];

    # One oneshot brings up the whole pool (TAPs + independent nft table)
    # by running the shared bring-up script. No networking.nftables.enable
    # / systemd.network.enable — see header.
    systemd.services."bot-bottle-firecracker-netpool" = {
      description = "bot-bottle Firecracker TAP pool + nft isolation table";
      wantedBy = [ "multi-user.target" ];
      after = [ "network-pre.target" ];
      path = runtimePath;
      environment = unitEnv;
      serviceConfig = {
        Type = "oneshot";
        RemainAfterExit = true;
        ExecStart = "${pkgs.bash}/bin/bash ${netpoolScript} up";
        ExecStop = "${pkgs.bash}/bin/bash ${netpoolScript} down";
      };
    };

    environment.etc."bot-bottle/firecracker.env" = lib.mkIf cfg.writeEnvFile {
      text = ''
        BOT_BOTTLE_FC_POOL_SIZE=${toString cfg.poolSize}
        BOT_BOTTLE_FC_IP_BASE=${cfg.ipBase}
        BOT_BOTTLE_FC_IFACE_PREFIX=${cfg.ifacePrefix}
        BOT_BOTTLE_FC_NFT_TABLE=${cfg.tableName}
        BOT_BOTTLE_FC_ORCH_IFACE=${cfg.orchIface}
        BOT_BOTTLE_FC_GW_IFACE=${cfg.gwIface}
      '';
    };
  };
}
