# NixOS image for scripts/linux-install-test.sh.
#
# NixOS publishes no downloadable cloud qcow2 (its cloud images are Hydra-built
# AMIs), so the harness BUILDS this one with nixos-generators (-f qcow). It is
# deliberately minimal — no python3/git/pipx — so the bare `test` variant is
# genuinely under-provisioned and exercises install.sh's guards; `test-ready`
# provisions them with `nix profile install` (hence flakes below).
{ lib, ... }:
{
  # Consume the same NoCloud seed the other distros use: cloud-init injects the
  # per-run ephemeral SSH key for root. Leave networking to NixOS's default
  # dhcpcd (QEMU user-mode NAT) — enabling cloud-init's networkd here conflicts
  # with dhcpcd and can drop the guest's network.
  services.cloud-init.enable = true;
  services.cloud-init.network.enable = false;

  services.openssh.enable = true;
  services.openssh.settings.PermitRootLogin = lib.mkForce "prohibit-password";

  # Flakes so the `test-ready` prereq step can `nix profile install nixpkgs#...`.
  nix.settings.experimental-features = [ "nix-command" "flakes" ];

  system.stateVersion = "24.11";
}
