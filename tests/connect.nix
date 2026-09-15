{ pkgs, package }:

let
  bluetoothPython = pkgs.python3.withPackages (ps: [ ps.dbus-next ]);
  fakeBluez = pkgs.writeText "bluetooth-auth-fake-bluez.py" ''
    import asyncio
    from dbus_next import BusType
    from dbus_next.aio import MessageBus
    from dbus_next.service import ServiceInterface, dbus_property

    class Adapter(ServiceInterface):
        def __init__(self):
            super().__init__("org.bluez.Adapter1")
            self.powered = False

        @dbus_property()
        def Powered(self) -> "b":
            return self.powered

        @Powered.setter
        def Powered(self, value: "b"):
            if self.powered != value:
                self.powered = value
                self.emit_properties_changed({"Powered": value})

    async def main():
        bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        bus.export("/org/bluez/hci0", Adapter())
        await bus.request_name("org.bluez")
        await bus.wait_for_disconnect()

    asyncio.run(main())
  '';
  sleepServices = [
    "systemd-suspend"
    "systemd-hibernate"
    "systemd-hybrid-sleep"
    "systemd-suspend-then-hibernate"
  ];
  fakeSleep = pkgs.writeShellScript "bluetooth-auth-test-sleep" ''
    while test -e /run/bluetooth-auth-sleep-hold; do ${pkgs.coreutils}/bin/sleep 0.05; done
    test ! -e /run/bluetooth-auth-sleep-fail
  '';
  fakeBluetoothAddress = pkgs.writeShellScript "bluetooth-auth-test-address" ''
    ${pkgs.coreutils}/bin/mkdir -p /run/noctalia-test
    printf '%s\n' 02:00:00:00:00:01 > /run/noctalia-test/address
    ${pkgs.coreutils}/bin/chown trusted /run/noctalia-test /run/noctalia-test/address
    ${pkgs.coreutils}/bin/chmod 0400 /run/noctalia-test/address
  '';
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
      (pkgs.writeShellScriptBin "bluetooth-auth-link" ''
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
      (pkgs.writeShellScriptBin "bluetooth-auth-prepare-le" ''
        set -eu
        read -r address
        test "$address" = 02:00:00:00:00:01
        id -u
        grep '^CapEff:' /proc/self/status
      '')
      (pkgs.writeShellScriptBin "bluetooth-auth-noctalia-auto-lock" ''
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
    { ... }:
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
      environment.systemPackages = [
        pkgs.socat
        pkgs.libcap
      ];
      services.dbus.enable = true;
      services.dbus.packages = [
        (pkgs.writeTextDir "share/dbus-1/system.d/bluetooth-auth-fake-bluez.conf" ''
          <busconfig>
            <policy user="root">
              <allow own="org.bluez"/>
              <allow send_destination="org.bluez"/>
            </policy>
          </busconfig>
        '')
      ];
      systemd.services.bluetooth = {
        wantedBy = [ "multi-user.target" ];
        serviceConfig = {
          Type = "dbus";
          BusName = "org.bluez";
          ExecStart = "${bluetoothPython}/bin/python ${fakeBluez}";
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
        trustedUser = "trusted";
        accessGroup = "test-bluetooth-auth";
        device.address.file = "/run/noctalia-test/address";
        connection.timeoutMs = 5000;
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
          sleepIntervalsMs = {
            unlockedConnected = 1100;
            unlockedDisconnected = 2200;
            lockedConnected = 3300;
            lockedDisconnected = 4400;
          };
        };
      }
    ];
  };
  autoConnectOnlySystem = import (pkgs.path + "/nixos/lib/eval-config.nix") {
    inherit (pkgs.stdenv.hostPlatform) system;
    modules = [
      common
      {
        my.security.bluetoothAuth.autoConnect.enable = true;
      }
    ];
  };
  bothSystem = import (pkgs.path + "/nixos/lib/eval-config.nix") {
    inherit (pkgs.stdenv.hostPlatform) system;
    modules = [
      common
      {
        my.security.bluetoothAuth = {
          noctaliaAutoLock.enable = true;
          autoConnect.enable = true;
        };
      }
    ];
  };
in
assert !(builtins.hasAttr "bluetooth-auth-connect" disabledSystem.config.systemd.sockets);
assert !(builtins.hasAttr "bluetooth-auth-power-monitor" disabledSystem.config.systemd.services);
assert !(builtins.hasAttr "bluetooth-auth-prepare-le" disabledSystem.config.security.wrappers);
assert builtins.hasAttr "bluetooth-auth-connect" autoLockSystem.config.systemd.sockets;
assert builtins.hasAttr "bluetooth-auth-connect" autoLockSystem.config.systemd.services;
assert autoLockSystem.config.hardware.bluetooth.settings.General.Experimental;
assert
  autoLockSystem.config.security.wrappers.bluetooth-auth-prepare-le.source
  == "${fakeBle}/bin/bluetooth-auth-prepare-le";
assert autoLockSystem.config.security.wrappers.bluetooth-auth-prepare-le.owner == "root";
assert
  autoLockSystem.config.security.wrappers.bluetooth-auth-prepare-le.group == "test-bluetooth-auth";
assert
  autoLockSystem.config.security.wrappers.bluetooth-auth-prepare-le.permissions == "u+rx,g+x,o-rwx";
assert
  autoLockSystem.config.security.wrappers.bluetooth-auth-prepare-le.capabilities
  == "cap_net_admin+ep";
assert !(builtins.hasAttr "bluetooth-auth-power-monitor" autoLockSystem.config.systemd.services);
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
assert pkgs.lib.hasInfix "/bin/bluetooth-auth-noctalia-auto-lock"
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
assert !(builtins.hasAttr "bluetooth-auth-connect" autoConnectOnlySystem.config.systemd.sockets);
assert builtins.hasAttr "bluetooth-auth-connect" autoConnectOnlySystem.config.systemd.services;
assert autoConnectOnlySystem.config.hardware.bluetooth.settings.General.Experimental;
assert builtins.hasAttr "bluetooth-auth-prepare-le" autoConnectOnlySystem.config.security.wrappers;
assert
  autoConnectOnlySystem.config.systemd.services.bluetooth-auth-power-monitor.serviceConfig.Type
  == "dbus";
assert
  autoConnectOnlySystem.config.systemd.services.bluetooth-auth-power-monitor.serviceConfig.BusName
  == "org.bluetooth_auth.PowerMonitor";
assert pkgs.lib.hasInfix "\"--address-file\" \"/run/noctalia-test/address\""
  autoConnectOnlySystem.config.systemd.services.bluetooth-auth-power-monitor.serviceConfig.ExecStart;
assert pkgs.lib.hasInfix "\"--timeout-ms\" \"5000\""
  autoConnectOnlySystem.config.systemd.services.bluetooth-auth-power-monitor.serviceConfig.ExecStart;
assert pkgs.lib.elem "bluetooth.service"
  autoConnectOnlySystem.config.systemd.services.bluetooth-auth-power-monitor.before;
assert pkgs.lib.any (
  rule: pkgs.lib.hasInfix "/run/bluetooth-auth/hci0.lock" rule
) autoConnectOnlySystem.config.systemd.tmpfiles.rules;
assert pkgs.lib.elem "bluetooth.service"
  autoConnectOnlySystem.config.systemd.services.bluetooth-auth-connect.wantedBy;
assert pkgs.lib.elem "bluetooth.service"
  autoConnectOnlySystem.config.systemd.services.bluetooth-auth-connect.after;
assert pkgs.lib.all (
  service:
  pkgs.lib.elem "bluetooth-auth-connect.service"
    autoConnectOnlySystem.config.systemd.services.${service}.onSuccess
) sleepServices;
assert autoLockSystem.config.systemd.services.bluetooth-auth-connect.wantedBy == [ ];
assert builtins.hasAttr "bluetooth-auth-connect" bothSystem.config.systemd.sockets;
assert builtins.hasAttr "bluetooth-auth-connect" bothSystem.config.systemd.services;
assert pkgs.lib.any (
  rule: pkgs.lib.hasInfix "/run/bluetooth-auth/hci0.lock" rule
) bothSystem.config.systemd.tmpfiles.rules;
pkgs.testers.runNixOSTest {
  name = "bluetooth-auth-connect";
  requiredFeatures.kvm = false;
  nodes = {
    machine = {
      imports = [ common ];
      # Replace only the sleep command; exercise the actual unit dependencies and OnSuccess.
      systemd.services =
        pkgs.lib.genAttrs sleepServices (_: {
          serviceConfig.ExecStart = [
            ""
            "${fakeSleep}"
          ];
        })
        // {
          fake-bluetooth-address = {
            requiredBy = [ "bluetooth-auth-power-monitor.service" ];
            before = [ "bluetooth-auth-power-monitor.service" ];
            serviceConfig = {
              Type = "oneshot";
              ExecStart = fakeBluetoothAddress;
            };
          };
          bluetooth-auth-power-monitor.serviceConfig.ExecStart = pkgs.lib.mkForce "${package}/bin/bluetooth-auth-power-monitor --address-file /run/noctalia-test/address --timeout-ms 5000";
        };
      my.security.bluetoothAuth = {
        autoConnect.enable = true;
        auth.sudo.enable = true;
        auth.polkit.enable = true;
        noctaliaAutoLock = {
          enable = true;
          sleepIntervalsMs = {
            unlockedConnected = 1100;
            unlockedDisconnected = 2200;
            lockedConnected = 3300;
            lockedDisconnected = 4400;
          };
        };
      };
    };
  };
  testScript = ''
    start_all()
    machine.wait_for_unit("bluetooth-auth-connect.socket", timeout=30)
    machine.wait_for_unit("bluetooth-auth-power-monitor.service", timeout=15)
    machine.wait_for_unit("bluetooth.service", timeout=15)
    machine.wait_until_succeeds("test $(wc -l < /run/bluetooth-auth-connect-test.log) = 1 && test $(systemctl show -p ActiveState --value bluetooth-auth-connect.service) = inactive", timeout=15)

    # Keep intentionally held mock connections alive on slow, software-emulated VMs.
    # Restore the production timeout before checking the timeout behavior below.
    connection_timeout_override = "/run/systemd/system/bluetooth-auth-connect.service.d/test-hold.conf"
    machine.succeed(f"mkdir -p $(dirname {connection_timeout_override}); printf '[Service]\\nTimeoutStartSec=infinity\\n' > {connection_timeout_override}; systemctl daemon-reload")
    machine.succeed("test $(systemctl show -p TimeoutStartUSec --value bluetooth-auth-connect.service) = infinity")

    # BlueZ must finish restarting while the triggered connection remains held.
    machine.succeed("touch /run/bluetooth-auth-connect-hold")
    machine.succeed("timeout 30 systemctl restart bluetooth.service")
    machine.succeed("systemctl is-active --quiet bluetooth.service")
    machine.wait_until_succeeds("test $(wc -l < /run/bluetooth-auth-connect-test.log) = 2")
    machine.succeed("test $(systemctl show -p ActiveState --value bluetooth-auth-connect.service) = activating")
    machine.succeed("rm /run/bluetooth-auth-connect-hold")
    machine.wait_until_succeeds("test $(systemctl show -p ActiveState --value bluetooth-auth-connect.service) = inactive")

    # No connection starts until the sleep operation has completed successfully.
    machine.succeed("touch /run/bluetooth-auth-sleep-hold")
    machine.succeed("systemctl start --no-block systemd-suspend.service")
    machine.wait_until_succeeds("test $(systemctl show -p ActiveState --value systemd-suspend.service) = activating")
    machine.succeed("test $(wc -l < /run/bluetooth-auth-connect-test.log) = 2")
    machine.succeed("rm /run/bluetooth-auth-sleep-hold")
    machine.wait_until_succeeds("test $(wc -l < /run/bluetooth-auth-connect-test.log) = 3 && test $(systemctl show -p ActiveState --value bluetooth-auth-connect.service) = inactive")
    for count, service in enumerate(["systemd-hibernate", "systemd-hybrid-sleep", "systemd-suspend-then-hibernate"], start=4):
        machine.succeed(f"systemctl start {service}.service")
        machine.wait_until_succeeds(f"test $(wc -l < /run/bluetooth-auth-connect-test.log) = {count} && test $(systemctl show -p ActiveState --value bluetooth-auth-connect.service) = inactive")

    # A failed sleep operation does not trigger a connection.
    machine.succeed("touch /run/bluetooth-auth-sleep-fail")
    machine.fail("systemctl start systemd-suspend.service")
    machine.succeed("sleep 1; test $(wc -l < /run/bluetooth-auth-connect-test.log) = 6")
    machine.succeed("rm /run/bluetooth-auth-sleep-fail")

    # Connection failure is final for this event; the next event can start another attempt.
    machine.succeed("touch /run/bluetooth-auth-connect-fail")
    machine.succeed("systemctl start systemd-suspend.service")
    machine.wait_until_succeeds("test $(wc -l < /run/bluetooth-auth-connect-test.log) = 7 && test $(systemctl show -p ActiveState --value bluetooth-auth-connect.service) = failed")
    machine.succeed("sleep 1; test $(wc -l < /run/bluetooth-auth-connect-test.log) = 7")
    machine.succeed("rm /run/bluetooth-auth-connect-fail")
    machine.succeed("systemctl start systemd-suspend.service")
    machine.wait_until_succeeds("test $(wc -l < /run/bluetooth-auth-connect-test.log) = 8 && test $(systemctl show -p ActiveState --value bluetooth-auth-connect.service) = inactive")

    machine.succeed("rm /run/bluetooth-auth-connect-test.log")
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
    machine.succeed("test $(stat -c '%a:%U:%G' /run/wrappers/bin/bluetooth-auth-prepare-le) = 510:root:test-bluetooth-auth")
    machine.succeed("getcap /run/wrappers/bin/bluetooth-auth-prepare-le | grep -E 'cap_setpcap,cap_net_admin=ep|cap_net_admin,cap_setpcap=ep'")
    machine.fail("getcap /run/wrappers/bin/bluetooth-auth-prepare-le | grep cap_sys_admin")
    machine.succeed("echo 02:00:00:00:00:01 | runuser -u trusted -- /run/wrappers/bin/bluetooth-auth-prepare-le > /run/bluetooth-auth-prepare-le-capabilities")
    machine.succeed("test $(sed -n '1p' /run/bluetooth-auth-prepare-le-capabilities) = $(id -u trusted)")
    machine.succeed("test $(sed -n '2p' /run/bluetooth-auth-prepare-le-capabilities | cut -f2) = 0000000000001000")
    machine.fail("echo 02:00:00:00:00:01 | runuser -u other -- /run/wrappers/bin/bluetooth-auth-prepare-le")
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

    machine.succeed(f"rm {connection_timeout_override}; systemctl daemon-reload")
    machine.succeed("test $(systemctl show -p TimeoutStartUSec --value bluetooth-auth-connect.service) = 6s")
    machine.succeed("touch /run/bluetooth-auth-connect-hold")
    machine.succeed("printf x | runuser -u trusted -- socat -u - UNIX-SENDTO:/run/bluetooth-auth/connect.sock")
    machine.wait_until_succeeds("test $(wc -l < /run/bluetooth-auth-connect-test.log) = 12")
    machine.wait_until_succeeds("test $(systemctl show -p ActiveState --value bluetooth-auth-connect.service) = failed")
    machine.succeed("sleep 1; test $(wc -l < /run/bluetooth-auth-connect-test.log) = 12")
    machine.succeed("rm /run/bluetooth-auth-connect-hold")
  '';
}
