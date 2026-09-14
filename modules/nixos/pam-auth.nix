{
  config,
  lib,
  service,
  name,
  allowRuser ? false,
}:

let
  cfg = config.my.security.bluetoothAuth;
  order = (config.security.pam.services.${service}.rules.auth.unix.order or 11700) - 100;
  pam = "${config.security.pam.package}/lib/security";
in
{
  "${name}-user" = {
    order = order - 2;
    # These guards leave password authentication available for other users.
    control = if allowRuser then "[success=1 default=ignore]" else "[success=ignore default=1]";
    modulePath = "${pam}/pam_succeed_if.so";
    args = [
      "quiet"
      "user"
      "="
      cfg.user
    ];
  };

  ${name} = {
    inherit order;
    control = "sufficient";
    modulePath = "${pam}/pam_exec.so";
    args = [
      "seteuid"
      "quiet"
      "${cfg.package}/bin/bluetooth-auth-link"
      "--address-file"
      cfg.bluetoothAddressFile
      "--connect"
      "-1"
    ];
  };
}
// lib.optionalAttrs allowRuser {
  "${name}-ruser" = {
    order = order - 1;
    # sudo may authenticate root on behalf of the trusted requesting user.
    control = "[success=ignore default=1]";
    modulePath = "${pam}/pam_succeed_if.so";
    args = [
      "quiet"
      "ruser"
      "="
      cfg.user
    ];
  };
}
