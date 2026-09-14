{ pkgs, package }:

let
  utils = import (pkgs.path + "/nixos/lib/utils.nix") {
    inherit (pkgs) lib;
    config = { };
    inherit pkgs;
  };
  loggedSops = pkgs.writeShellScriptBin "sops" ''
    echo sops >> "$TEST_CALLS"
    if test "''${TEST_SOPS_FAIL_AFTER_OUTPUT:-}" = 1; then
      printf %s throwaway-pass
      exit 1
    fi
    exec ${pkgs.sops}/bin/sops "$@"
  '';
  system = import (pkgs.path + "/nixos/lib/eval-config.nix") {
    inherit (pkgs.stdenv.hostPlatform) system;
    modules = [
      (
        { config, lib, ... }:
        import ../modules/nixos/bluetooth-auth.nix {
          inherit config lib;
          bluetoothAuthPackage = package;
        }
      )
      {
        nixpkgs.overlays = [ (_: _: { sops = loggedSops; }) ];
        my.security.bluetoothAuth = {
          enable = true;
          trustedUser = "alice";
          device.address.file = "/proc/self/cwd/address";
          connection.timeoutMs = 100;
          gnomeKeyringUnlock = {
            enable = true;
            # Resolve fixtures in the isolated build's working directory.
            password = {
              sopsFile = "/proc/self/cwd/password.yaml";
              ageKeyFile = "/proc/self/cwd/age-key.txt";
            };
          };
        };
      }
    ];
  };
  unit = system.config.systemd.user.services.bluetooth-auth-keyring-unlock;
  busConfig = pkgs.writeText "keyring-test-bus.conf" ''
    <busconfig>
      <type>session</type>
      <listen>unix:tmpdir=/tmp</listen>
      <policy context="default">
        <allow send_destination="*"/>
        <allow receive_sender="*"/>
        <allow own="*"/>
      </policy>
    </busconfig>
  '';
  testScript = pkgs.writeShellScript "keyring-unlock-tests" ''
    set -euo pipefail
    trap 'status=$?; echo "keyring unlock test failed at line $LINENO" >&2; exit "$status"' ERR

    export XDG_CONFIG_HOME="$PWD/config" XDG_DATA_HOME="$PWD/data" XDG_RUNTIME_DIR="$PWD/runtime"
    mkdir -p "$XDG_CONFIG_HOME" "$XDG_DATA_HOME/keyrings" "$XDG_RUNTIME_DIR/keyring"
    chmod 700 "$XDG_RUNTIME_DIR" "$XDG_RUNTIME_DIR/keyring"
    export TEST_CALLS="$PWD/calls"
    export SOPS_AGE_KEY_FILE=${pkgs.lib.escapeShellArg unit.environment.SOPS_AGE_KEY_FILE}
    export DBUS_SYSTEM_BUS_ADDRESS="unix:path=$PWD/no-system-bus"
    export BT_AUTH_RUNTIME_DIR="$XDG_RUNTIME_DIR"

    cc -shared -fPIC ${./support/oneshot_hci.c} -o "$PWD/oneshot-hci.so"
    hci_preload="$PWD/oneshot-hci.so"
    hci_log="$PWD/hci-queries"
    printf '%s\n' 02:00:00:00:00:01 > address

    age-keygen -o age-key.txt
    recipient=$(age-keygen -y age-key.txt)
    write_password() {
      printf 'login_keyring_password: %s\n' "$1" |
        sops encrypt --age "$recipient" --input-type yaml --output-type yaml /dev/stdin > password.yaml
    }
    write_password throwaway-pass

    printf %s throwaway-pass |
      gnome-keyring-daemon --daemonize --login --control-directory="$XDG_RUNTIME_DIR/keyring" >/dev/null
    gnome-keyring-daemon --start --components=secrets --control-directory="$XDG_RUNTIME_DIR/keyring" >/dev/null

    locked() {
      busctl --user get-property org.freedesktop.secrets /org/freedesktop/secrets/aliases/login \
        org.freedesktop.Secret.Collection Locked
    }
    owner() {
      busctl --user call org.freedesktop.DBus /org/freedesktop/DBus \
        org.freedesktop.DBus GetNameOwner s org.freedesktop.secrets
    }
    lock() {
      busctl --user call org.freedesktop.secrets /org/freedesktop/secrets \
        org.freedesktop.Secret.Service Lock ao 1 /org/freedesktop/secrets/aliases/login >/dev/null
      test "$(locked)" = "b true"
    }
    reset_calls() {
      : > "$TEST_CALLS"
      : > "$hci_log"
    }
    no_sops_or_hci() {
      test ! -s "$TEST_CALLS"
      test ! -s "$hci_log"
    }
    queried_once() {
      test "$(cat "$hci_log")" = 1
    }
    unlock() {
      LD_PRELOAD="$hci_preload" BT_AUTH_HCI_SCENARIO="$1" BT_AUTH_HCI_LOG="$hci_log" \
        PATH=${pkgs.lib.escapeShellArg unit.environment.PATH} \
        ${unit.serviceConfig.ExecStart}
    }

    initial_owner=$(owner)
    test "$(locked)" = "b false"

    # An already-unlocked keyring does not inspect Bluetooth or start SOPS.
    reset_calls
    unlock encrypted
    no_sops_or_hci

    # Omitting the address unlocks without querying Bluetooth or needing its lock file.
    lock
    reset_calls
    LD_PRELOAD="$hci_preload" BT_AUTH_HCI_SCENARIO=error BT_AUTH_HCI_LOG="$hci_log" \
      PATH=${pkgs.lib.escapeShellArg unit.environment.PATH} \
      ${package}/bin/bluetooth-auth-keyring-unlock --sops-file password.yaml
    test ! -s "$hci_log"
    test "$(cat "$TEST_CALLS")" = sops
    test "$(locked)" = "b false"
    test "$(owner)" = "$initial_owner"

    # A concurrent attempt holds the lock; timing out must not decrypt or unlock.
    touch "$BT_AUTH_RUNTIME_DIR/hci0.lock"
    exec 9< "$BT_AUTH_RUNTIME_DIR/hci0.lock"
    flock --exclusive 9
    lock
    reset_calls
    unlock disconnected
    queried_once
    test ! -s "$TEST_CALLS"
    test "$(locked)" = "b true"

    reset_calls
    unlock unencrypted
    queried_once
    test ! -s "$TEST_CALLS"
    test "$(locked)" = "b true"

    # Another syntactically valid address is not the connected target.
    printf '%s\n' 02:00:00:00:00:02 > address
    reset_calls
    unlock encrypted
    queried_once
    test ! -s "$TEST_CALLS"
    test "$(locked)" = "b true"
    printf '%s\n' 02:00:00:00:00:01 > address
    flock --unlock 9
    exec 9<&-

    # Real SOPS ciphertext unlocks the same existing daemon through the Rust helper.
    reset_calls
    unlock encrypted
    queried_once
    test "$(cat "$TEST_CALLS")" = sops
    test "$(locked)" = "b false"
    test "$(owner)" = "$initial_owner"

    # A failing SOPS process must not use its output to unlock the keyring.
    lock
    reset_calls
    export TEST_SOPS_FAIL_AFTER_OUTPUT=1
    if unlock encrypted >/dev/null 2>&1; then exit 1; fi
    unset TEST_SOPS_FAIL_AFTER_OUTPUT
    queried_once
    test "$(cat "$TEST_CALLS")" = sops
    test "$(locked)" = "b true"
    test "$(owner)" = "$initial_owner"

    # A wrong password fails and preserves the locked collection and its daemon.
    write_password wrong-password
    reset_calls
    if unlock encrypted >/dev/null 2>&1; then exit 1; fi
    queried_once
    test "$(cat "$TEST_CALLS")" = sops
    test "$(locked)" = "b true"
    test "$(owner)" = "$initial_owner"

    # A missing decryption identity must also fail without unlocking.
    write_password throwaway-pass
    rm age-key.txt
    reset_calls
    if unlock encrypted >/dev/null 2>&1; then exit 1; fi
    queried_once
    test "$(cat "$TEST_CALLS")" = sops
    test "$(locked)" = "b true"

    # A Bluetooth query failure must not invoke SOPS or unlock anything.
    reset_calls
    if unlock error >/dev/null 2>&1; then exit 1; fi
    queried_once
    test ! -s "$TEST_CALLS"
    test "$(locked)" = "b true"

    # Losing Secret Service fails before Bluetooth or decryption.
    daemon_pid=$(busctl --user call org.freedesktop.DBus /org/freedesktop/DBus \
      org.freedesktop.DBus GetConnectionUnixProcessID s org.freedesktop.secrets)
    kill "''${daemon_pid#u }"
    for _ in {1..50}; do
      owner >/dev/null 2>&1 || break
      sleep 0.05
    done
    reset_calls
    if unlock encrypted >/dev/null 2>&1; then exit 1; fi
    no_sops_or_hci
  '';
in
assert unit.wantedBy == [ "graphical-session.target" ];
assert unit.after == [ "graphical-session.target" ];
assert unit.partOf == [ "graphical-session.target" ];
assert unit.unitConfig.ConditionUser == "alice";
assert unit.serviceConfig.Type == "oneshot";
assert unit.serviceConfig.RemainAfterExit;
assert unit.serviceConfig.TimeoutStartSec == "15100ms";
assert pkgs.lib.elem "f /run/bluetooth-auth/hci0.lock 0660 root bluetooth-auth-connect -"
  system.config.systemd.tmpfiles.rules;
assert
  unit.serviceConfig.ExecStart == utils.escapeSystemdExecArgs [
    "${package}/bin/bluetooth-auth-keyring-unlock"
    "--address-file"
    "/proc/self/cwd/address"
    "--timeout-ms"
    "100"
    "--sops-file"
    "/proc/self/cwd/password.yaml"
    "--sops-key"
    "login_keyring_password"
  ];
assert pkgs.lib.elem loggedSops unit.path;
pkgs.runCommand "bluetooth-auth-keyring-tests"
  {
    nativeBuildInputs = with pkgs; [
      age
      coreutils
      dbus
      gnome-keyring
      sops
      stdenv.cc
      systemd
      util-linux
    ];
  }
  ''
    dbus-run-session --config-file=${busConfig} -- ${testScript}
    touch "$out"
  ''
