{ lib, pkgs }:

let
  inherit ((fromTOML (builtins.readFile ./Cargo.toml))) package;
in
pkgs.rustPlatform.buildRustPackage {
  pname = package.name;
  inherit (package) version;
  src = lib.fileset.toSource {
    root = ./.;
    fileset = lib.fileset.unions [
      ./Cargo.toml
      ./Cargo.lock
      (lib.fileset.fileFilter (file: file.hasExt "rs") ./src)
      (lib.fileset.fileFilter (file: file.hasExt "rs" || file.hasExt "c") ./tests)
    ];
  };
  cargoLock.lockFile = ./Cargo.lock;
  nativeBuildInputs = [ pkgs.pkg-config ];
  buildInputs = [ pkgs.dbus ];
  nativeCheckInputs = [ pkgs.dbus ];
  meta = {
    description = "BlueZ D-Bus Bluetooth auth tools";
    license = lib.licenses.mit;
    platforms = lib.platforms.linux;
  };
}
