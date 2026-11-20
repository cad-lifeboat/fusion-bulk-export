"""What the user sees when something goes wrong.

Expected conditions (an unusable output folder, an unreadable manifest, a full disk, Fusion
signed out) are a UserError: one or two plain sentences and what to do next. Anything else is
unexpected: its details go to errors.log beside the add-in, and the dialog says where that
is and shows the details in a box they can be copied from for a bug report.
"""
import json
import os
import time

from .core import Trouble, disk_full
from .lock import FolderBusy

TITLE = 'Fusion Bulk Export'


class UserError(Exception):
    """An expected problem: message says what happened, next_step what to do about it."""
    def __init__(self, message, next_step=''):
        super().__init__(message)
        self.message, self.next_step = message, next_step

    def text(self):
        return self.message + ('\n\n' + self.next_step if self.next_step else '')


# What to do after the run stopped itself, by core.Trouble code.
NEXT_STEP = {
    'disk_full': 'Free some disk space (or choose a folder on another disk), then export again: '
                 'it carries on where it stopped.',
    'output_missing': 'Reconnect the drive or choose the folder again, then export again: '
                      'it carries on where it stopped.',
    'output_unwritable': 'Choose a folder you can write to, then export again.',
    'failing': 'Check that Fusion is online and signed in, then export again: '
               'the failed designs are tried again.',
}
SIGN_IN = 'Sign in to Fusion (top right), wait until the Data Panel shows your projects, then try again.'


def trouble(code, reason):
    """UserError for a run that stopped itself (core.Trouble code and reason)."""
    return UserError('The export stopped: ' + reason,
                     NEXT_STEP.get(code, 'Export again: it carries on where it stopped.'))


def no_hubs():
    return UserError('Fusion shows no hubs, so there is nothing to export. You may be signed out.', SIGN_IN)


def output_folder(error, folder):
    """UserError for a folder the exporter cannot use (raised while opening it), or None."""
    where = str(folder)
    if isinstance(error, UserError):
        return error
    if isinstance(error, json.JSONDecodeError):
        return UserError('The manifest.json in %s cannot be read; it may be damaged.' % where,
                         'Choose another folder. To start this one afresh, move its manifest.json away '
                         '(the files already there are then exported again).')
    text = str(error)
    if 'Unsupported manifest schema' in text:
        return UserError('The folder %s was written by a newer version of Fusion Bulk Export.' % where,
                         'Install the latest version, or choose another folder.')
    if isinstance(error, (ValueError, OSError)):
        if isinstance(error, OSError) and disk_full(error):
            return trouble('disk_full', 'The disk is full.')
        reason = text if isinstance(error, ValueError) else (error.strerror or text)
        if isinstance(error, ValueError) and 'output folder' not in text:
            return None
        return UserError('This folder cannot be used for the export: %s' % reason,
                         'Choose a folder on this computer that you can write to.')
    return None


def friendly(error):
    """The UserError for an error raised during an export, or None when it is unexpected."""
    if isinstance(error, UserError):
        return error
    if isinstance(error, Trouble):
        return trouble(error.code, error.reason)
    if isinstance(error, FolderBusy):
        return UserError('Another export is using this folder (in another Fusion window, or a job from the '
                         'command line).', 'Wait for it to finish, or choose another folder.')
    if isinstance(error, OSError) and disk_full(error):
        return trouble('disk_full', 'The disk is full.')
    if isinstance(error, TimeoutError) and 'hubs' in str(error):
        return no_hubs()
    return None


def log_unexpected(log_dir, title, detail):
    """Append detail to errors.log in log_dir; returns its path, or None if it cannot be written."""
    if not log_dir:
        return None
    path = os.path.join(log_dir, 'errors.log')
    try:
        with open(path, 'a', encoding='utf-8') as stream:
            stream.write('%s %s\n%s\n' % (time.strftime('%Y-%m-%dT%H:%M:%S'), title, detail))
        return path
    except OSError:
        return None


def message_box(ui, text, title=TITLE, warning=True):
    """messageBox with a warning icon where this Fusion offers the constants."""
    import adsk.core
    buttons = getattr(getattr(adsk.core, 'MessageBoxButtonTypes', None), 'OKButtonType', None)
    icon = getattr(getattr(adsk.core, 'MessageBoxIconTypes', None),
                   'WarningIconType' if warning else 'InformationIconType', None)
    if buttons is None or icon is None:
        return ui.messageBox(text, title)
    return ui.messageBox(text, title, buttons, icon)


def show(ui, title, error, detail, log_dir):
    """Tell the user about error: plainly when expected, with the logged details when not."""
    known = friendly(error)
    if known is not None:
        message_box(ui, known.text(), title)
        return 'user'
    path = log_unexpected(log_dir, title, detail)
    where = ('The details are saved in %s.' % path) if path else 'The details could not be saved to errors.log.'
    prompt = ('Something unexpected went wrong. %s\n\nTo report it, copy the text below into a new issue '
              '(replace your user name where it appears in paths).' % where)
    # A one-line box, so the whole text can be selected and copied.
    ui.inputBox(prompt, title, ' | '.join(line.rstrip() for line in detail.strip().splitlines()))
    return 'unexpected'
