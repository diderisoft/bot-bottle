{
  description = "bot-bottle — sandboxed runtime for AI coding agents";

  outputs = { self, ... }: {
    # Declarative host setup for the Firecracker backend's network pool.
    # Consume from a flake-based NixOS config:
    #
    #   inputs.bot-bottle.url = "git+ssh://<your-bot-bottle-remote>";  # or path:/…
    #   # then, in your host module:
    #   imports = [ inputs.bot-bottle.nixosModules.firecracker-netpool ];
    #   services.bot-bottle-firecracker = { enable = true; owner = "you"; };
    #
    # The module is plain (no nixpkgs pin), so channel users can import
    # ./nix/firecracker-netpool.nix directly without the flake.
    nixosModules.firecracker-netpool = import ./nix/firecracker-netpool.nix;
    nixosModules.default = self.nixosModules.firecracker-netpool;
  };
}
