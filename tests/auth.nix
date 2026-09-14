{ pkgs, package }:

let
  lib = pkgs.lib;
  fakeBle = pkgs.writeShellScriptBin "ble-link" ''
    set -eu
    test "$#" -eq 4
    test "$1" = --address-file
    test "$2" = /private/test-address
    test "$3" = --connect
    test "$4" = -1
    test -z "''${BLE_MARK:-}" || : > "$BLE_MARK"
    exit "''${BLE_RESULT:-1}"
  '';
  nextAuth = pkgs.writeShellScript "pam-next-auth" ''
    test -z "''${NEXT_MARK:-}" || : > "$NEXT_MARK"
    exit "''${NEXT_RESULT:-1}"
  '';
  failedAuth = pkgs.writeShellScript "pam-prior-failure" "exit 1";
  system = import (pkgs.path + "/nixos/lib/eval-config.nix") {
    inherit (pkgs.stdenv.hostPlatform) system;
    modules = [
      (
        { config, lib, ... }:
        import ../modules/nixos/bluetooth-auth.nix {
          inherit config lib;
          bluetoothAuthPackage = fakeBle;
        }
      )
      {
        networking.useDHCP = false;
        my.security.bluetoothAuth = {
          enable = true;
          user = "nobody";
          bluetoothAddressFile = "/private/test-address";
          sudoAuth.enable = true;
          lockerAuth = {
            enable = true;
            pamService = "login";
          };
          polkitAuth = {
            enable = true;
            allowedActions = [ "test.bluetooth-action" ];
          };
          greetdAuth.enable = true;
        };
      }
    ];
  };
  greetdOnly = import (pkgs.path + "/nixos/lib/eval-config.nix") {
    inherit (pkgs.stdenv.hostPlatform) system;
    modules = [
      (
        { config, lib, ... }:
        import ../modules/nixos/bluetooth-auth.nix {
          inherit config lib;
          bluetoothAuthPackage = fakeBle;
        }
      )
      {
        networking.useDHCP = false;
        my.security.bluetoothAuth = {
          enable = true;
          user = "nobody";
          bluetoothAddressFile = "/private/test-address";
          greetdAuth.enable = true;
        };
      }
    ];
  };
  bluetoothLines =
    service:
    lib.filter (line: lib.hasPrefix "auth " line && lib.hasInfix "# bluetooth-auth" line) (
      lib.splitString "\n" system.config.security.pam.services.${service}.text
    );
  sudoPam = lib.concatStringsSep "\n" (bluetoothLines "sudo");
  lockerPam = lib.concatStringsSep "\n" (bluetoothLines "login");
  greetdPam = lib.concatStringsSep "\n" (bluetoothLines "greetd");
  polkitRules = pkgs.writeText "bluetooth-auth-polkit.rules" system.config.security.polkit.extraConfig;
in
pkgs.runCommand "bluetooth-auth-integration-tests"
  {
    nativeBuildInputs = [
      pkgs.python3
      pkgs.nodejs
      pkgs.linux-pam
    ];
  }
  ''
      test -x ${package}/bin/ble-link
      test "${lib.concatStringsSep " " system.config.systemd.services.polkit.serviceConfig.RestrictAddressFamilies}" = "AF_UNIX AF_BLUETOOTH"
      test "${
        if system.config.systemd.services.polkit.serviceConfig.PrivateNetwork then "0" else "1"
      }" = 1
      test "${
        if lib.any (line: lib.hasInfix " seteuid " line) (bluetoothLines "sudo") then "1" else "0"
      }" = 1
      test "${if greetdOnly.config.systemd.sockets ? bluetooth-auth-connect then "1" else "0"}" = 1
      test "${if greetdOnly.config.systemd.services ? bluetooth-auth-connect then "1" else "0"}" = 1
      mkdir -p pam
      cat > pam/sudo <<'EOF'
    ${sudoPam}
    auth required ${pkgs.linux-pam}/lib/security/pam_exec.so quiet ${nextAuth}
    EOF
      cat > pam/locker <<'EOF'
    ${lockerPam}
    auth required ${pkgs.linux-pam}/lib/security/pam_exec.so quiet ${nextAuth}
    EOF
      cat > pam/greetd <<'EOF'
    ${greetdPam}
    auth required ${pkgs.linux-pam}/lib/security/pam_exec.so quiet ${nextAuth}
    EOF
      cat > pam/prior-failure <<'EOF'
    auth required ${pkgs.linux-pam}/lib/security/pam_exec.so quiet ${failedAuth}
    ${lockerPam}
    auth required ${pkgs.linux-pam}/lib/security/pam_exec.so quiet ${nextAuth}
    EOF
      ${pkgs.python3}/bin/python ${./pam_integration.py} "$PWD/pam" ${pkgs.linux-pam}/lib/libpam.so.0
      ${pkgs.nodejs}/bin/node ${./polkit.js} ${polkitRules}
      touch "$out"
  ''
