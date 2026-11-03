"""Deliberately break moving the installation folder and require the move tests to catch it.

Run with nix develop -c python3 tools/check_move_mutations.py.
Sources are restored after every mutation. Reports stay in .pytest_cache.
"""
from check_uninstall_mutations import ROOT, replace, run

OUTPUT = ROOT / '.pytest_cache' / 'move-mutations'
MOVE = 'src/waystone/move.py'
LIB = 'src/waystone/library.py'
QUEUE = 'src/waystone/downloads.py'
LAUNCH = 'src/waystone/launch.py'
APP = 'src/waystone/ui/app.py'


def test(name, parameter='', file='move'):
    return f'tests/test_{file}.py::test_{name}{parameter}'


CARRY = test('move_carries_every_client_to_the_new_folder')
REFUSE = test('move_refuses_in_plain_words_and_changes_nothing')
BUSY = test('nothing_starts_during_a_move_and_cancel_leaves_the_source_as_it_was')
CRASH = test('a_crash_at_any_step_leaves_one_complete_folder_and_recovery_settles_it')
UI_MOVE = test('move_takes_the_installation_to_the_chosen_folder_and_the_window_follows', file='ui')
UI_RECOVER = test('the_window_undoes_a_move_a_crash_cut_short_before_it_opens_the_folder', file='ui')
UI_OFFER = test('move_is_offered_only_without_a_download_or_a_game', file='ui')
# Each mutation removes one guarantee the move gives, not an assertion or its wording.
MUTATIONS = [
    ('forget-setting-after-rename', [CARRY + '[new-same]', CARRY + '[empty-same]', UI_MOVE], [
        replace(MOVE, "    try:\n        save_root(settings, target)\n    except OSError as error:\n        # The setting",
                "    try:\n        pass\n    except OSError as error:\n        # The setting")]),
    ('forget-setting-after-copy', [CARRY + '[new-other]', CARRY + '[empty-other]'], [
        replace(MOVE, "    try:\n        save_root(settings, target)\n    except OSError as error:\n        settings.root",
                "    try:\n        pass\n    except OSError as error:\n        settings.root")]),
    ('skip-copy-check', [test('a_copy_that_lost_bytes_on_the_new_drive_is_never_switched_to')], [
        replace(MOVE, '    _verify(source, target, folders, files, cancelled)\n', '')]),
    ('delete-source-before-switch', [CRASH + '[other]'], [
        replace(MOVE, "    record['phase'] = 'switched'\n    write_journal(record)\n    try:",
                "    _delete_marked(source, record['token'], keep_folder=False)\n"
                "    record['phase'] = 'switched'\n    write_journal(record)\n    try:")]),
    ('undo-a-switched-move', [CRASH + '[other]'], [
        replace(MOVE, "switched = record['phase'] == 'switched'", 'switched = False')]),
    ('trust-the-rename-side-blindly', [CRASH + '[same]'], [
        replace(MOVE, '        if marker_token(target) == token:\n            switched = True',
                '        if True:\n            switched = True')]),
    ('delete-without-the-token', [test('recovery_never_deletes_a_folder_the_move_did_not_make')], [
        replace(MOVE, "        partial = record['mode'] == 'copy' and marker_token(target) == token",
                "        partial = record['mode'] == 'copy'"),
        replace(MOVE, '    if marker_token(root) != token:\n        # Already deleted', '    if False:\n        # Already deleted')]),
    ('move-while-busy', [REFUSE + '[running-Close World of Warcraft]', REFUSE + '[job-Finish or remove every download]'], [
        replace(MOVE, '            refuse_busy(library, has_jobs)\n', '')]),
    ('move-links-and-mounts', [REFUSE + '[link-link at _retail_/INTERFACE/elsewhere]', REFUSE + '[mount-mounted at]'], [
        replace(MOVE, 'if stat.S_ISLNK(info.st_mode) or path.is_junction():', 'if False:'),
        replace(MOVE, '    if mounted:\n', '    if False:\n')]),
    ('ignore-free-space', [REFUSE + '[space-free, but the installation needs]'], [
        replace(MOVE, '    if free < total + SPARE:\n        raise', '    if False:\n        raise')]),
    ('accept-any-destination', [REFUSE + f'[{case}]' for case in (
            'not_empty-not empty', 'file-a file, not a folder', 'inside-inside the installation folder',
            'no_parent-does not exist')], [
        replace(MOVE, '            target = check_destination(library, destination)\n',
                '            target = Path(destination)\n')]),
    ('lock-ignores-the-marker', [BUSY], [
        replace(LIB, '                if self.moving():\n                    # Checked', '                if False:\n                    # Checked')]),
    ('queue-during-a-move', [BUSY], [
        replace(QUEUE, "            if self.moving and self.moving.active:\n                raise LibraryError('Wait until",
                "            if False:\n                raise LibraryError('Wait until")]),
    ('play-during-a-move', [BUSY], [
        replace(LAUNCH, '        if self.library.moving():', '        if False:')]),
    ('ignore-cancel', [BUSY], [
        replace(QUEUE, 'cancelled=lambda: task.cancel_requested or self._stopping', 'cancelled=lambda: False')]),
    ('window-skips-recovery', [UI_RECOVER], [
        replace(APP, '        self.move_notice = self.recover_move()', '        self.move_notice = None')]),
    ('worker-skips-the-leftover-copy', [UI_RECOVER], [
        replace(QUEUE, '        if record:\n            self.moving = MoveTask(', '        if False:\n            self.moving = MoveTask(')]),
    ('offer-move-while-busy', [UI_OFFER + '[job]', UI_OFFER + '[game]'], [
        replace(APP, '    def move_reason(self) -> str:\n        """Why the installation folder cannot move now, or nothing."""\n',
                '    def move_reason(self) -> str:\n        """Why the installation folder cannot move now, or nothing."""\n        return \'\'\n')]),
]


def main():
    run(MUTATIONS, OUTPUT, lambda test_id: test_id.startswith('tests/test_move.py::')
        or 'tests/test_ui.py::test_move_' in test_id or UI_RECOVER in test_id)


if __name__ == '__main__':
    main()
