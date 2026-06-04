{
  config,
  lib,
  ...
}:

let
  cfg = config.my.security.bluetoothAuth;
  configFile = builtins.toFile "bluetooth-auth-config.json" (builtins.toJSON cfg.config);
  helper = "${cfg.package}/bin/bluetooth-auth-oneshot-auth";
  allowedActions = builtins.toJSON cfg.config.polkitAuth.allowedActions;
  trustedUser = builtins.toJSON cfg.config.user;
in
{
  options.my.security.bluetoothAuth.polkitAuth.enable =
    lib.mkEnableOption "polkit authentication bypass when the Bluetooth device is connected";

  config = lib.mkIf (cfg.enable && cfg.polkitAuth.enable) {
    security.polkit.enable = true;

    security.polkit.extraConfig = ''
      polkit.addRule(function(action, subject) {
        var allowedActions = ${allowedActions};

        if (
          !subject.user ||
          subject.user != ${trustedUser} ||
          !subject.local ||
          !subject.active
        ) {
          return polkit.Result.NOT_HANDLED;
        }

        if (allowedActions.indexOf(action.id) == -1) {
          return polkit.Result.NOT_HANDLED;
        }

        try {
          polkit.spawn([
            ${builtins.toJSON helper},
            ${builtins.toJSON "${configFile}"},
            "polkit"
          ]);
          return polkit.Result.YES;
        } catch (error) {
          return polkit.Result.NOT_HANDLED;
        }
      });
    '';
  };
}
