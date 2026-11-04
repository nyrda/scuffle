{
  description = "Install, update and launch the public World of Warcraft clients without Battle.net";
  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      system = "x86_64-linux";
      pkgs = import nixpkgs { inherit system; };
      version = (builtins.fromTOML (builtins.readFile ./pyproject.toml)).project.version;
      bridge = pkgs.pkgsCross.mingwW64.stdenv.mkDerivation {
        pname = "scuffle-bridge";
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
        pname = "scuffle";
        inherit version;
        pyproject = true;
        src = pkgs.lib.fileset.toSource {
          root = ./.;
          fileset = pkgs.lib.fileset.unions [ ./src ./tests ./pyproject.toml ];
        };
        build-system = [ python.pkgs.setuptools ];
        nativeBuildInputs = [ pkgs.copyDesktopItems ];
        # jeepney sends desktop notifications over D-Bus. PySide6 cannot marshal the uint32 they need.
        dependencies = [ python.pkgs.pyside6 python.pkgs.jeepney ];
        # The window sets its Wayland app id to "scuffle", which matches this entry.
        desktopItems = [
          (pkgs.makeDesktopItem {
            name = "scuffle";
            desktopName = "Scuffle";
            comment = "Install, update and launch the public World of Warcraft clients";
            exec = "scuffle";
            icon = "scuffle";
            categories = [ "Game" ];
            startupWMClass = "scuffle";
          })
        ];
        postInstall = ''
          install -Dm644 src/scuffle/ui/icon.svg $out/share/icons/hicolor/scalable/apps/scuffle.svg
          # Launchers and panels that ask for a small icon get the version drawn for it.
          for size in 16 22 24; do
            install -Dm644 src/scuffle/ui/icon-small.svg $out/share/icons/hicolor/''${size}x''${size}/apps/scuffle.svg
          done
        '';
        makeWrapperArgs = [
          "--set" "SCUFFLE_BRIDGE" "${bridge}/libexec/wow_auth_bridge.exe"
          # NixOS needs the Nix build of umu-launcher for its FHS container. nsenter closes a game in it.
          "--prefix" "PATH" ":" (pkgs.lib.makeBinPath [ pkgs.umu-launcher pkgs.util-linux ])
        ];
        # A private dbus-daemon carries the notification tests.
        nativeCheckInputs = [ python.pkgs.pytestCheckHook pkgs.dbus ];
        pythonImportsCheck = [ "scuffle.library" "scuffle.launch" "scuffle.cli" ];
        meta = {
          mainProgram = "scuffle";
          license = pkgs.lib.licenses.mit;
        };
      };
      # A Linux package for distributions without Nix. See packaging/appimage.
      appimage = pkgs.callPackage ./packaging/appimage { inherit version bridge app; source = ./src/scuffle; };
    in {
      packages.${system} = { default = app; inherit bridge appimage; };
      devShells.${system}.default = pkgs.mkShell {
        packages = [ (python.withPackages (p: [ p.pytest p.pyside6 p.jeepney ])) pkgs.umu-launcher pkgs.dbus ];
        SCUFFLE_BRIDGE = "${bridge}/libexec/wow_auth_bridge.exe";
      };
      checks.${system}.default = app;
    };
}
