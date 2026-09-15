{ pkgs, package }:

let
  inherit (pkgs) lib;
  fakeBle = pkgs.symlinkJoin {
    name = "fake-bluetooth-auth";
    paths = [
      (pkgs.writeShellScriptBin "bluetooth-auth-link" ''
        set -eu
        test "$#" -eq 4
        test "$1" = --address-file
        test "$2" = /private/test-address
        test "$3" = --connect
        test "$4" = -1
        test -z "''${BLE_MARK:-}" || : > "$BLE_MARK"
        exit "''${BLE_RESULT:-1}"
      '')
      (pkgs.writeShellScriptBin "bluetooth-auth-prepare-le" ''
        read -r _address
      '')
    ];
  };
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
        services.greetd = {
          enable = true;
          settings.default_session.command = "${pkgs.coreutils}/bin/true";
        };
        my.security.bluetoothAuth = {
          enable = true;
          trustedUser = "nobody";
          device.address.file = "/private/test-address";
          auth.sudo.enable = true;
          auth.locker = {
            enable = true;
            pamService = "login";
          };
          auth.polkit = {
            enable = true;
            allowedActions = [ "test.bluetooth-action" ];
          };
          auth.greetd.enable = true;
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
          trustedUser = "nobody";
          device.address.file = "/private/test-address";
          auth.greetd.enable = true;
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
  greeterPam = lib.concatStringsSep "\n" (
    lib.filter (line: lib.hasPrefix "auth " line || lib.hasPrefix "account " line) (
      lib.splitString "\n" system.config.security.pam.services.greetd.text
    )
  );
  polkitRules = pkgs.writeText "bluetooth-auth-polkit.rules" system.config.security.polkit.extraConfig;
in
assert
  system.config.security.wrappers.bluetooth-auth-prepare-le.source
  == "${fakeBle}/bin/bluetooth-auth-prepare-le";
pkgs.runCommand "bluetooth-auth-integration-tests"
  {
    nativeBuildInputs = [
      pkgs.python3
      pkgs.nodejs
      pkgs.linux-pam
    ];
  }
  ''
      test -x ${package}/bin/bluetooth-auth-link
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
      cat > pam/login <<'EOF'
    account required ${pkgs.linux-pam}/lib/security/pam_permit.so
    auth required ${pkgs.linux-pam}/lib/security/pam_permit.so
    ${lockerPam}
    auth required ${pkgs.linux-pam}/lib/security/pam_permit.so
    EOF
      cat > pam/greeter <<'EOF'
    ${greeterPam}
    EOF
      cat > pam/sudo-credentials <<'EOF'
    account required ${pkgs.linux-pam}/lib/security/pam_permit.so
    auth required ${pkgs.linux-pam}/lib/security/pam_permit.so
    ${sudoPam}
    EOF
      ${pkgs.python3}/bin/python ${./pam_integration.py} "$PWD/pam" ${pkgs.linux-pam}/lib/libpam.so.0
      ${pkgs.nodejs}/bin/node ${./polkit.js} ${polkitRules}
      touch "$out"
  ''
