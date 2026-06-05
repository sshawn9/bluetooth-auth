{
  config,
  lib,
  ...
}:

let
  cfg = config.my.security.bluetoothAuth;
  mkSettingsFile = import ./settings-file.nix;
  settingsFile = mkSettingsFile cfg;
  pamService = cfg.greetdAuth.pamService;
  earlyMediaEndpoints = cfg.greetdAuth.earlyMediaEndpoints.enable;
  trustedUser = cfg.user;
in
{
  options.my.security.bluetoothAuth.greetdAuth = {
    enable = lib.mkEnableOption "greetd PAM authentication bypass when the Bluetooth device is connected";

    pamService = lib.mkOption {
      type = lib.types.str;
      default = "greetd";
      example = "greetd";
      description = "PAM service name used by greetd.";
    };

    timeoutSeconds = lib.mkOption {
      type = lib.types.ints.between 1 2147483647;
      default = 2;
      description = "Maximum time to wait for the Bluetooth connection check during greetd PAM auth.";
    };

    earlyMediaEndpoints.enable = lib.mkEnableOption ''
      early target-user PipeWire and WirePlumber startup for BlueZ media
      endpoint registration before greetd authentication
    '';
  };

  config = lib.mkIf (cfg.enable && cfg.greetdAuth.enable) {
    assertions = lib.optionals earlyMediaEndpoints [
      {
        assertion = trustedUser != "";
        message = ''
          my.security.bluetoothAuth.user must be set when
          greetdAuth.earlyMediaEndpoints.enable is enabled.
        '';
      }
      {
        assertion = config.services.pipewire.enable or false;
        message = ''
          services.pipewire.enable must be enabled when
          greetdAuth.earlyMediaEndpoints.enable is enabled.
        '';
      }
      {
        assertion = config.services.pipewire.wireplumber.enable or false;
        message = ''
          services.pipewire.wireplumber.enable must be enabled when
          greetdAuth.earlyMediaEndpoints.enable is enabled.
        '';
      }
    ];

    security.pam.services.${pamService}.rules.auth.bluetooth-auth-greetd = {
      order = (config.security.pam.services.${pamService}.rules.auth.unix.order or 11700) - 100;
      control = "sufficient";
      modulePath = "${config.security.pam.package}/lib/security/pam_exec.so";
      args = [
        "seteuid"
        "quiet"
        "${cfg.package}/bin/bluetooth-auth-oneshot-auth"
        settingsFile
        "greetd"
      ];
    };

    users = lib.mkIf earlyMediaEndpoints {
      manageLingering = lib.mkDefault true;
      users.${trustedUser}.linger = lib.mkDefault true;
    };

    systemd.user.services = lib.mkIf earlyMediaEndpoints {
      pipewire.wantedBy = [ "default.target" ];
      wireplumber.wantedBy = [ "default.target" ];
    };
  };
}
