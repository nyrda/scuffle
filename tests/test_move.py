"""Moving the installation folder, with real folders, a real store and real crashes.

A second file system is simulated where it matters: the rename of the installation
folder fails with EXDEV, as it does between drives, so the move copies instead.
"""
import errno
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading

import pytest

from test_library import FixtureCDN, SyntheticBuild, run_build

from scuffle import move
from scuffle.library import Library, LibraryError, Updater
from scuffle.ngdp.store import StoreError
from scuffle.settings import Settings

SRC = Path(__file__).resolve().parents[1] / 'src'


@pytest.fixture
def installed(tmp_path):
    """Two installed clients that share content, with the setting pointing at them."""
    root = tmp_path / 'drive-a' / 'Scuffle'
    forever = SyntheticBuild()
    retail = SyntheticBuild('0.0.2.2', changed=b'retail only', product='wow', folder='_retail_')
    run_build(root, FixtureCDN(forever, tmp_path / 'forever-cache'))
    run_build(root, FixtureCDN(retail, tmp_path / 'retail-cache'), product='wow')
    (root / '_retail_/WTF/account.wtf').write_bytes(b'personal settings')
    settings = Settings(root=str(root))
    settings.save()
    return root, settings, {'wowf': forever, 'wow': retail}


def tree(root: Path) -> dict:
    """Every file's bytes, without the locks a library recreates."""
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob('*'))
            if p.is_file() and p.name not in {'library.lock', 'store.lock'} and not p.name.endswith('-shm')}


def complete(root: Path, builds: dict, scratch: Path) -> bool:
    """Whether root is a whole installation: both clients verify. Checked on a copy, so a
    partial folder is never written to by the check itself."""
    if not (root / '.scuffle/identity.json').is_file():
        return False
    probe = scratch / f'probe-{len(list(scratch.glob("probe-*")))}'
    shutil.copytree(root, probe)
    (probe / move.MARKER).unlink(missing_ok=True)
    library = Library(probe)
    if library.installed() != sorted(builds):
        return False
    for code, build in builds.items():
        try:
            # A store cut short, such as one half deleted, does not even open.
            updater = Updater(library, code, FixtureCDN(build, scratch / 'verify-cache'))
        except (StoreError, OSError, LibraryError):
            return False
        try:
            if not updater.verify()['ok']:
                return False
        finally:
            updater.close()
    return True


def across_drives(monkeypatch, source: Path):
    """Renaming the installation folder fails as it does between two drives."""
    original = os.rename

    def rename(a, b, *args, **kwargs):
        if Path(a) == source:
            raise OSError(errno.EXDEV, 'Invalid cross-device link')
        return original(a, b, *args, **kwargs)
    monkeypatch.setattr(move.os, 'rename', rename)


def assert_moved(source, target, settings, builds, scratch):
    assert not source.exists()
    assert Settings.load().root == settings.root == str(target)
    assert complete(target, builds, scratch)
    assert not move.journal_path().exists() and not (target / move.MARKER).exists()
    with Library(target).locked():          # an ordinary installation again
        pass


@pytest.mark.parametrize('drives', ['same', 'other'])
@pytest.mark.parametrize('destination', ['new', 'empty'])
def test_move_carries_every_client_to_the_new_folder(installed, tmp_path, monkeypatch, drives, destination):
    source, settings, builds = installed
    before = tree(source)
    target = tmp_path / 'drive-b' / 'Games'
    target.parent.mkdir()
    if destination == 'empty':
        target.mkdir()
    if drives == 'other':
        across_drives(monkeypatch, source)
    closed = []

    def removing(event):
        # While the old folder goes, the new one stays closed to other processes' jobs.
        if event['stage'] == 'removing_old':
            with pytest.raises(LibraryError, match='being moved'):
                with Library(target).locked():
                    pass
            closed.append(True)
    result = move.move(Library(source), target, settings, progress=removing)
    assert result['moved'] and result['copied'] == (drives == 'other') and not result['leftover']
    assert closed == ([True] if drives == 'other' else [])
    assert_moved(source, target, settings, builds, tmp_path)
    after = tree(target)
    # The copy's check opens the copied catalogue, which may rewrite its pages but not its rows.
    assert set(after) == set(before)
    assert {k: v for k, v in after.items() if 'sqlite' not in k} == {k: v for k, v in before.items() if 'sqlite' not in k}
    assert (target / '_retail_/WTF/account.wtf').read_bytes() == b'personal settings'


def test_a_copy_that_lost_bytes_on_the_new_drive_is_never_switched_to(installed, tmp_path, monkeypatch):
    source, settings, builds = installed
    before = tree(source)
    across_drives(monkeypatch, source)
    target = tmp_path / 'drive-b' / 'Games'
    target.parent.mkdir()
    damaged = []

    def flaky_drive(event):
        # The new drive loses the end of a file it already took, as a failing disk can.
        copied = target / '_retail_/Wow.exe'
        if event['stage'] == 'copying' and copied.is_file() and not damaged:
            copied.write_bytes(copied.read_bytes()[:-1] + b'?')
            damaged.append(copied)
    with pytest.raises(LibraryError, match='differs from the original'):
        move.move(Library(source), target, settings, progress=flaky_drive)
    assert damaged and not target.exists()
    assert tree(source) == before and Settings.load().root == str(source)
    assert not move.journal_path().exists()


@pytest.mark.skipif(sys.platform == 'win32' or os.geteuid() == 0, reason='needs a folder this user cannot write')
def test_a_switch_the_journal_could_not_record_is_undone(installed, tmp_path, monkeypatch):
    source, settings, builds = installed
    across_drives(monkeypatch, source)
    target = tmp_path / 'drive-b' / 'Games'
    target.parent.mkdir()
    data = move.journal_path().parent

    def full_data_folder(event):
        if event['stage'] == 'verifying':
            data.chmod(0o500)       # the journal cannot record the switch now
    try:
        with pytest.raises(OSError):
            move.move(Library(source), target, settings, progress=full_data_folder)
        assert settings.root == Settings.load().root == str(source)
        assert complete(source, builds, tmp_path)
    finally:
        data.chmod(0o700)
    move.recover(settings)
    assert settings.root == str(source) and complete(source, builds, tmp_path) and not target.exists()


def refusal(case, source, target, monkeypatch):
    """Arrange one reason a move must not start. Returns has_jobs for the move."""
    if case == 'running':
        monkeypatch.setattr(move.system, 'running_executables', lambda *a: [
            (1, str(source / '_retail_/Wow.exe'), str(source / '_retail_'))])
    elif case == 'job':
        return lambda: True
    elif case == 'not_empty':
        target.mkdir()
        (target / 'notes.txt').write_text('mine')
    elif case == 'file':
        target.write_text('a file')
    elif case == 'inside':
        return 'inside'
    elif case == 'link':
        (source / '_retail_/INTERFACE/elsewhere').symlink_to(target.parent, target_is_directory=True)
    elif case == 'mount':
        mounted = source / '_retail_/Screenshots'
        mounted.mkdir()
        monkeypatch.setattr(move, 'mount_points', lambda: [Path('/'), mounted.resolve()])
    elif case == 'space':
        across_drives(monkeypatch, source)
        monkeypatch.setattr(move.shutil, 'disk_usage', lambda path: shutil._ntuple_diskusage(10**12, 10**12, 1000))
    elif case == 'no_parent':
        return 'no_parent'
    return lambda: False


@pytest.mark.parametrize('case, words', [
    ('running', 'Close World of Warcraft'), ('job', 'Finish or remove every download'),
    ('not_empty', 'not empty'), ('file', 'a file, not a folder'),
    ('inside', 'inside the installation folder'), ('link', 'link at _retail_/INTERFACE/elsewhere'),
    ('mount', 'mounted at'), ('space', 'free, but the installation needs'), ('no_parent', 'does not exist')])
def test_move_refuses_in_plain_words_and_changes_nothing(installed, tmp_path, monkeypatch, case, words):
    source, settings, builds = installed
    target = tmp_path / 'drive-b' / 'Games'
    target.parent.mkdir()
    has_jobs = refusal(case, source, target, monkeypatch)
    if has_jobs == 'inside':
        target, has_jobs = source / '_retail_' / 'Moved', lambda: False
    elif has_jobs == 'no_parent':
        target, has_jobs = tmp_path / 'missing' / 'Games', lambda: False
    before, outside = tree(source), sorted(target.parent.rglob('*')) if target.parent.exists() else []
    with pytest.raises(LibraryError, match=words):
        move.move(Library(source), target, settings, has_jobs=has_jobs)
    assert tree(source) == before
    assert Settings.load().root == settings.root == str(source)
    assert not move.journal_path().exists() and not (source / move.MARKER).exists()
    if target.parent.exists():
        assert sorted(target.parent.rglob('*')) == outside


def test_nothing_starts_during_a_move_and_cancel_leaves_the_source_as_it_was(installed, tmp_path, monkeypatch):
    from scuffle.downloads import DONE, DownloadManager
    from scuffle.launch import Launcher, LaunchError
    source, settings, builds = installed
    def stamps():
        # Files only: the marker comes and goes in .scuffle, so that folder's own time changes.
        return {p: p.stat().st_mtime_ns for p in source.rglob('*')
                if p.is_file() and p.name not in {'library.lock', 'store.lock'}}
    before = stamps()
    before_bytes = tree(source)
    across_drives(monkeypatch, source)
    target = tmp_path / 'drive-b' / 'Games'
    target.parent.mkdir()
    manager = DownloadManager(settings, Library(source))
    copying, release = threading.Event(), threading.Event()

    def hold(task):
        # Listeners run on the worker, so this keeps the move in the middle of its copy.
        on_worker = threading.current_thread().name == 'scuffle-downloads'
        if on_worker and task.stage == 'copying' and task.done_bytes and not release.is_set():
            copying.set()
            release.wait(10)
    manager.move_listeners.append(hold)
    try:
        task = manager.start_move(str(target))
        assert copying.wait(10)
        with pytest.raises(LibraryError, match='moved'):
            manager.enqueue('wowf', 'verify')
        with pytest.raises(LibraryError, match='being moved'):
            with Library(source).locked():
                pass
        launcher = Launcher(settings, Library(source), runtime=object(), vault=object())
        with pytest.raises(LaunchError, match='being moved'):
            launcher.play('wow', lambda url: b'')
        manager.cancel_move()
        release.set()
        for _ in range(500):
            if not task.active:
                break
            threading.Event().wait(0.02)
        assert task.status == DONE and task.result == {'cancelled': True}
    finally:
        release.set()
        manager.shutdown()
        manager._worker.join(5)
    assert not target.exists()
    assert tree(source) == before_bytes
    assert stamps() == before
    assert Settings.load().root == str(source) and not move.journal_path().exists()
    with Library(source).locked():
        pass


def crashing_move(crash_at: int, drives: str, source: Path, target: Path) -> None:
    """Move, dying like a power loss at the crash_at-th file operation. Runs in a child process."""
    count = 0

    def crashing(original):
        def call(*args, **kwargs):
            nonlocal count
            count += 1
            if count == crash_at:
                os._exit(70)            # nothing after this runs, no cleanup either
            return original(*args, **kwargs)
        return call
    rename = os.rename

    def cross(a, b, *args, **kwargs):
        if drives == 'other' and Path(a) == source:
            raise OSError(errno.EXDEV, 'Invalid cross-device link')
        return rename(a, b, *args, **kwargs)
    os.rename = crashing(cross)
    for name in ('replace', 'rmdir', 'unlink', 'mkdir'):
        setattr(os, name, crashing(getattr(os, name)))
    shutil.copystat = crashing(shutil.copystat)     # after each copied file
    settings = Settings.load()
    move.move(Library(settings.root), target, settings)


def crash(step: int, drives: str, source: Path, target: Path) -> bool:
    """Run the move in a new process that dies at step. True when it got to the end instead."""
    if hasattr(os, 'fork'):
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', DeprecationWarning)     # the child never returns to pytest
            pid = os.fork()
        if pid == 0:
            try:
                crashing_move(step, drives, source, target)
                os._exit(0)
            except BaseException:
                os._exit(1)
        code = os.waitstatus_to_exitcode(os.waitpid(pid, 0)[1])
    else:
        code = subprocess.run([sys.executable, '-c', 'import sys, test_move; from pathlib import Path; '
                               'test_move.crashing_move(int(sys.argv[1]), sys.argv[2], Path(sys.argv[3]), Path(sys.argv[4]))',
                               str(step), drives, str(source), str(target)], timeout=60,
                              env=os.environ | {'PYTHONPATH': os.pathsep.join([str(SRC), str(Path(__file__).parent)])}).returncode
    assert code in (0, 70), f'the move failed at step {step} instead of crashing'
    return code == 0


@pytest.fixture
def scratch(tmp_path):
    """A folder in memory where Linux has one. Every step syncs dozens of files, which a disk makes slow."""
    if not Path('/dev/shm').is_dir():
        yield tmp_path
        return
    import tempfile
    folder = Path(tempfile.mkdtemp(prefix='scuffle-move-', dir='/dev/shm'))
    try:
        yield folder
    finally:
        shutil.rmtree(folder, ignore_errors=True)


@pytest.mark.parametrize('drives', ['same', 'other'])
def test_a_crash_at_any_step_leaves_one_complete_folder_and_recovery_settles_it(scratch, monkeypatch, drives):
    """Kills the move at every rename, write, copy and deletion in turn, then starts again."""
    tmp_path = scratch
    for name in ('XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'XDG_CACHE_HOME'):
        monkeypatch.setenv(name, str(tmp_path / 'home' / name.lower()))
    # No game runs here. Listing every process of the machine at each step costs most of the time.
    monkeypatch.setattr(move.system, 'running_executables', lambda *names: [])
    source = tmp_path / 'drive-a' / 'Scuffle'
    builds = {'wowf': SyntheticBuild()}
    run_build(source, FixtureCDN(builds['wowf'], tmp_path / 'cache'))
    Settings(root=str(source)).save()
    pristine = tmp_path / 'pristine'
    shutil.copytree(source, pristine)
    settings_file = Settings.path().read_bytes()
    target = tmp_path / 'drive-b' / 'Games'
    target.parent.mkdir()
    finished, step, seen = False, 0, set()
    while not finished:
        step += 1
        finished = crash(step, drives, source, target)
        # At the moment of the crash, one whole installation exists.
        whole = [root for root in (source, target) if complete(root, builds, tmp_path)]
        assert whole, step
        if drives == 'other' and source not in whole:
            # Deleting the old folder begins only once the setting names the checked copy.
            assert target in whole and Settings.load().root == str(target), step
        settings = Settings.load()
        result = move.recover(settings)          # what the next start does
        root = Path(settings.root)
        assert root in whole and Settings.load().root == settings.root, step
        assert not move.journal_path().exists() and not (root / move.MARKER).exists(), step
        assert not (target if root == source else source).exists(), step
        with Library(root).locked():
            pass
        seen.add(result and result['recovered'])
        # The next step starts from the original installation again.
        shutil.rmtree(root)
        shutil.copytree(pristine, source)
        Settings.path().write_bytes(settings_file)
        for probe in tmp_path.glob('probe-*'):
            shutil.rmtree(probe)
        assert step < 400
    # Both ways out were taken: crashes before the switch undo the move, after it finish it.
    assert {'undone', 'finished'} <= seen, seen


def test_recovery_never_deletes_a_folder_the_move_did_not_make(scratch, monkeypatch):
    """After a crash mid-copy the user replaces the new folder with their own. It stays."""
    for name in ('XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'XDG_CACHE_HOME'):
        monkeypatch.setenv(name, str(scratch / 'home' / name.lower()))
    monkeypatch.setattr(move.system, 'running_executables', lambda *names: [])
    source, target = scratch / 'drive-a' / 'Scuffle', scratch / 'drive-b' / 'Games'
    build = SyntheticBuild()
    run_build(source, FixtureCDN(build, scratch / 'cache'))
    Settings(root=str(source)).save()
    target.parent.mkdir()
    assert not crash(40, 'other', source, target)       # in the middle of the copy
    assert (target / move.MARKER).is_file()
    shutil.rmtree(target)
    target.mkdir()
    (target / 'mine.txt').write_text('the user\'s own file')
    settings = Settings.load()
    assert move.recover(settings)['recovered'] == 'undone'
    assert (target / 'mine.txt').read_text() == 'the user\'s own file'
    assert settings.root == str(source) and complete(source, {'wowf': build}, scratch)
    assert not move.journal_path().exists()


def crash_in_phase(scratch, monkeypatch, phase):
    """A real installation whose move to another drive died with its journal in phase."""
    for name in ('XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'XDG_CACHE_HOME'):
        monkeypatch.setenv(name, str(scratch / 'home' / name.lower()))
    monkeypatch.setattr(move.system, 'running_executables', lambda *names: [])
    source, target = scratch / 'drive-a' / 'Scuffle', scratch / 'drive-b' / 'Games'
    builds = {'wowf': SyntheticBuild()}
    run_build(source, FixtureCDN(builds['wowf'], scratch / 'cache'))
    Settings(root=str(source)).save()
    target.parent.mkdir()
    pristine, settings_file = scratch / 'pristine', Settings.path().read_bytes()
    shutil.copytree(source, pristine)
    for step in range(1, 400):
        assert not crash(step, 'other', source, target)
        if move.journal_path().is_file() and json.loads(move.journal_path().read_text())['phase'] == phase:
            return source, target, builds
        move.recover(Settings.load())
        for root in (source, target):
            shutil.rmtree(root, ignore_errors=True)
        shutil.copytree(pristine, source)
        Settings.path().write_bytes(settings_file)
    raise AssertionError(f'the move never reached {phase}')


@pytest.mark.parametrize('phase', ['switched', 'deleting'])
def test_recovery_keeps_the_old_folder_when_the_new_one_vanished_after_a_crash(scratch, monkeypatch, phase):
    """The copy was checked and switched to, then the user deleted it before Scuffle started again."""
    source, target, builds = crash_in_phase(scratch, monkeypatch, phase)
    shutil.rmtree(target)
    left = tree(source)
    settings = Settings.load()
    if phase == 'switched':
        # Nothing was deleted yet, so the old folder is whole and the move is undone.
        assert move.recover(settings)['recovered'] == 'undone'
        assert settings.root == Settings.load().root == str(source)
        assert complete(source, builds, scratch) and not move.journal_path().exists()
    else:
        with pytest.raises(LibraryError, match='deletes nothing more'):
            move.recover(settings)
        assert move.journal_path().exists()
        assert tree(source) == left         # what is left of the old folder stays


@pytest.mark.skipif(sys.platform == 'win32' or os.geteuid() == 0, reason='needs a folder this user cannot write')
def test_recovery_deletes_nothing_while_the_setting_cannot_be_saved(scratch, monkeypatch):
    """As the window and then the worker recover, with a settings folder that refuses writes."""
    source, target, builds = crash_in_phase(scratch, monkeypatch, 'switched')
    settings = Settings.load()
    Settings.path().parent.chmod(0o500)
    try:
        for delete in (False, True):
            with pytest.raises(OSError):
                move.recover(settings, delete=delete)
            # The object goes on naming what the file names, so a retry saves again first.
            assert settings.root == Settings.load().root == str(source)
            assert complete(source, builds, scratch) and move.journal_path().exists()
    finally:
        Settings.path().parent.chmod(0o700)
    assert move.recover(settings)['recovered'] == 'finished'
    assert Settings.load().root == str(target) and not source.exists()
