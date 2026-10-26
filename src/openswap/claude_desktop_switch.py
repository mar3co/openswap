"""Coordinate a saved Code account with a user-linked Desktop profile.

Desktop sign-ins are owned by Claude. A link is user-supplied routing metadata,
never evidence of authentication. No credentials are copied into a profile.
"""

from __future__ import annotations

import threading

from openswap.claude_desktop import ClaudeDesktopApp, ClaudeDesktopError, CodeAccount
from openswap.exceptions import ClaudeSwitchError


class DesktopSelectionError(ClaudeDesktopError):
    def __init__(self, message, *, code_result=None, desktop_result=None):
        super().__init__(message)
        self.code_result = code_result
        self.desktop_result = desktop_result


class ClaudeDesktopSwitch:
    def __init__(self, engine, profiles, *, enabled, app_factory=None, completed=None):
        self.engine = engine
        self.profiles = profiles
        self.enabled = enabled
        self.app_factory = app_factory or ClaudeDesktopApp
        self.completed = completed or (lambda result, error: None)
        self._operation = threading.Lock()

    @property
    def busy(self):
        return self._operation.locked()

    def account(self, number):
        identity = self.engine.slot_identity(str(number))
        if identity is None or self.engine.account_kind_for(str(number)) == 'api_key':
            raise ClaudeDesktopError('Choose a saved Claude OAuth account. Desktop profiles cannot use an API-key account.')
        return CodeAccount.for_identity(self.engine.backup_dir, identity)

    def select(self, number, *, expected=None, expected_profile=None):
        """Preflight both targets, change Code, then open Desktop. Never undo a newer login."""
        if not self._operation.acquire(blocking=False):
            raise ClaudeDesktopError('Another Desktop selection is in progress. Wait for it to finish.')
        code_result = desktop_result = None
        try:
            if not self.enabled():
                raise ClaudeDesktopError('Desktop features are off. No account was selected.')
            account = self.account(number)
            if expected is not None and expected != account:
                raise ClaudeDesktopError('The saved account changed. Review its Desktop link and try again.')
            if str(number) not in {str(n) for n in self.engine.switchable_account_numbers()}:
                raise ClaudeDesktopError('This Code account is unavailable or held out of rotation. Repair or enable it before selecting both apps.')
            profile = self.profiles.linked(account, include_archived=True)
            if profile is None:
                raise ClaudeDesktopError('Link a Desktop profile to this account first.')
            if expected_profile is not None and profile.id != expected_profile:
                raise ClaudeDesktopError('The Desktop link changed after confirmation. Select the account again.')
            if profile.archived:
                raise ClaudeDesktopError('The linked Desktop profile is archived. Restore it or unlink it before selecting this account.')
            app = self.app_factory()
            info = app.validate()
            # The same lock is used by link, archive, rename and standalone open.
            # Resolve again under it, after the slow publisher check.
            with self.profiles._lock():
                current = self.profiles.get(profile.id)
                if current.code_account != account or self.account(number) != account:
                    raise ClaudeDesktopError('The account or Desktop link changed. Review it and try again.')
                desktop, code = self.profiles._directories(current)
                app._running([str(app.binary), f'--user-data-dir={desktop}'], desktop)
                if not self.enabled():
                    raise ClaudeDesktopError('Desktop features were turned off. No account was selected.')
                code_result = self.engine.switch_to(str(number), json_output=True, expected_identity=account.identity)
                if not isinstance(code_result, dict) or self.engine.live_identity() != account.identity:
                    raise ClaudeDesktopError('Claude Code did not confirm the selected login. Check Code before retrying.')
                if not self.enabled():
                    raise ClaudeDesktopError('Desktop features were turned off before the profile opened.')
                desktop_result = {**app.open(desktop, code), 'profile': {'id': current.id, 'name': current.name},
                                  'app_version': info['version'], 'identity_verified': False}
                if self.engine.live_identity() != account.identity:
                    raise ClaudeDesktopError('Claude Code changed again while Desktop was opening. Check both apps before continuing.')
                return {'code_result': code_result, 'desktop_result': desktop_result,
                        'number': str(number), 'identity_verified': False}
        except Exception as exc:
            detail = str(exc) if isinstance(exc, ClaudeSwitchError) else 'The selection could not be completed. Check both apps before retrying.'
            if code_result is not None:
                detail = ('Claude Code selection completed, but Desktop selection was not confirmed. ' + detail
                          + ' No automatic rollback was attempted. Select the account again to retry.')
            raise DesktopSelectionError(detail, code_result=code_result, desktop_result=desktop_result) from exc
        finally:
            self._operation.release()

    def adapter(self):
        return LinkedClaudeEngine(self)


class LinkedClaudeEngine:
    """Preserve the normal rotation engine's policy, restricting its destinations."""
    def __init__(self, coordinator):
        self.coordinator = coordinator

    def __getattr__(self, name):
        return getattr(self.coordinator.engine, name)

    def switchable_account_numbers(self):
        if not self.coordinator.enabled():
            return []
        linked = {p.code_account for p in self.coordinator.profiles.list() if p.code_account is not None}
        return [str(n) for n in self.coordinator.engine.switchable_account_numbers()
                if self.coordinator.engine.account_kind_for(str(n)) != 'api_key'
                and self.coordinator.account(n) in linked]

    def switch_to(self, identifier, json_output=False, **kwargs):
        try:
            result = self.coordinator.select(identifier)
        except DesktopSelectionError as exc:
            self.coordinator.completed(None, exc)
            raise
        self.coordinator.completed(result, None)
        return result['code_result']
