{ pkgs }:

let
  fakeNoctalia = pkgs.writeShellScriptBin "noctalia" ''
    set -eu
    test "$#" -eq 2
    test "$1" = msg
    test "$2" = status
    test "$(id -un)" = trusted
    test "$XDG_RUNTIME_DIR" = "$(${pkgs.systemd}/bin/loginctl show-user trusted --property=RuntimePath --value)"
    test "$WAYLAND_DISPLAY" = test-wayland
    echo status > /run/noctalia-test/called
  '';
  fakeBle = pkgs.symlinkJoin {
    name = "fake-bluetooth-auth";
    paths = [
      (pkgs.writeShellScriptBin "ble-link" ''
        set -eu
        test "$#" -eq 4
        test "$1" = --address-file
        test "$2" = /run/noctalia-test/address
        test "$3" = --connect
        test "$4" = 5000
        echo start >> /run/bluetooth-auth-connect-test.log
        while test -e /run/bluetooth-auth-connect-hold; do ${pkgs.coreutils}/bin/sleep 0.05; done
        test ! -e /run/bluetooth-auth-connect-fail
      '')
      (pkgs.writeShellScriptBin "ble-noctalia-auto-lock" ''
        set -eu
        test "$(id -u)" -ne 0
        test "$(id -un)" = trusted
        test "$#" -eq 12
        test "$1" = --address-file
        test "$2" = /run/noctalia-test/address
        test "$(cat "$2")" = private-fake-address
        test "$3" = --timeout-ms
        test "$4" = 5000
        test "$5" = --unlocked-connected-interval-ms
        test "$6" = 1100
        test "$7" = --unlocked-disconnected-interval-ms
        test "$8" = 2200
        test "$9" = --locked-connected-interval-ms
        test "''${10}" = 3300
        test "''${11}" = --locked-disconnected-interval-ms
        test "''${12}" = 4400
        test "$XDG_RUNTIME_DIR" = "$(${pkgs.systemd}/bin/loginctl show-user trusted --property=RuntimePath --value)"
        test "$WAYLAND_DISPLAY" = test-wayland
        ${pkgs.util-linux}/bin/flock -xn /run/bluetooth-auth/hci0.lock ${pkgs.coreutils}/bin/true
        noctalia msg status
        echo start >> /run/noctalia-test/auto-lock.log
        touch /run/noctalia-test/auto-lock-running
        trap 'rm -f /run/noctalia-test/auto-lock-running' EXIT
        while true; do
          test ! -e /run/noctalia-test/auto-lock-fail || exit 1
          ${pkgs.coreutils}/bin/sleep 0.05
        done
      '')
    ];
  };
  common =
    { config, lib, ... }:
    {
      imports = [
        (
          { config, lib, ... }:
          import ../modules/nixos/bluetooth-auth.nix {
            inherit config lib;
            bluetoothAuthPackage = fakeBle;
          }
        )
      ];
      networking.useDHCP = false;
      environment.systemPackages = [ pkgs.socat ];
      systemd.services.bluetooth = {
        wantedBy = [ "multi-user.target" ];
        serviceConfig = {
          Type = "oneshot";
          RemainAfterExit = true;
          ExecStart = "${pkgs.coreutils}/bin/true";
        };
      };
      users.users.trusted.isNormalUser = true;
      users.users.trusted.packages = [ fakeNoctalia ];
      users.users.other.isNormalUser = true;
      systemd.user.targets.test-session = {
        description = "Simulated graphical session";
        bindsTo = [ "graphical-session.target" ];
        wants = [ "graphical-session-pre.target" ];
        after = [ "graphical-session-pre.target" ];
      };
      systemd.user.services.test-session-environment = {
        description = "Import test graphical-session environment";
        wantedBy = [ "test-session.target" ];
        before = [ "graphical-session.target" ];
        partOf = [ "graphical-session.target" ];
        serviceConfig = {
          Type = "oneshot";
          RemainAfterExit = true;
          ExecStart = "${pkgs.systemd}/bin/systemctl --user set-environment WAYLAND_DISPLAY=test-wayland";
          ExecStop = "${pkgs.systemd}/bin/systemctl --user unset-environment WAYLAND_DISPLAY";
        };
      };
      my.security.bluetoothAuth = {
        enable = true;
        package = fakeBle;
        user = "trusted";
        group = "test-bluetooth-auth";
        bluetoothAddressFile = "/run/noctalia-test/address";
        connect.timeoutMilliseconds = 5000;
      };
    };
  disabledSystem = import (pkgs.path + "/nixos/lib/eval-config.nix") {
    inherit (pkgs.stdenv.hostPlatform) system;
    modules = [
      common
      {
        my.security.bluetoothAuth.enable = pkgs.lib.mkForce false;
      }
    ];
  };
  autoLockSystem = import (pkgs.path + "/nixos/lib/eval-config.nix") {
    inherit (pkgs.stdenv.hostPlatform) system;
    modules = [
      common
      {
        my.security.bluetoothAuth.noctaliaAutoLock = {
          enable = true;
          unlockedConnectedIntervalMilliseconds = 1100;
          unlockedDisconnectedIntervalMilliseconds = 2200;
          lockedConnectedIntervalMilliseconds = 3300;
          lockedDisconnectedIntervalMilliseconds = 4400;
        };
      }
    ];
  };
in
assert !(builtins.hasAttr "bluetooth-auth-connect" disabledSystem.config.systemd.sockets);
assert builtins.hasAttr "bluetooth-auth-connect" autoLockSystem.config.systemd.sockets;
assert builtins.hasAttr "bluetooth-auth-connect" autoLockSystem.config.systemd.services;
assert pkgs.lib.any (
  rule: pkgs.lib.hasInfix "/run/bluetooth-auth/hci0.lock" rule
) autoLockSystem.config.systemd.tmpfiles.rules;
assert !(builtins.hasAttr "bluetooth-auth-auto-lock" autoLockSystem.config.systemd.services);
assert builtins.hasAttr "bluetooth-auth-auto-lock" autoLockSystem.config.systemd.user.services;
assert
  autoLockSystem.config.systemd.user.services.bluetooth-auth-auto-lock.wantedBy
  == [ "graphical-session.target" ];
assert
  autoLockSystem.config.systemd.user.services.bluetooth-auth-auto-lock.after
  == [ "graphical-session.target" ];
assert
  autoLockSystem.config.systemd.user.services.bluetooth-auth-auto-lock.partOf
  == [ "graphical-session.target" ];
assert
  autoLockSystem.config.systemd.user.services.bluetooth-auth-auto-lock.unitConfig.ConditionUser
  == "trusted";
assert
  autoLockSystem.config.systemd.user.services.bluetooth-auth-auto-lock.serviceConfig.Type == "exec";
assert
  autoLockSystem.config.systemd.user.services.bluetooth-auth-auto-lock.serviceConfig.Restart
  == "on-failure";
assert
  autoLockSystem.config.systemd.user.services.bluetooth-auth-auto-lock.serviceConfig.RestartSec
  == "5s";
assert
  autoLockSystem.config.systemd.user.services.bluetooth-auth-auto-lock.serviceConfig.TimeoutStopSec
  == "1s";
assert !(builtins.hasAttr "bluetooth-auth-auto-lock" autoLockSystem.config.systemd.timers);
assert autoLockSystem.config.systemd.user.services.bluetooth-auth-auto-lock.script == "";
assert pkgs.lib.hasInfix "/bin/ble-noctalia-auto-lock"
  autoLockSystem.config.systemd.user.services.bluetooth-auth-auto-lock.serviceConfig.ExecStart;
assert pkgs.lib.hasInfix "\"--address-file\" \"/run/noctalia-test/address\""
  autoLockSystem.config.systemd.user.services.bluetooth-auth-auto-lock.serviceConfig.ExecStart;
assert pkgs.lib.hasInfix "\"--timeout-ms\" \"5000\""
  autoLockSystem.config.systemd.user.services.bluetooth-auth-auto-lock.serviceConfig.ExecStart;
assert pkgs.lib.hasInfix "\"--unlocked-connected-interval-ms\" \"1100\""
  autoLockSystem.config.systemd.user.services.bluetooth-auth-auto-lock.serviceConfig.ExecStart;
assert pkgs.lib.hasInfix "\"--unlocked-disconnected-interval-ms\" \"2200\""
  autoLockSystem.config.systemd.user.services.bluetooth-auth-auto-lock.serviceConfig.ExecStart;
assert pkgs.lib.hasInfix "\"--locked-connected-interval-ms\" \"3300\""
  autoLockSystem.config.systemd.user.services.bluetooth-auth-auto-lock.serviceConfig.ExecStart;
assert pkgs.lib.hasInfix "\"--locked-disconnected-interval-ms\" \"4400\""
  autoLockSystem.config.systemd.user.services.bluetooth-auth-auto-lock.serviceConfig.ExecStart;
pkgs.testers.runNixOSTest {
  name = "bluetooth-auth-connect";
  requiredFeatures.kvm = false;
  nodes = {
    machine = {
      imports = [ common ];
      my.security.bluetoothAuth = {
        sudoAuth.enable = true;
        polkitAuth.enable = true;
        noctaliaAutoLock = {
          enable = true;
          unlockedConnectedIntervalMilliseconds = 1100;
          unlockedDisconnectedIntervalMilliseconds = 2200;
          lockedConnectedIntervalMilliseconds = 3300;
          lockedDisconnectedIntervalMilliseconds = 4400;
        };
      };
    };
  };
  testScript = ''
    start_all()
    machine.wait_for_unit("bluetooth-auth-connect.socket")
    machine.succeed("rm -f /run/noctalia-test/auto-lock.log /run/noctalia-test/auto-lock-running /run/noctalia-test/auto-lock-fail")
    machine.succeed("mkdir -p /run/noctalia-test; chown trusted /run/noctalia-test; printf private-fake-address >/run/noctalia-test/address; chown trusted /run/noctalia-test/address; chmod 0400 /run/noctalia-test/address")
    machine.succeed("loginctl enable-linger trusted; systemctl start user@$(id -u trusted).service")
    machine.wait_until_succeeds("runtime_dir=$(loginctl show-user trusted --property=RuntimePath --value); test -n \"$runtime_dir\" && test -d \"$runtime_dir\"", timeout=15)
    machine.succeed("runtime_dir=$(loginctl show-user trusted --property=RuntimePath --value); runuser -u trusted -- env XDG_RUNTIME_DIR=$runtime_dir DBUS_SESSION_BUS_ADDRESS=unix:path=$runtime_dir/bus systemctl --user start test-session.target")
    machine.wait_until_succeeds("test -e /run/noctalia-test/auto-lock-running && test -e /run/noctalia-test/called && test $(wc -l < /run/noctalia-test/auto-lock.log) = 1", timeout=15)
    machine.succeed("sleep 1; test $(wc -l < /run/noctalia-test/auto-lock.log) = 1")
    machine.succeed("other_uid=$(id -u other); loginctl enable-linger other; systemctl start user@$other_uid.service; runuser -u other -- env XDG_RUNTIME_DIR=/run/user/$other_uid DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$other_uid/bus systemctl --user start bluetooth-auth-auto-lock.service")
    machine.succeed("other_uid=$(id -u other); ! runuser -u other -- env XDG_RUNTIME_DIR=/run/user/$other_uid DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$other_uid/bus systemctl --user is-active --quiet bluetooth-auth-auto-lock.service")
    machine.succeed("test $(wc -l < /run/noctalia-test/auto-lock.log) = 1")
    machine.succeed("touch /run/noctalia-test/auto-lock-fail")
    machine.wait_until_succeeds("test ! -e /run/noctalia-test/auto-lock-running", timeout=15)
    machine.wait_until_succeeds("test $(wc -l < /run/noctalia-test/auto-lock.log) -ge 2", timeout=15)
    machine.succeed("rm /run/noctalia-test/auto-lock-fail")
    machine.wait_until_succeeds("runtime_dir=$(loginctl show-user trusted --property=RuntimePath --value); test -e /run/noctalia-test/auto-lock-running && test $(wc -l < /run/noctalia-test/auto-lock.log) -ge 3 && runuser -u trusted -- env XDG_RUNTIME_DIR=$runtime_dir DBUS_SESSION_BUS_ADDRESS=unix:path=$runtime_dir/bus systemctl --user is-active --quiet bluetooth-auth-auto-lock.service", timeout=15)
    machine.succeed("runtime_dir=$(loginctl show-user trusted --property=RuntimePath --value); runuser -u trusted -- env XDG_RUNTIME_DIR=$runtime_dir DBUS_SESSION_BUS_ADDRESS=unix:path=$runtime_dir/bus systemctl --user stop graphical-session.target")
    machine.wait_until_succeeds("runtime_dir=$(loginctl show-user trusted --property=RuntimePath --value); ! runuser -u trusted -- env XDG_RUNTIME_DIR=$runtime_dir DBUS_SESSION_BUS_ADDRESS=unix:path=$runtime_dir/bus systemctl --user is-active --quiet bluetooth-auth-auto-lock.service")
    machine.succeed("auto_lock_starts=$(wc -l < /run/noctalia-test/auto-lock.log); sleep 6; test $(wc -l < /run/noctalia-test/auto-lock.log) = $auto_lock_starts")
    machine.succeed("runtime_dir=$(loginctl show-user trusted --property=RuntimePath --value); runuser -u trusted -- env XDG_RUNTIME_DIR=$runtime_dir DBUS_SESSION_BUS_ADDRESS=unix:path=$runtime_dir/bus systemctl --user start test-session.target")
    machine.wait_until_succeeds("runtime_dir=$(loginctl show-user trusted --property=RuntimePath --value); test -e /run/noctalia-test/auto-lock-running && runuser -u trusted -- env XDG_RUNTIME_DIR=$runtime_dir DBUS_SESSION_BUS_ADDRESS=unix:path=$runtime_dir/bus systemctl --user is-active --quiet bluetooth-auth-auto-lock.service", timeout=15)
    machine.succeed("runtime_dir=$(loginctl show-user trusted --property=RuntimePath --value); runuser -u trusted -- env XDG_RUNTIME_DIR=$runtime_dir DBUS_SESSION_BUS_ADDRESS=unix:path=$runtime_dir/bus systemctl --user stop bluetooth-auth-auto-lock.service")
    machine.wait_until_succeeds("runtime_dir=$(loginctl show-user trusted --property=RuntimePath --value); ! runuser -u trusted -- env XDG_RUNTIME_DIR=$runtime_dir DBUS_SESSION_BUS_ADDRESS=unix:path=$runtime_dir/bus systemctl --user is-active --quiet bluetooth-auth-auto-lock.service")
    machine.succeed("auto_lock_starts=$(wc -l < /run/noctalia-test/auto-lock.log); sleep 6; test $(wc -l < /run/noctalia-test/auto-lock.log) = $auto_lock_starts")
    machine.succeed("test -d /run/bluetooth-auth && test -f /run/bluetooth-auth/hci0.lock")
    machine.succeed("test $(stat -c '%a:%U:%G' /run/bluetooth-auth) = 750:root:test-bluetooth-auth")
    machine.succeed("test $(stat -c '%a:%U:%G' /run/bluetooth-auth/hci0.lock) = 660:root:test-bluetooth-auth")
    machine.succeed("test $(stat -c '%a:%U:%G' /run/bluetooth-auth/connect.sock) = 660:root:test-bluetooth-auth")
    machine.succeed("stat -c %i /run/bluetooth-auth/hci0.lock > /run/lock-inode; stat -c %i /run/bluetooth-auth/connect.sock > /run/socket-inode")
    machine.succeed("test $(id -nG trusted | tr ' ' '\\n' | grep -x test-bluetooth-auth)")
    machine.succeed("test $(id -nG polkituser | tr ' ' '\\n' | grep -x test-bluetooth-auth)")
    machine.fail("printf x | runuser -u other -- socat -u - UNIX-SENDTO:/run/bluetooth-auth/connect.sock")

    machine.succeed("touch /run/bluetooth-auth-connect-hold")
    machine.succeed("printf x | runuser -u trusted -- socat -u - UNIX-SENDTO:/run/bluetooth-auth/connect.sock")
    machine.wait_until_succeeds("test $(wc -l < /run/bluetooth-auth-connect-test.log) = 1")
    machine.succeed("printf x | runuser -u polkituser -- socat -u - UNIX-SENDTO:/run/bluetooth-auth/connect.sock")
    machine.succeed("sleep 1; test $(wc -l < /run/bluetooth-auth-connect-test.log) = 1")
    machine.succeed("rm /run/bluetooth-auth-connect-hold")
    machine.wait_until_succeeds("test $(systemctl show -p ActiveState --value bluetooth-auth-connect.service) = inactive")
    machine.succeed("sleep 1; test $(wc -l < /run/bluetooth-auth-connect-test.log) = 1")

    machine.succeed("touch /run/bluetooth-auth-connect-fail")
    # Repeated failures must not permanently disable either activation unit.
    for attempt in range(2, 11):
        machine.succeed("printf x | runuser -u trusted -- socat -u - UNIX-SENDTO:/run/bluetooth-auth/connect.sock")
        machine.wait_until_succeeds(f"test $(wc -l < /run/bluetooth-auth-connect-test.log) = {attempt}")
        machine.wait_until_succeeds("test $(systemctl show -p ActiveState --value bluetooth-auth-connect.service) = failed")
    machine.succeed("sleep 1; test $(wc -l < /run/bluetooth-auth-connect-test.log) = 10")
    machine.succeed("rm /run/bluetooth-auth-connect-fail")
    machine.succeed("printf x | runuser -u trusted -- socat -u - UNIX-SENDTO:/run/bluetooth-auth/connect.sock")
    machine.wait_until_succeeds("test $(wc -l < /run/bluetooth-auth-connect-test.log) = 11")
    machine.wait_until_succeeds("test $(systemctl show -p ActiveState --value bluetooth-auth-connect.service) = inactive")
    machine.succeed("test -S /run/bluetooth-auth/connect.sock && test -f /run/bluetooth-auth/hci0.lock")
    machine.succeed("test $(stat -c %i /run/bluetooth-auth/hci0.lock) = $(cat /run/lock-inode); test $(stat -c %i /run/bluetooth-auth/connect.sock) = $(cat /run/socket-inode)")

    machine.succeed("touch /run/bluetooth-auth-connect-hold")
    machine.succeed("printf x | runuser -u trusted -- socat -u - UNIX-SENDTO:/run/bluetooth-auth/connect.sock")
    machine.wait_until_succeeds("test $(wc -l < /run/bluetooth-auth-connect-test.log) = 12")
    machine.wait_until_succeeds("test $(systemctl show -p ActiveState --value bluetooth-auth-connect.service) = failed")
    machine.succeed("sleep 1; test $(wc -l < /run/bluetooth-auth-connect-test.log) = 12")
    machine.succeed("rm /run/bluetooth-auth-connect-hold")
  '';
}
