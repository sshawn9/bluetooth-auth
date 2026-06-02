{
  description = "BlueZ D-Bus Bluetooth auth tools";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

    flake-parts = {
      url = "github:hercules-ci/flake-parts";
      inputs.nixpkgs-lib.follows = "nixpkgs";
    };

    pyproject-nix = {
      url = "github:pyproject-nix/pyproject.nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };

    uv2nix = {
      url = "github:pyproject-nix/uv2nix";
      inputs.nixpkgs.follows = "nixpkgs";
      inputs.pyproject-nix.follows = "pyproject-nix";
    };

    pyproject-build-systems = {
      url = "github:pyproject-nix/build-system-pkgs";
      inputs.nixpkgs.follows = "nixpkgs";
      inputs.pyproject-nix.follows = "pyproject-nix";
      inputs.uv2nix.follows = "uv2nix";
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
        { pkgs, ... }:
        {
          packages = rec {
            bluetooth-auth = pkgs.callPackage ./package.nix {
              inherit pkgs;
              inherit (inputs)
                pyproject-build-systems
                pyproject-nix
                uv2nix
                ;
            };
            default = bluetooth-auth;
          };

          devShells.default = pkgs.mkShell {
            packages = [
              pkgs.python3
              pkgs.uv
            ];
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
