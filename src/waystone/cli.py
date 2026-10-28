"""Command-line access to the same operations as the interface."""
from __future__ import annotations

import argparse
import getpass
import json
import os
from pathlib import Path
import sys

from . import catalog
from .library import Library, LibraryError, Updater
from .ngdp.cdn import CDNClient
from .settings import Settings, cache_dir


def terminal_sign_in(url: str) -> bytes:
    from .launch import open_browser, parse_ticket
    print(f'Sign in to Battle.net in your browser:\n\n  {url}\n\n'
          'Afterwards the browser cannot open http://localhost:0/?ST=... '
          'Copy that whole address and paste it here.', file=sys.stderr, flush=True)
    open_browser(url)
    text = getpass.getpass('Address (hidden): ') if sys.stdin.isatty() else sys.stdin.readline(4096)
    return parse_ticket(text)


def progress(event: dict) -> None:
    print(json.dumps(event), file=sys.stderr, flush=True)


def updater(library: Library, code: str, args) -> Updater:
    state = library.state(code) or {}
    region = args.region or state.get('region', 'eu')
    locale = args.locale or state.get('locale', 'enUS')
    client = CDNClient(cache_dir(), region, code, workers=args.workers)
    return Updater(library, code, client, region=region, locale=locale,
                   seed_from=getattr(args, 'seed_from', None), progress=progress)


def main(argv=None):
    parser = argparse.ArgumentParser(prog='waystone', description=__doc__)
    parser.add_argument('--root', type=Path, help='Installation folder, by default the saved one')
    commands = parser.add_subparsers(dest='command')
    commands.add_parser('clients', help='List the clients and their installed versions')
    commands.add_parser('sign-in', help='Sign in to Battle.net through the browser')
    commands.add_parser('sign-out', help='Forget the saved Battle.net session')
    play = commands.add_parser('play', help='Renew the session and start a client')
    play.add_argument('client', choices=sorted(catalog.BY_CODE))
    play.add_argument('--mode', help='Game mode, such as hardcore for Classic Era')
    play.add_argument('--wait', action='store_true', help='Wait until the game exits')
    adopt = commands.add_parser('adopt-wowupdater', help='Adopt a wowupdater installation in place')
    adopt.add_argument('source', type=Path)
    for name in ['install', 'update', 'verify']:
        command = commands.add_parser(name)
        command.add_argument('client', choices=sorted(catalog.BY_CODE))
        command.add_argument('--region')
        command.add_argument('--locale')
        command.add_argument('--workers', type=int, default=16)
        if name == 'install':
            command.add_argument('--seed-from', type=Path, help='Read-only installation to copy verified content from')
        if name == 'verify':
            command.add_argument('--quick', action='store_true', help='Skip reading every stored object')
    args = parser.parse_args(argv)
    if args.command is None:
        from .ui.app import main as interface
        return interface()
    settings = Settings.load()
    root = args.root or (Path(settings.root) if settings.root else None)
    if root is None:
        raise LibraryError('Choose an installation folder with --root')
    library = Library(root)
    if args.command in ('sign-in', 'sign-out', 'play'):
        from .launch import Launcher
        launcher = Launcher(settings, library)
        if args.command == 'sign-out':
            launcher.vault.forget()
        elif args.command == 'sign-in':
            states = [library.state(code) for code in library.installed()]
            if not states:
                raise LibraryError('Install a client before signing in')
            launcher.sign_in(int(states[0]['version'].rsplit('.', 1)[-1]), terminal_sign_in)
        else:
            process = launcher.play(args.client, terminal_sign_in, mode=args.mode)
            print(json.dumps({'started': args.client, 'pid': process.pid}), flush=True)
            if args.wait:
                process.wait()
        return

    if args.command == 'adopt-wowupdater':
        source = args.source.resolve()
        if source != library.root:
            if library.root.exists():
                raise LibraryError(f'{library.root} already exists')
            os.rename(source, library.root)
        Library.migrate_wowupdater(library.root)
        print(json.dumps({'adopted': str(library.root), 'clients': Library(library.root).installed()}))
        return
    if args.command == 'clients':
        rows = []
        for client in catalog.CLIENTS:
            state = library.state(client.code) if library.exists() else None
            rows.append({'client': client.code, 'label': client.label, 'test': client.test,
                         'installed': state['version'] if state else None,
                         'folder': state['folder'] if state else None})
        print(json.dumps(rows, indent=2))
        return
    if args.command == 'install':
        library.create()
    elif library.state(args.client) is None:
        raise LibraryError(f'{catalog.get(args.client).label} is not installed')
    with library.locked():
        job = updater(library, args.client, args)
        try:
            if args.command == 'verify':
                result = job.verify(deep=not args.quick)
            else:
                result = job.run()
                result = {k: result[k] for k in ['product', 'version', 'folder', 'objects', 'encoded_bytes',
                                                 'seeded_objects', 'downloaded_objects']}
        finally:
            job.close()
    print(json.dumps(result, indent=2))
    if args.command == 'verify' and not result['ok']:
        raise SystemExit(2)


def entrypoint():
    try:
        main()
    except (LibraryError, OSError, ValueError) as error:
        raise SystemExit(str(error))


if __name__ == '__main__':
    entrypoint()
