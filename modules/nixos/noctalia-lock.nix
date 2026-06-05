{
  config,
  lib,
  pkgs,
  ...
}:

let
  cfg = config.my.security.bluetoothAuth;
  inherit (cfg) user;
in
{
  config = lib.mkIf (cfg.enable && cfg.autoLock.enable) {
    systemd.services.noctalia-lock = {
      description = "Trigger the user's Noctalia lock service";
      path = [
        pkgs.coreutils
        pkgs.systemd
        pkgs.util-linux
      ];
      serviceConfig.Type = "oneshot";
      script = ''
        set -euo pipefail

        user=${lib.escapeShellArg user}
        runtime_dir="$(loginctl show-user "$user" --property=RuntimePath --value)"

        if [ ! -d "$runtime_dir" ]; then
          echo "noctalia-lock: runtime dir not found for $user: $runtime_dir" >&2
          exit 1
        fi

        if [ ! -S "$runtime_dir/bus" ]; then
          echo "noctalia-lock: user bus not found for $user: $runtime_dir/bus" >&2
          exit 1
        fi

        exec runuser -u "$user" -- env \
          XDG_RUNTIME_DIR="$runtime_dir" \
          DBUS_SESSION_BUS_ADDRESS="unix:path=$runtime_dir/bus" \
          systemctl --user start --wait noctalia-lock.service
      '';
    };

    systemd.user.services.noctalia-lock = {
      description = "Lock the graphical session with Noctalia";
      unitConfig.ConditionUser = user;
      serviceConfig = {
        Type = "oneshot";
        ExecStart = lib.escapeShellArgs [
          "/etc/profiles/per-user/${user}/bin/noctalia-shell"
          "ipc"
          "call"
          "lockScreen"
          "lock"
        ];
      };
    };
  };
}
