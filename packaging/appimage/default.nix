# The Linux AppImage for distributions without Nix.
#
# Nothing here comes from the Nix store at run time. Python is python-build-standalone,
# Qt comes from the PySide6 manylinux wheels, a few X11 helper libraries come from Debian 12,
# and umu-launcher is its own release zipapp. All of them are fetched by hash, so a commit
# always yields the same AppImage. The bridge is the Nix mingw build, which is a Windows
# program and needs nothing from the host.
{ lib, stdenvNoCC, fetchurl, binutils, dpkg, glibc, squashfsTools, unzip
, version, bridge, app, source }:

let
  python = fetchurl {
    url = "https://github.com/astral-sh/python-build-standalone/releases/download/20261026/cpython-3.14.10+20261026-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz";
    hash = "sha256-TTwWxZ7QgCo9HTTYvOUEsVzur/OdYtrezGqfqANBKu0=";
  };
  # PySide6 matches the version the Nix package and the Windows package use.
  # manylinux_2_34 sets the glibc baseline: Ubuntu 22.04, Fedora 35, Debian 12 and newer.
  wheels = [
    (fetchurl {
      url = "https://files.pythonhosted.org/packages/7b/3c/7597d8dcb36aa8315427635ec6ec9d850ffbad4ce78b9118ac854889f2d3/pyside6_essentials-6.12.0-cp310-abi3-manylinux_2_34_x86_64.whl";
      hash = "sha256-FvS15B2vSSNa4EhFgkPanv5fPYBA+cxapFTOaYk9O9k=";
    })
    (fetchurl {
      url = "https://files.pythonhosted.org/packages/49/c2/a04fa003b4360e99d8e3fc2829abacf37396ace63ea1cd616402209ec7c6/shiboken6-6.12.0-cp310-abi3-manylinux_2_34_x86_64.whl";
      hash = "sha256-O95WW7CJAES1x/Y4CK3d7eWrwkHpOVor/tYZGYhNYkA=";
    })
  ];
  # The version the Nix package wraps. The zipapp carries its pure Python dependencies.
  umu = fetchurl {
    url = "https://github.com/Open-Wine-Components/umu-launcher/releases/download/1.4.4/umu-launcher-1.4.4-zipapp.tar";
    hash = "sha256-61kGkYQff60/w62P1dtMy4eEn+eUjmKyjs56TuSMyFE=";
  };
  # Qt's X11 plugin needs these, and many desktops install them only with other Qt programs.
  # They use only libxcb's public interface. The rest of what Qt needs, such as libxcb,
  # libxkbcommon, Wayland, fontconfig and OpenGL, comes with every desktop. libxkbcommon-x11
  # is not bundled: it must come from the same release as the host's libxkbcommon, or the
  # X11 plugin crashes. The snapshot URLs are permanent.
  debian = name: path: sha1: hash: fetchurl {
    urls = [ "https://deb.debian.org/debian/pool/main/${path}" "https://snapshot.debian.org/file/${sha1}" ];
    name = "${name}.deb";
    inherit hash;
  };
  x11Libraries = [
    (debian "libxcb-cursor0" "x/xcb-util-cursor/libxcb-cursor0_0.1.4-1_amd64.deb"
      "65e6e05bd9d8485ea4203a4723d19e9ee551f277" "sha256-pLPDLcAIJ1/8rMzBx3wDDwGq044jLgXVrRFrdmVsYHw=")
    (debian "libxcb-icccm4" "x/xcb-util-wm/libxcb-icccm4_0.4.1-1.1_amd64.deb"
      "e560263e55e6335671a0c902bb45b4f042130cd0" "sha256-8yMZTLBM1OWuBk+v7Dnbbc+KQxy9ZaC8U/psNZhi2P8=")
    (debian "libxcb-image0" "x/xcb-util-image/libxcb-image0_0.4.0-2_amd64.deb"
      "75bd5031657e0befcff822ae2f52cf1ae09ee1f4" "sha256-pHVSL673ZyygZf3NJZS8dVv8xNgZkJ+dlE6MACtEYNE=")
    (debian "libxcb-keysyms1" "x/xcb-util-keysyms/libxcb-keysyms1_0.4.0-1+b2_amd64.deb"
      "f7bd77164c1f5336d3591c4e20c202b3f7dce086" "sha256-rtFDbbmj5jsQ0AxO0WJItcgrXdKWOoOnYfQGr2XrS0k=")
    (debian "libxcb-render-util0" "x/xcb-util-renderutil/libxcb-render-util0_0.3.9-1+b1_amd64.deb"
      "3f5015eda3339c1b90c5359c47eb80476286bbd8" "sha256-vks4pj5lyE4vEyLwRNBam6pnfg89xot0KgoQmj/0Cuk=")
    (debian "libxcb-util1" "x/xcb-util/libxcb-util1_0.4.0-1+b1_amd64.deb"
      "4049f832a1452632f6d3694fe1fbc2d10a7d15cb" "sha256-TEivUfsqwb4EkAZ+dFCu2ie/bGw5UWXeAhme7kg1M28=")
  ];
  # The AppImage runtime is a static program, so the host needs FUSE but not libfuse2.
  runtime = fetchurl {
    url = "https://github.com/AppImage/type2-runtime/releases/download/20251108/runtime-x86_64";
    hash = "sha256-L8qLRDySUQ8Ug6iD9gBhrQm0a5eLJjHIB82HOkfsJg0=";
  };
  # Qt libraries that QtCore, QtGui, QtWidgets and the kept plugins load. The build fails
  # if a kept library needs one that is not here.
  qtLibraries = [ "Core" "DBus" "Gui" "OpenGL" "Svg" "WaylandClient" "Widgets" "WlShellIntegration" "XcbQpa" ];
  # Plugins Waystone uses: X11, Wayland and the offscreen platform, desktop themes and
  # input methods, SVG icons and images.
  qtPlugins = [ "iconengines" "imageformats" "platforminputcontexts" "platforms" "platformthemes"
                "wayland-decoration-client" "wayland-graphics-integration-client"
                "wayland-shell-integration" "xcbglintegrations" ];
in
stdenvNoCC.mkDerivation {
  pname = "waystone-appimage";
  inherit version;
  dontUnpack = true;
  dontFixup = true;
  nativeBuildInputs = [ binutils dpkg squashfsTools unzip ];

  buildPhase = ''
    runHook preBuild
    appdir=$PWD/AppDir
    prefix=$appdir/usr/python
    stdlib=$prefix/lib/python3.14
    site=$stdlib/site-packages
    mkdir -p $prefix
    tar -xzf ${python} -C $prefix --strip-components=1

    # Only what Waystone and umu-run import, without the build and GUI tools of Python.
    rm -r $prefix/include $prefix/share $prefix/lib/pkgconfig $prefix/lib/libpython3*.so*
    rm -r $prefix/lib/itcl* $prefix/lib/tcl* $prefix/lib/tk* $prefix/lib/thread* $prefix/lib/libtcl*
    rm -r $stdlib/{ensurepip,idlelib,tkinter,turtledemo,venv,config-3.14-x86_64-linux-gnu} $stdlib/lib-dynload/_tkinter*
    rm -r $site/pip* $prefix/bin/{idle3*,pip*,pydoc3*,python3*-config}

    for wheel in ${lib.concatStringsSep " " wheels}; do
      unzip -q -o $wheel -d $site
    done
    pyside=$site/PySide6
    find $pyside $site/shiboken6 -maxdepth 1 \( -name '*.pyi' -o -name include \) -exec rm -r {} +
    find $pyside -maxdepth 1 -name 'Qt*.abi3.so' \
      ! -name QtCore.abi3.so ! -name QtGui.abi3.so ! -name QtWidgets.abi3.so -delete
    find $pyside -maxdepth 1 -type f -executable ! -name '*.so*' -delete       # designer, qmlls and the like
    rm -r $pyside/{doc,glue,scripts,typesystems,libpyside6qml.abi3.so*} $pyside/Qt/{libexec,metatypes,qml,translations}
    find $pyside/Qt/lib -name 'libQt6*' ${lib.concatMapStrings (name: "! -name 'libQt6${name}.so.6' ") qtLibraries} -delete
    find $pyside/Qt/plugins -mindepth 1 -maxdepth 1 ${lib.concatMapStrings (name: "! -name '${name}' ") qtPlugins} -exec rm -r {} +
    # Plugins for screens without a window system, and those whose libraries the wheel lacks.
    rm $pyside/Qt/plugins/platforms/lib{qeglfs,qlinuxfb,qminimalegl,qvkkhrdisplay,qvnc}.so
    rm $pyside/Qt/plugins/platforminputcontexts/libqtvirtualkeyboardplugin.so $pyside/Qt/plugins/imageformats/libqpdf.so

    # Next to Qt's own libraries, where the X11 plugin's RPATH finds them.
    for deb in ${lib.concatStringsSep " " x11Libraries}; do
      dpkg-deb -x $deb debian
    done
    cp -P debian/usr/lib/x86_64-linux-gnu/*.so.* $pyside/Qt/lib/

    # Every library Qt or PySide loads from the bundle must be in it.
    missing=0
    while IFS= read -r -d "" library; do
      for needed in $(readelf -d "$library" | sed -nE 's/.*NEEDED.*\[(lib(Qt6|icu|pyside6|shiboken6|xcb-(cursor|icccm|image|keysyms|render-util|util))[^]]*)\]/\1/p'); do
        if [ ! -e $pyside/Qt/lib/$needed ] && [ ! -e $pyside/$needed ] && [ ! -e $site/shiboken6/$needed ]; then
          echo "$library needs $needed, which is not bundled" >&2
          missing=1
        fi
      done
    done < <(find $site -name '*.so*' -type f -print0)
    [ $missing = 0 ]

    cp -r ${source} $site/waystone
    chmod -R u+w $site/waystone
    # The package metadata, so the version Waystone reports, as in diagnostics, is this build's.
    mkdir $site/waystone-${version}.dist-info
    printf 'Metadata-Version: 2.1\nName: waystone\nVersion: ${version}\n' > $site/waystone-${version}.dist-info/METADATA

    mkdir -p $appdir/usr/lib/umu
    tar -xf ${umu} -C $appdir/usr/lib/umu --strip-components=1 umu/umu-run
    install -Dm644 ${bridge}/libexec/wow_auth_bridge.exe $appdir/usr/libexec/waystone/wow_auth_bridge.exe
    install -Dm755 ${./AppRun} $appdir/AppRun
    install -Dm755 ${./umu-run} $appdir/usr/bin/umu-run

    # The same desktop entry and icon as the Nix package. AppImage tools read them at the top.
    install -Dm644 ${app}/share/applications/waystone.desktop $appdir/usr/share/applications/waystone.desktop
    install -Dm644 ${app}/share/icons/hicolor/scalable/apps/waystone.svg $appdir/usr/share/icons/hicolor/scalable/apps/waystone.svg
    ln -s usr/share/applications/waystone.desktop $appdir/waystone.desktop
    ln -s usr/share/icons/hicolor/scalable/apps/waystone.svg $appdir/waystone.svg
    ln -s waystone.svg $appdir/.DirIcon

    # The mount is read-only, so bytecode is compiled now. Unchecked hashes keep the files
    # independent of timestamps. The bundled Python runs through the build's loader.
    ${glibc}/lib/ld-linux-x86-64.so.2 --library-path ${glibc}/lib $prefix/bin/python3.14 -I -m compileall \
      -q --invalidation-mode unchecked-hash -s $appdir -p / $stdlib

    # mksquashfs gives every file and the image the time in SOURCE_DATE_EPOCH, which Nix sets.
    mksquashfs $appdir waystone.squashfs -quiet -no-progress -noappend -all-root -no-xattrs \
      -comp zstd -Xcompression-level 19 -b 1M
    runHook postBuild
  '';

  installPhase = ''
    runHook preInstall
    mkdir -p $out
    cat ${runtime} waystone.squashfs > $out/Waystone-${version}-x86_64.AppImage
    chmod 755 $out/Waystone-${version}-x86_64.AppImage
    runHook postInstall
  '';

  meta = {
    description = "Waystone as an AppImage for Linux distributions without Nix";
    platforms = [ "x86_64-linux" ];
  };
}
