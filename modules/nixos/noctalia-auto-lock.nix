{
  config,
  lib,
  pkgs,
  utils,
  ...
}:

let
  cfg = config.my.security.bluetoothAuth;
in
{
  options.my.security.bluetoothAuth.noctaliaAutoLock = {
    enable = lib.mkEnableOption "automatic locking with Noctalia v5 when the Bluetooth auth device is disconnected";

    unlockedConnectedIntervalMilliseconds = lib.mkOption {
      type = lib.types.ints.between 1 2147483647;
      default = 30000;
      description = "Delay after processing while the session is unlocked with an encrypted BLE connection, in milliseconds.";
    };

    unlockedDisconnectedIntervalMilliseconds = lib.mkOption {
      type = lib.types.ints.between 1 2147483647;
      default = 30000;
      description = "Delay between checks while unlocked without an encrypted BLE connection, in milliseconds.";
    };

    lockedConnectedIntervalMilliseconds = lib.mkOption {
      type = lib.types.ints.between 1 2147483647;
      default = 120000;
      description = "Delay between checks while locked with an encrypted BLE connection, in milliseconds.";
    };

    lockedDisconnectedIntervalMilliseconds = lib.mkOption {
      type = lib.types.ints.between 1 2147483647;
      default = 60000;
      description = "Delay between checks while locked without an encrypted BLE connection, in milliseconds.";
    };
  };

  config = lib.mkIf (cfg.enable && cfg.noctaliaAutoLock.enable) {
    systemd.user.services.bluetooth-auth-auto-lock = {
      description = "Lock the Noctalia session when the Bluetooth auth device is disconnected";
      wantedBy = [ "graphical-session.target" ];
      partOf = [ "graphical-session.target" ];
      # The graphical session must import WAYLAND_DISPLAY before reaching this target.
      after = [ "graphical-session.target" ];
      unitConfig.ConditionUser = cfg.user;
      path = [
        pkgs.coreutils
        "/etc/profiles/per-user/${cfg.user}"
      ];
      startLimitIntervalSec = 0;

      serviceConfig = {
        Type = "exec";
        ExecStart = utils.escapeSystemdExecArgs [
          "${cfg.package}/bin/bluetooth-auth-noctalia-auto-lock"
          "--address-file"
          cfg.bluetoothAddressFile
          "--timeout-ms"
          (toString cfg.connect.timeoutMilliseconds)
          "--unlocked-connected-interval-ms"
          (toString cfg.noctaliaAutoLock.unlockedConnectedIntervalMilliseconds)
          "--unlocked-disconnected-interval-ms"
          (toString cfg.noctaliaAutoLock.unlockedDisconnectedIntervalMilliseconds)
          "--locked-connected-interval-ms"
          (toString cfg.noctaliaAutoLock.lockedConnectedIntervalMilliseconds)
          "--locked-disconnected-interval-ms"
          (toString cfg.noctaliaAutoLock.lockedDisconnectedIntervalMilliseconds)
        ];
        Restart = "on-failure";
        RestartSec = "5s";
        TimeoutStopSec = "1s";
      };
    };
  };
}
