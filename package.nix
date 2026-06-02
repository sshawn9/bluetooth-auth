{
  lib,
  pkgs,
  pyproject-nix,
  uv2nix,
  pyproject-build-systems,
}:

let
  inherit ((builtins.fromTOML (builtins.readFile ./pyproject.toml))) project;
  workspace = uv2nix.lib.workspace.loadWorkspace { workspaceRoot = ./.; };
  overlay = workspace.mkPyprojectOverlay { sourcePreference = "wheel"; };
  python = lib.head (
    pyproject-nix.lib.util.filterPythonInterpreters {
      inherit (workspace) requires-python;
      inherit (pkgs) pythonInterpreters;
    }
  );
  pythonBase = pkgs.callPackage pyproject-nix.build.packages {
    inherit python;
  };
  pythonSet = pythonBase.overrideScope (
    lib.composeManyExtensions [
      pyproject-build-systems.overlays.wheel
      overlay
      (_final: prev: {
        ${project.name} = prev.${project.name}.overrideAttrs (old: {
          src = lib.fileset.toSource {
            root = old.src;
            fileset = lib.fileset.unions [
              (old.src + "/pyproject.toml")
              (old.src + "/README.md")
              (old.src + "/src/bluetooth_auth")
            ];
          };
        });
      })
    ]
  );
  inherit (pkgs.callPackages pyproject-nix.build.util { }) mkApplication;
in
(mkApplication {
  venv = pythonSet.mkVirtualEnv "${project.name}-env" workspace.deps.default;
  package = pythonSet.${project.name};
}).overrideAttrs
  (old: {
    meta = (old.meta or { }) // {
      inherit (project) description;
      platforms = lib.platforms.linux;
    };
  })
