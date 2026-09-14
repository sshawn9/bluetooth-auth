{
  description = "BlueZ D-Bus Bluetooth auth tools";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

    flake-parts = {
      url = "github:hercules-ci/flake-parts";
      inputs.nixpkgs-lib.follows = "nixpkgs";
    };
  };

  outputs =
    inputs@{
      self,
      flake-parts,
      ...
    }:
    flake-parts.lib.mkFlake { inherit inputs; } {
      systems = [
        "x86_64-linux"
        "aarch64-linux"
      ];

      perSystem =
        { pkgs, self', ... }:
        {
          packages = rec {
            bluetooth-auth = pkgs.callPackage ./package.nix { };
            default = bluetooth-auth;
          };

          checks.auth = pkgs.callPackage ./tests/auth.nix {
            package = self'.packages.bluetooth-auth;
          };
          checks.connect = pkgs.callPackage ./tests/connect.nix { };

          devShells.default = pkgs.mkShell {
            inputsFrom = [ self'.packages.bluetooth-auth ];
          };
        };

      flake.nixosModules = rec {
        bluetooth-auth =
          {
            config,
            lib,
            pkgs,
            ...
          }:
          import ./modules/nixos/bluetooth-auth.nix {
            inherit config lib pkgs;
            bluetoothAuthPackage = self.packages.${pkgs.stdenv.hostPlatform.system}.bluetooth-auth;
          };
        default = bluetooth-auth;
      };
    };
}
