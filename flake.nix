{
  description = "Install, update and launch the public World of Warcraft clients without Battle.net";
  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      system = "x86_64-linux";
      pkgs = import nixpkgs { inherit system; };
      version = (builtins.fromTOML (builtins.readFile ./pyproject.toml)).project.version;
      bridge = pkgs.pkgsCross.mingwW64.stdenv.mkDerivation {
        pname = "waystone-bridge";
        inherit version;
        src = ./bridge;
        dontConfigure = true;
        dontFixup = true;
        buildPhase = ''
          $CC -Os -Wall -Wextra -Werror -mwindows -static auth_bridge.c device_id.c \
            -o wow_auth_bridge.exe -lcrypt32 -ladvapi32 -liphlpapi
        '';
        installPhase = ''
          install -Dm755 wow_auth_bridge.exe $out/libexec/wow_auth_bridge.exe
        '';
      };
      python = pkgs.python3;
      app = python.pkgs.buildPythonApplication {
        pname = "waystone";
        inherit version;
        pyproject = true;
        src = pkgs.lib.fileset.toSource {
          root = ./.;
          fileset = pkgs.lib.fileset.unions [ ./src ./tests ./pyproject.toml ];
        };
        build-system = [ python.pkgs.setuptools ];
        nativeBuildInputs = [ pkgs.copyDesktopItems ];
        dependencies = [ python.pkgs.pyside6 ];
        # The window sets its Wayland app id to "waystone", which matches this entry.
        desktopItems = [
          (pkgs.makeDesktopItem {
            name = "waystone";
            desktopName = "Waystone";
            comment = "Install, update and launch the public World of Warcraft clients";
            exec = "waystone";
            icon = "waystone";
            categories = [ "Game" ];
            startupWMClass = "waystone";
          })
        ];
        postInstall = ''
          install -Dm644 src/waystone/ui/icon.svg $out/share/icons/hicolor/scalable/apps/waystone.svg
        '';
        makeWrapperArgs = [
          "--set" "WAYSTONE_BRIDGE" "${bridge}/libexec/wow_auth_bridge.exe"
          # NixOS needs the Nix build of umu-launcher for its FHS container. nsenter closes a game in it.
          "--prefix" "PATH" ":" (pkgs.lib.makeBinPath [ pkgs.umu-launcher pkgs.util-linux ])
        ];
        nativeCheckInputs = [ python.pkgs.pytestCheckHook ];
        pythonImportsCheck = [ "waystone.library" "waystone.launch" "waystone.cli" ];
        meta.mainProgram = "waystone";
      };
      # A Linux package for distributions without Nix. See packaging/appimage.
      appimage = pkgs.callPackage ./packaging/appimage { inherit version bridge app; source = ./src/waystone; };
    in {
      packages.${system} = { default = app; inherit bridge appimage; };
      devShells.${system}.default = pkgs.mkShell {
        packages = [ (python.withPackages (p: [ p.pytest p.pyside6 ])) pkgs.umu-launcher ];
        WAYSTONE_BRIDGE = "${bridge}/libexec/wow_auth_bridge.exe";
      };
      checks.${system}.default = app;
    };
}
