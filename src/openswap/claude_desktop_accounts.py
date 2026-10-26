"""One account selection for Claude Desktop and Code; native UI stays on the main thread."""

from __future__ import annotations

import queue
import threading

from openswap.claude_desktop import ClaudeDesktopError, Profile
from openswap.claude_desktop_switch import ClaudeDesktopSwitch
from openswap.exceptions import ClaudeSwitchError


class ClaudeDesktopAccounts:
    def __init__(self, engine, profiles, *, prompt, alert, show_error, changed, enabled,
                 persisted_enabled, blocked, accounts, name, completed, confirm):
        self.profiles = profiles
        self.prompt, self.alert, self.show_error = prompt, alert, show_error
        self.changed, self.enabled, self.blocked = changed, enabled, blocked
        self.accounts, self.name, self.completed, self.confirm = accounts, name, completed, confirm
        self._results = queue.SimpleQueue()
        self.pending = False
        self.switch = ClaudeDesktopSwitch(engine, profiles,
            enabled=lambda: enabled() and persisted_enabled() and not blocked(),
            completed=lambda result, error: self._results.put((result, error, False)))

    @property
    def busy(self):
        return self.pending or self.switch.busy

    def _ready(self):
        return self.enabled() and not self.busy and not self.blocked()

    def _error(self, exc):
        self.show_error(str(exc) if isinstance(exc, ClaudeSwitchError)
                        else 'Could not complete the Desktop selection. Check both apps before retrying.')

    def link(self, number, profile=None):
        if not self._ready():
            return None
        try:
            account = self.switch.account(number)
            new_name = None
            choices = []
            if profile is None:
                choices = [p for p in self.profiles.list() if p.code_account in (None, account)]
                response = self.prompt(title='Link a Claude Desktop profile',
                    message=f'{account.email}\nWorkspace: {account.organization or "Personal / unspecified"}\n\nChoose the profile you use for this account. OpenSwap cannot verify its Desktop sign-in. A new profile will ask you to sign in after opening.',
                    choices=['Create a new profile…', *[p.name for p in choices]], ok='Continue', cancel='Cancel')
                if response.clicked != 1:
                    return None
                if response.choice == 0:
                    named = self.prompt(title='Name the Desktop profile', message='Use a name you will recognize in the account picker.',
                                        default_text=self.name(number), ok='Create and link', cancel='Cancel')
                    if named.clicked != 1:
                        return None
                    if not self._ready() or self.switch.account(number) != account:
                        raise ClaudeDesktopError('The account or Desktop mode changed. Try linking again.')
                    new_name = named.text
                    profile = Profile('', named.text)
                elif 0 < response.choice <= len(choices):
                    profile = choices[response.choice - 1]
                else:
                    return None
            if not self._ready() or self.switch.account(number) != account:
                raise ClaudeDesktopError('The account or Desktop mode changed. Try linking again.')
            if self.alert(title='Link Desktop and Code?',
                message=f'Code account: {account.email}\nWorkspace: {account.organization or "Personal / unspecified"}\nDesktop profile: {profile.name}\n\nSelecting this account will change the Code login and open this profile. Confirm the profile uses the same account and workspace; sign in there on first use. Linking does not copy credentials.',
                ok='Link profile', cancel='Cancel') != 1:
                return None
            if not self._ready() or self.switch.account(number) != account:
                raise ClaudeDesktopError('The account or Desktop mode changed. Try linking again.')
            if new_name is not None:
                profile = self.profiles.add(new_name)
            linked = self.profiles.assign(profile.id, account)
            self.changed()
            return linked
        except (ClaudeSwitchError, OSError, ValueError) as exc:
            self._error(exc)
            return None

    def link_profile(self, profile):
        if not self._ready():
            return
        choices = []
        for number in self.accounts():
            try:
                account = self.switch.account(number)
            except (ClaudeSwitchError, ValueError, OSError):
                continue
            choices.append((str(number), account))
        if not choices:
            self.show_error('Add a Claude Code account first, then link this Desktop profile.')
            return
        response = self.prompt(title='Choose the Code account', message=f'Link {profile.name} to its saved Code account and workspace.',
            choices=[f'{self.name(n)} · {a.email} · {a.organization or "Personal / unspecified"}' for n, a in choices],
            ok='Continue', cancel='Cancel')
        if response.clicked == 1 and 0 <= response.choice < len(choices):
            number, account = choices[response.choice]
            try:
                if self.switch.account(number) != account:
                    raise ClaudeDesktopError('That account changed. Choose it again.')
            except (ClaudeSwitchError, OSError, ValueError) as exc:
                self._error(exc)
                return
            self.link(number, profile)

    def select_profile(self, profile):
        if not self._ready():
            return
        try:
            current = self.profiles.get(profile.id)
            if current.code_account != profile.code_account or current.code_account is None:
                raise ClaudeDesktopError('The profile link changed. Open its menu again.')
        except (ClaudeSwitchError, ValueError, OSError) as exc:
            self._error(exc)
            return
        for number in self.accounts():
            try:
                if self.switch.account(number) == profile.code_account:
                    self.select(number, expected_profile=profile.id)
                    return
            except (ClaudeSwitchError, ValueError, OSError):
                continue
        self.show_error('The linked Code account is no longer in this account store. Unlink the profile and choose its current Code account.')

    def select(self, number, *, expected_profile=None):
        if not self._ready():
            return
        try:
            account = self.switch.account(number)
            profile = self.profiles.linked(account, include_archived=True)
            if expected_profile is not None and (profile is None or profile.id != expected_profile):
                raise ClaudeDesktopError('The profile link changed. Open its menu again.')
            if profile is None:
                profile = self.link(number)
                if profile is None:
                    return
            if profile.archived:
                raise ClaudeDesktopError('This account’s Desktop profile is archived. Restore it in Profiles, or choose another link from the account’s menu.')
            if self.confirm() and self.alert(title='Select Desktop and Code?',
                message=f'Code account: {account.email}\nWorkspace: {account.organization or "Personal / unspecified"}\nDesktop profile: {profile.name}\n\nExisting Desktop windows stay open. Conversations are not moved. Running Code clients may need to reload their login. Check the account inside Desktop before starting work.',
                ok='Select both', cancel='Cancel') != 1:
                return
            if not self._ready() or self.switch.account(number) != account:
                raise ClaudeDesktopError('The account or Desktop mode changed. Select the account again.')
        except (ClaudeSwitchError, ValueError, OSError) as exc:
            self._error(exc)
            return
        self.pending = True
        self.changed()

        def work():
            try:
                self._results.put((self.switch.select(str(number), expected=account, expected_profile=profile.id), None, True))
            except Exception as exc:
                self._results.put((None, exc, True))
        try:
            threading.Thread(target=work, daemon=True, name='claude-desktop-select').start()
        except Exception as exc:
            self._results.put((None, exc, True))

    def poll(self):
        try:
            result, error, manual = self._results.get_nowait()
        except queue.Empty:
            return
        if manual:
            self.pending = False
        self.changed()
        self.completed(result, error, manual)

    def labels(self):
        """Decorations only. Actual selection always re-resolves both identities."""
        if not self.enabled():
            return {}
        try:
            links = {p.code_account: p for p in self.profiles.list(include_archived=True) if p.code_account is not None}
            result = {}
            for number in self.accounts():
                try:
                    profile = links.get(self.switch.account(number))
                    result[str(number)] = ('Desktop: ' + profile.name + (' · Archived' if profile.archived else '')
                                          if profile else 'Desktop: Click to link a profile')
                except (ClaudeSwitchError, ValueError, OSError):
                    result[str(number)] = 'Desktop: OAuth account required'
            return result
        except (ClaudeSwitchError, OSError):
            return {str(n): 'Desktop: Profile links unavailable' for n in self.accounts()}
