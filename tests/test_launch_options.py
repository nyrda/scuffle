"""Launch options: what is saved, what is rejected, and the command and environment the game gets."""
import json
import os
from pathlib import Path
import stat
import sys

import pytest

from waystone import launch, launch_options, settings as settings_module
from waystone.auth.vault import Vault
from waystone.launch_options import ClientOptions, LinuxOptions, OptionError, parse_environment, split_arguments
from waystone.runtime.game import LinuxRuntime
from waystone.runtime.proton import Proton
from waystone.settings import Settings


@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.setattr(settings_module, 'config_dir', lambda: tmp_path)
    return tmp_path / 'settings.json'


def test_settings_from_before_launch_options_load_with_the_defaults(config):
    config.write_text(json.dumps({'root': '/games', 'proton': '/opt/GE-Proton10-34', 'modes': {'wow_classic_era': 'sod'}}))
    loaded = Settings.load()
    assert loaded.proton == '/opt/GE-Proton10-34' and loaded.modes == {'wow_classic_era': 'sod'}
    assert loaded.linux_launch == LinuxOptions() and loaded.client_launch == {} and not loaded.problems


def test_launch_options_survive_a_save_and_load(config):
    saved = Settings(root='/games')
    saved.linux_launch = LinuxOptions(gamemode=True, mangohud=True, ntsync=False, log=True,
                                      environment={'DXVK_HUD': 'fps', 'QUOTED': '"kept"'},
                                      wrapper='gamescope -w 2560 -h 1440 -f --')
    saved.client_launch = {'wow': ClientOptions(graphics='-d3d11', window='-windowed', switches=['-console'],
                                                arguments='-config "WTF\\Config 2.wtf"',
                                                environment={'VKD3D_CONFIG': 'dxr'})}
    saved.save()
    loaded = Settings.load()
    assert loaded.linux_launch == saved.linux_launch
    assert loaded.client_launch == saved.client_launch
    assert not loaded.problems


def test_invalid_launch_options_are_dropped_and_reported_instead_of_failing_the_start(config):
    config.write_text(json.dumps({
        'linux_launch': {'gamemode': 'yes', 'mangohud': True, 'environment': {'BAD NAME': '1', 'GOOD': '2'},
                         'wrapper': 'gamescope "unclosed'},
        'client_launch': {'wow': {'graphics': '-vulkan', 'switches': ['-console', '-made-up'],
                                  'arguments': '-console -uid wow', 'environment': {'WINEPREFIX': '/elsewhere'}},
                          'wow_classic': {'graphics': ['-d3d11'], 'window': {'mode': 1}, 'switches': [{}]},
                          'wow_not_a_client': {}, 'wowf': 'not a dict'},
    }))
    loaded = Settings.load()
    assert loaded.linux_launch == LinuxOptions(mangohud=True, environment={'GOOD': '2'})
    # What was valid stays. The rest goes, and each removal is reported.
    assert loaded.client_launch == {'wow': ClientOptions(switches=['-console']), 'wow_classic': ClientOptions()}
    text = ' '.join(loaded.problems)
    for part in ['gamemode', 'Linux settings', 'wrapper', 'graphics', 'switches', '-uid',
                 'World of Warcraft', 'wow_not_a_client', 'Forever', 'window option of Mists']:
        assert part in text


@pytest.mark.parametrize('text, variables, error', [
    ('DXVK_HUD=fps\n\n# a comment\nexport MANGOHUD_CONFIG = "fps_only"', {'DXVK_HUD': 'fps', 'MANGOHUD_CONFIG': 'fps_only'}, ''),
    ('EMPTY=', {'EMPTY': ''}, ''),
    ('A=b=c', {'A': 'b=c'}, ''),
    ('DXVK HUD=fps', {}, 'Line 1: DXVK HUD is not a valid name'),
    ('1ST=x', {}, 'not a valid name'),
    ('=x', {}, 'the name is not a valid name'),
    ('JUSTANAME', {}, 'Line 1: write it as NAME=value'),
    ('OK=1\nWINEPREFIX=/tmp/x', {'OK': '1'}, 'Line 2: Waystone sets WINEPREFIX itself'),
    ('A=1\nA=2', {'A': '1'}, 'Line 2: A is already set on line 1'),
])
def test_environment_lines_are_checked_one_by_one(text, variables, error):
    parsed, errors = parse_environment(text)
    assert parsed == variables
    assert (error in errors[0]) if error else not errors


def test_saved_values_keep_their_quotes_and_spaces_through_the_editor():
    variables = {'QUOTED': '"kept"', 'SINGLE': "'kept'", 'EMPTY': '""', 'SPACED': ' x ', 'PLAIN': 'a "b" c',
                 'NOTHING': ''}
    assert parse_environment(launch_options.format_environment(variables)) == (variables, [])


@pytest.mark.parametrize('nvidia, nvapi, expected', [
    (True, True, {}), (True, False, {'PROTON_DISABLE_NVAPI': '1'}), (False, False, {})])
def test_nvapi_is_left_to_proton_unless_turned_off_on_an_nvidia_gpu(nvidia, nvapi, expected, tmp_path):
    options = LinuxOptions(nvapi=nvapi)
    assert launch_options.linux_environment(options, nvidia=nvidia, log_dir=tmp_path) == expected


@pytest.mark.parametrize('text', ['-uid wow_beta', '-LauncherLogin', '-initialgamemode=chc', '--uid=x',
                                  '-console -initialgamemode cera'])
def test_arguments_waystone_sets_itself_are_rejected(text):
    with pytest.raises(OptionError, match='Waystone sets'):
        split_arguments(text)


def test_other_arguments_split_like_a_shell_and_keep_windows_paths():
    assert split_arguments('-config "WTF\\Config 2.wtf" -uidless') == ['-config', 'WTF\\Config 2.wtf', '-uidless']
    with pytest.raises(OptionError, match='quote'):
        split_arguments('-config "WTF')


def test_toggles_come_first_and_the_extra_words_follow_as_typed():
    options = ClientOptions(graphics='-d3d12', window='-fullscreen', switches=['-nosound', '-console'],
                            arguments='-config Other.wtf')
    assert options.game_arguments() == ['-d3d12', '-fullscreen', '-console', '-nosound', '-config', 'Other.wtf']
    # A word that looks like a toggle may be another argument's value, so it stays where it was typed.
    options = ClientOptions(switches=['-console'], arguments='-config -console')
    assert options.game_arguments() == ['-console', '-config', '-console']


def test_extra_arguments_that_repeat_or_contradict_a_toggle_are_named():
    options = ClientOptions(graphics='-d3d12', window='-windowed', switches=['-console'],
                            arguments='-D3D11 -console -windowed -config Other.wtf')
    assert options.overlaps() == ['-d3d11 contradicts DirectX 12 under Graphics.',
                                  '-console is already on under Options.',
                                  '-windowed is already chosen under Window.']
    assert ClientOptions(arguments='-d3d11 -nosound -fullscreen').overlaps() == []


def executable(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


@pytest.fixture
def programs(tmp_path, monkeypatch):
    """A PATH holding only the given fake programs, plus the interpreter the fakes use."""
    folder = tmp_path / 'bin'
    folder.mkdir()
    monkeypatch.setenv('PATH', f'{folder}{os.pathsep}{Path(sys.executable).parent}')
    return folder


@pytest.mark.parametrize('text, message', [
    ('gamescope -f "unclosed', 'quote'),
    ('gamescope -f -- %command%', '%command%'),
    ('MANGOHUD=1 gamescope -f --', 'Environment'),
    ('gamescope -w 2560 -h 1440 -f', 'needs --'),
    ('not-installed --flag', 'not-installed was not found'),
])
def test_a_wrapper_command_that_cannot_work_is_named(programs, text, message):
    executable(programs / 'gamescope', '#!/bin/sh\n')
    with pytest.raises(OptionError, match=message):
        launch_options.wrapper_command(LinuxOptions(wrapper=text))


def test_wrapper_programs_are_found_on_path_and_gamemode_runs_closest_to_umu(programs):
    gamescope = executable(programs / 'gamescope', '#!/bin/sh\n')
    gamemoderun = executable(programs / 'gamemoderun', '#!/bin/sh\n')
    command = launch_options.wrapper_command(LinuxOptions(gamemode=True, wrapper="gamescope -f --"))
    assert command == [str(gamescope), '-f', '--', str(gamemoderun)]
    # Without gamemoderun the settings show GameMode as off, so the game starts without it.
    gamemoderun.unlink()
    assert launch_options.wrapper_command(LinuxOptions(gamemode=True)) == []


def test_the_windows_runtime_starts_the_game_without_wrappers_or_variables(tmp_path, monkeypatch):
    from waystone.runtime import game
    started = {}
    monkeypatch.setattr(game.subprocess, 'Popen', lambda command, env, **kwargs: started.update(
        command=command, env=env))
    runtime = game.WindowsRuntime(tmp_path / 'bridge.exe')
    runtime.start(tmp_path / 'Wow.exe', ['-console'], tmp_path / 'token', tmp_path / 'ack',
                  environment={'DXVK_HUD': 'fps'}, wrapper=['gamemoderun'])
    assert started['command'][0] == str(tmp_path / 'bridge.exe') and started['command'][-1] == '-console'
    assert 'gamemoderun' not in started['command'] and 'DXVK_HUD' not in started['env']


def test_windows_gets_the_arguments_only(programs):
    settings = Settings()
    settings.linux_launch = LinuxOptions(gamemode=True, mangohud=True, wrapper='not-installed --')
    settings.client_launch['wow'] = ClientOptions(switches=['-console'], environment={'DXVK_HUD': 'fps'})
    plan = launch.launch_plan(settings, 'wow', windows=True)
    assert (plan.arguments, plan.environment, plan.wrapper) == (['-console'], {}, [])


# A fake umu-run and fake wrappers that record how they were called, then act like the real
# ones: createprefix makes system.reg, a game start acknowledges through the ack file.
FAKE_UMU = r'''#!PYTHON
import json, os, sys
from pathlib import Path
log = Path(os.environ['FAKE_LOG'])
with log.open('a') as handle:
    handle.write(json.dumps({'argv': sys.argv, 'env': dict(os.environ)}) + '\n')
if sys.argv[1:] == ['createprefix']:
    (Path(os.environ['WINEPREFIX']) / 'system.reg').write_text('')
else:
    ack = sys.argv[sys.argv.index('--launch-protected-file') + 2]
    Path(ack[2:].replace('\\', '/')).write_text(json.dumps({'game_started': True}))
'''
FAKE_WRAPPER = r'''#!PYTHON
import json, os, sys
from pathlib import Path
with Path(os.environ['FAKE_LOG']).open('a') as handle:
    handle.write(json.dumps({'argv': sys.argv, 'env': {}}) + '\n')
rest = sys.argv[sys.argv.index('--') + 1:] if '--' in sys.argv else sys.argv[1:]
os.execv(rest[0], rest)
'''


class Library:
    def __init__(self, root: Path):
        self.folder = root / '_classic_era_'
        self.folder.mkdir(parents=True)

    def state(self, code):
        return {'version': '1.15.8.66710', 'launch_arguments': ['-fromproductconfig']}

    def pending(self, code):
        return None

    def running(self, code):
        return False

    def executable(self, code):
        return self.folder / 'Wow.exe'


def test_the_game_runs_inside_the_wrappers_with_the_options_and_the_prefix_without_them(
        tmp_path, programs, monkeypatch):
    log = tmp_path / 'calls.jsonl'
    monkeypatch.setenv('FAKE_LOG', str(log))
    monkeypatch.setenv('UMU_CONTAINER_NSENTER', '1')
    python = '#!' + sys.executable
    umu = executable(programs / 'umu-run', FAKE_UMU.replace('#!PYTHON', python))
    executable(programs / 'fakewrap', FAKE_WRAPPER.replace('#!PYTHON', python))
    executable(programs / 'gamemoderun', FAKE_WRAPPER.replace('#!PYTHON', python))
    proton = tmp_path / 'GE-Proton'
    executable(proton / 'files/bin/wine', '#!/bin/sh\n')
    monkeypatch.setattr('waystone.runtime.game.wine_wrapper', lambda: [])
    monkeypatch.setattr(launch, 'has_nvidia', lambda: False)
    monkeypatch.setattr(launch, 'renew', lambda *args: None)           # no Battle.net in a test

    settings = Settings()
    settings.linux_launch = LinuxOptions(gamemode=True, mangohud=True, log=True, wrapper='fakewrap --opt --',
                                         environment={'DXVK_HUD': 'fps', 'SHARED': 'global',
                                                      'WINEDEBUG': '+loaddll'})
    settings.client_launch['wow_classic_era'] = ClientOptions(
        graphics='-d3d11', switches=['-console'], arguments='-config Era.wtf', environment={'SHARED': 'client'})
    vault = Vault(tmp_path / 'session', bridge=None)
    vault.directory.mkdir()
    vault.session.write_bytes(b'protected')
    runtime = LinuxRuntime(tmp_path / 'bridge.exe', tmp_path / 'prefix', Proton('GE-Proton', proton), umu)
    library = Library(tmp_path / 'games')
    process = launch.Launcher(settings, library, runtime=runtime, vault=vault).play(
        'wow_classic_era', sign_in=None, mode='hardcore')
    process.wait()

    calls = [json.loads(line) for line in log.read_text().splitlines()]
    prefix, outer, inner, game = calls
    # Prefix creation runs umu-run alone, with none of the user's variables.
    assert prefix['argv'] == [str(umu), 'createprefix']
    assert 'MANGOHUD' not in prefix['env'] and 'DXVK_HUD' not in prefix['env']
    # The game: the user's wrapper, then gamemoderun, then umu-run with the bridge and Wow.exe.
    assert [Path(outer['argv'][0]).name, *outer['argv'][1:3]] == ['fakewrap', '--opt', '--']
    assert Path(outer['argv'][3]).name == 'gamemoderun' and Path(inner['argv'][0]).name == 'gamemoderun'
    assert game['argv'][0] == str(umu) and game['argv'][1:3] == [str(tmp_path / 'bridge.exe'),
                                                                  '--launch-protected-file']
    assert game['argv'][5].endswith('\\_classic_era_\\Wow.exe')
    assert game['argv'][6:] == ['-launcherlogin', '-uid', 'wow_classic_era', '-fromproductconfig',
                                '-d3d11', '-console', '-config', 'Era.wtf', '-initialgamemode=chc']
    env = game['env']
    assert env['MANGOHUD'] == '1' and env['DXVK_HUD'] == 'fps' and env['SHARED'] == 'client'
    assert env['PROTON_LOG'] == '1' and env['PROTON_LOG_DIR'] == str(launch.log_dir())
    assert env['WINEDEBUG'] == '+loaddll'           # the user's choice beats Waystone's quiet default
    assert env['WINEPREFIX'] == str(tmp_path / 'prefix') and env['WOW_BROKER_PIPE'] == '1'
    assert 'UMU_CONTAINER_NSENTER' not in env


def test_a_proton_log_without_a_chosen_wine_debug_leaves_the_channels_to_proton(tmp_path):
    runtime = LinuxRuntime.__new__(LinuxRuntime)
    runtime.prefix, runtime.proton = tmp_path / 'prefix', Proton('GE', tmp_path)
    assert runtime.game_environment({})['WINEDEBUG'] == '-all'
    env = runtime.game_environment({'PROTON_LOG': '1', 'WINEPREFIX': '/elsewhere'})
    assert 'WINEDEBUG' not in env and env['WINEPREFIX'] == str(tmp_path / 'prefix')


def test_a_missing_wrapper_stops_the_launch_before_the_sign_in_is_used(tmp_path, programs, monkeypatch):
    renewed = []
    monkeypatch.setattr(launch, 'renew', lambda *args: renewed.append(args))
    settings = Settings()
    settings.linux_launch = LinuxOptions(wrapper='not-installed --')

    class Runtime:
        bridge = None

        def prepare(self):
            renewed.append('prepare')

    launcher = launch.Launcher(settings, Library(tmp_path / 'games'), runtime=Runtime(),
                               vault=Vault(tmp_path / 'session', bridge=None))
    with pytest.raises(launch.LaunchError, match='not-installed was not found'):
        launcher.play('wow_classic_era', sign_in=None)
    assert renewed == []


@pytest.mark.parametrize('options, hint', [(ClientOptions(), False), (ClientOptions(switches=['-console']), True)])
def test_a_game_that_does_not_start_points_at_the_launch_options_when_there_are_any(
        tmp_path, monkeypatch, options, hint):
    from types import SimpleNamespace
    monkeypatch.setattr(launch, 'renew', lambda *args: None)
    monkeypatch.setattr(launch, 'has_nvidia', lambda: False)

    class Runtime:
        bridge = None

        def prepare(self):
            pass

        def start(self, *args, **kwargs):
            return SimpleNamespace(poll=lambda: 1)        # a wrapper or the game that quit at once

    settings = Settings()
    settings.client_launch['wow_classic_era'] = options
    vault = Vault(tmp_path / 'session', bridge=None)
    vault.directory.mkdir()
    vault.session.write_bytes(b'protected')
    launcher = launch.Launcher(settings, Library(tmp_path / 'games'), runtime=Runtime(), vault=vault)
    with pytest.raises(launch.LaunchError, match='did not start') as raised:
        launcher.play('wow_classic_era', sign_in=None)
    assert ('launch options may be the cause' in str(raised.value)) == hint
