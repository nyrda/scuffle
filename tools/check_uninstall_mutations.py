"""Deliberately break uninstall behavior and require each new test to catch it.

Run with nix develop -c python3 tools/check_uninstall_mutations.py.
Sources are restored after every mutation. Reports stay in .pytest_cache.
"""
from pathlib import Path
import importlib.util
import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / '.pytest_cache' / 'uninstall-mutations'
LIB = 'src/scuffle/library.py'
QUEUE = 'src/scuffle/downloads.py'
APP = 'src/scuffle/ui/app.py'
PAGES = 'src/scuffle/ui/pages.py'


def test(file, name, parameter=''):
    return f'tests/test_{file}.py::test_uninstall_{name}{parameter}'


def replace(path, old, new):
    return (path, old, new)


def method(path, name, next_name, body):
    source = (ROOT / path).read_text()
    start = source.index(f'    def {name}(')
    end = source.index(f'    def {next_name}(', start)
    old = source[start:end]
    signature = old[:old.index('\n') + 1]
    return replace(path, old, signature + body + '\n')


REMOVAL = test('library', 'removes_only_selected_client_and_unused_content')
BACKUP = test('library', 'settings_backup_preserves_user_files')
BUSY = test('library', 'refuses_busy_client_without_changing_files')
MENU = test('ui', 'menu_is_only_available_for_idle_installed_clients')
FLOW = test('ui', 'confirmation_cancel_and_explicit_choice')
WORKER = test('downloads', 'worker_uses_library_without_cdn_and_cannot_cancel')
# Each mutation changes a behavior, rather than the assertion or its error text.
MUTATIONS = [
    ('skip-cleanup', [REMOVAL, test('library', 'defers_cleanup_while_another_client_runs')], [
        replace(LIB, 'cleanup = self.cleanup(threshold=0.0)', 'cleanup = {}')]),
    ('skip-build-info', [REMOVAL], [
        replace(LIB, '        self.write_build_info()\n        if client.exists():', '        if client.exists():')]),
    ('discard-shared-content', [REMOVAL], [
        replace(LIB, '        for code in self.installed():\n            if self.pending(code):',
                '        plans = []\n        for code in self.installed():\n            if self.pending(code):')]),
    ('discard-settings', [BACKUP + '[True]'], [
        replace(LIB, "if record['keep_settings'] and trash.exists():", 'if False and trash.exists():')]),
    ('ignore-delete-settings-choice', [BACKUP + '[False]'], [
        replace(LIB, "if record['keep_settings'] and trash.exists():", 'if trash.exists():')]),
    ('allow-running-client', [BUSY + '[running]'], [
        replace(LIB, "if self.running(code):\n            raise LibraryError('Close the client before uninstalling')",
                "if False:\n            raise LibraryError('Close the client before uninstalling')")]),
    ('allow-pending-update', [BUSY + '[pending]', BUSY + '[empty_pending]'], [
        replace(LIB, 'if self.pending(code) is not None:', 'if False:')]),
    ('allow-download-job', [BUSY + '[job]'], [
        replace(LIB, 'if self.has_download_job(code):', 'if False:')]),
    ('skip-path-safety', [test('library', 'refuses_symlinks_without_touching_the_target'),
                          test('library', 'refuses_unsafe_or_other_client_folder')], [
        # Remove the cooperating checks as a unit, including the pre-existing
        # lock check. Leaving a second guard would produce an equivalent mutant.
        replace(LIB, '        self.reject_symlinks()\n', ''),
        method(LIB, '_removal_path', '_removal_tree', '        return self.root / relative\n'),
        method(LIB, '_removal_tree', '_uninstall_folder', '        pass\n'),
        method(LIB, '_uninstall_folder', 'uninstall', '        return self.root / folder\n')]),
    ('skip-lock-recovery', [test('library', 'interruption_recovers_on_next_start')], [
        replace(LIB, 'if self.uninstall_record.exists():', 'if False:')]),
    ('skip-startup-recovery', [test('library', 'restart_worker_finishes_journal_even_without_auto_resume')], [
        replace(QUEUE, 'if (code := library.uninstalling()):', 'if False:')]),
    ('recover-over-running-game', [test('library', 'recovery_refuses_a_game_started_after_the_crash')], [
        replace(LIB, 'if any(self._executable_running(path) for path in game.iterdir()',
                'if False and any(self._executable_running(path) for path in game.iterdir()')]),
    ('replace-existing-job', [test('downloads', 'cannot_replace_a_download_job')], [
        replace(QUEUE, "if kind == 'uninstall' and existing and existing.status != DONE and existing.kind != 'uninstall':",
                'if False:')]),
    ('restore-unconfirmed-uninstall', [test('downloads', 'queue_ignores_unstarted_requests_on_restore')], [
        replace(QUEUE, "('install', 'update', 'verify', 'repair'):",
                "('install', 'update', 'verify', 'repair', 'uninstall'):")]),
    ('use-updater-for-uninstall', [WORKER], [
        replace(QUEUE, "        if job.kind == 'uninstall':\n            job.stage", '        if False:\n            job.stage')]),
    ('lose-database-errors', [test('downloads', 'worker_reports_database_errors')], [
        replace(QUEUE, 'RuntimeError, sqlite3.Error) as failure:', 'RuntimeError) as failure:')]),
    ('show-menu-for-uninstalled-client', [MENU + '[none]'], [
        replace(PAGES, 'self.folder_action, self.uninstall_action, self.menu_separator',
                'self.folder_action, self.menu_separator')]),
    ('enable-menu-while-busy', [MENU + f'[{p}]' for p in ['running', 'external_running', 'queued', 'paused', 'failed', 'pending']], [
        replace(APP, '    def uninstall_reason(self, code: str) -> str:\n',
                "    def uninstall_reason(self, code: str) -> str:\n        return ''\n")]),
    ('unsafe-confirmation-default', [FLOW], [
        replace(PAGES, 'self.keep.setChecked(True)', 'self.keep.setChecked(False)')]),
    ('ignore-confirmed-choice', [FLOW], [
        replace(APP, 'keep_settings=page.keep.isChecked()', 'keep_settings=not page.keep.isChecked()')]),
    ('skip-final-busy-check', [test('ui', 'confirmation_rechecks_if_client_starts')], [
        replace(APP, 'reason = self.uninstall_reason(page.code)', "reason = ''")]),
    ('skip-storage-refresh', [test('ui', 'completion_refreshes_storage_and_not_installed_notice')], [
        replace(APP, "job.kind in {'install', 'update', 'uninstall'}", "job.kind in {'install', 'update'}")]),
    ('lose-optional-client-notice', [test('ui', 'completion_refreshes_storage_and_not_installed_notice', '[wow_classic_era_ptr]')], [
        replace(APP, "client=job.client if job.client in self.client_list.rows else ''", 'client=job.client')]),
]


def run(mutations, output, expected) -> None:
    """Apply each mutation, run its tests, and require every expected test case to catch one.

    expected tells from a collected test id whether it belongs to the checked tests.
    """
    output.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    for name in ('XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'XDG_CACHE_HOME', 'APPDATA', 'LOCALAPPDATA'):
        env[name] = str(output / 'private-home' / name)
    env['QT_QPA_PLATFORM'] = 'offscreen'
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    results, covered = [], set()
    for name, tests, changes in mutations:
        originals = {path: (ROOT / path).read_text() for path, _, _ in changes}
        try:
            for path, old, new in changes:
                file = ROOT / path
                source = file.read_text()
                if old not in source:
                    raise RuntimeError(f'{name}: mutation anchor missing in {path}')
                file.write_text(source.replace(old, new))
                Path(importlib.util.cache_from_source(str(file))).unlink(missing_ok=True)
            report = output / f'{name}.xml'
            run = subprocess.run([sys.executable, '-m', 'pytest', '-q', *tests, f'--junitxml={report}'],
                                 cwd=ROOT, env=env, capture_output=True, text=True, timeout=300)
            (output / f'{name}.log').write_text(run.stdout + run.stderr)
            cases = list(ET.parse(report).getroot().iter('testcase'))
            killed = [c.attrib['name'] for c in cases if c.find('failure') is not None]
            passed = [c.attrib['name'] for c in cases if c.find('failure') is None]
            ok = run.returncode == 1 and killed and not passed
            covered.update(killed)
            results.append({'mutation': name, 'killed': killed, 'survived_or_error': passed, 'ok': bool(ok)})
            print(f'{name}: {len(killed)} test cases detected the mutation' + ('' if ok else ' FAILED'), flush=True)
        finally:
            for path, source in originals.items():
                (ROOT / path).write_text(source)
                Path(importlib.util.cache_from_source(str(ROOT / path))).unlink(missing_ok=True)
    collection = subprocess.run([sys.executable, '-m', 'pytest', '--collect-only', '-q'],
                                cwd=ROOT, env=env, capture_output=True, text=True, check=True)
    wanted = {line.split('::')[-1] for line in collection.stdout.splitlines() if '::' in line and expected(line)}
    missing = sorted(wanted - covered)
    summary = {'mutations': results, 'covered_test_cases': len(covered), 'missing': missing}
    (output / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n')
    print(f'Covered {len(covered)}/{len(wanted)} new test cases; report: {output / "summary.json"}')
    if missing or not all(row['ok'] for row in results):
        raise SystemExit(1)


def main():
    run(MUTATIONS, OUTPUT, lambda test_id: '::test_uninstall_' in test_id)


if __name__ == '__main__':
    main()
