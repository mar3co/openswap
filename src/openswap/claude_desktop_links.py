"""macOS browser returns for isolated Claude Desktop instances.

When Claude isn't the system claude:// handler, its Google login uses
ASWebAuthenticationSession (verified in Claude 3.6556.2). macOS then delivers
that session's callback to its originating process. Unbound external links
cannot establish an originating profile: ask once, never guess or broadcast.
"""
from __future__ import annotations

import hashlib
import os
import queue
import time
from pathlib import Path
from urllib.parse import unquote, urlsplit

from openswap.claude_desktop import APP_PATH, BUNDLE_ID, ClaudeDesktopError
from openswap.claude_desktop_signin import callback_link, fourcc, link_text

OWNER_IDS = {"com.opensoft.openswap", "com.opensoft.openswap.dev"}
PREVIOUS_KEY = "ClaudeLinksPreviousApplication"
PAUSED_KEY = "ClaudeLinksPermissionPaused"
MAX_AGE = 120
# How long Quit waits for macOS to answer a handler change before leaving the
# consent sheet to the user, and how often idle ownership is reconsidered.
RESTORE_TIMEOUT = 10
RECHECK = 5
# Long enough after a Claude launch for a slow cold start to register its scheme.
OWNERSHIP_CHECK = 30
RESTORE_FAILED = "Could not restore Claude links. Open Claude once before signing in again."
RESTORE_REINSTALL = "Could not restore Claude links. Open Claude once to restore its link handling, or reinstall Claude."
OWNERSHIP_LOST = "Claude links no longer open in OpenSwap, so a sign-in may open the wrong profile. Choose Reconnect sign-in in the profile menu to fix it."
PAUSED = "Automatic sign-in is paused. Choose Reconnect sign-in in the profile menu to allow OpenSwap to open Claude links."
SETUP_FAILED = "Could not set up automatic sign-in. Choose Reconnect sign-in in the profile menu to retry."


def register_application(path):
    """Publish this bundle's URL claims before asking macOS to select it.

    Executing Contents/MacOS/OpenSwap directly (DEV, CLI and some login items)
    does not register the bundle with Launch Services. NSWorkspace's default
    handler request can otherwise fail without ever presenting consent.
    Registration makes the app a candidate; it does not change user defaults.
    """
    import ctypes as c

    cf = c.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    ls = c.CDLL("/System/Library/Frameworks/CoreServices.framework/CoreServices")
    create = cf.CFURLCreateFromFileSystemRepresentation
    create.argtypes = [c.c_void_p, c.c_char_p, c.c_long, c.c_bool]
    create.restype = c.c_void_p
    release = cf.CFRelease
    release.argtypes, release.restype = [c.c_void_p], None
    register = ls.LSRegisterURL
    register.argtypes, register.restype = [c.c_void_p, c.c_bool], c.c_int32
    raw = os.fsencode(path)
    url = create(None, raw, len(raw), True)
    if not url:
        raise ClaudeDesktopError("macOS could not register OpenSwap for sign-in. Reopen OpenSwap and try again.")
    try:
        if register(url, True) != 0:
            raise ClaudeDesktopError("macOS could not register OpenSwap for sign-in. Reopen OpenSwap and try again.")
    finally:
        release(url)


def owns_system_handler():
    """Whether the system claude:// handler is this install's OpenSwap.app.

    Decided by the bundle this process runs from, not the process's main
    bundle, so the CLI and the app agree. The registered copy must stay alive
    to receive a cold-start URL event.
    """
    from openswap import bundle
    app = bundle.app_path()
    if app is None:
        return False
    try:
        from AppKit import NSWorkspace
        from Foundation import NSURL
        target = NSWorkspace.sharedWorkspace().URLForApplicationToOpenURL_(NSURL.URLWithString_("claude://"))
        return target is not None and Path(target.path()).resolve() == app.resolve()
    except Exception:
        return False


def restore_for_uninstall():
    """Undo only this install's registration before it is removed.

    False when there is nothing to undo, including when the handler to return
    links to is gone. Raises with the remedy when macOS refuses or never answers.
    """
    if not owns_system_handler():
        return False
    from Foundation import NSDate, NSRunLoop
    native = MacOSLinks(lambda _value: None, lambda: None)
    previous = native.previous()
    if not previous.is_dir():
        return False
    result = []
    try:
        native.set_handler(previous, result.append)
        deadline = time.monotonic() + RESTORE_TIMEOUT
        while not result and time.monotonic() < deadline:
            NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.05))
        if not result or not result[0] or native.current() == native.application:
            raise ValueError
    except ClaudeDesktopError:
        raise
    except Exception:
        raise ClaudeDesktopError("Could not restore Claude links. Open Claude once to restore its link handling.") from None
    return True


def claude_link(value):
    """Bound all external input, including ordinary links passed through."""
    if link_text(value, strip=False) is None:
        return False
    try:
        return urlsplit(value).scheme == "claude"
    except ValueError:
        return False


def ordinary_link(value):
    """Neither a recognized callback nor an unknown auth route, which is never forwarded."""
    try:
        callback_link(value)
    except ClaudeDesktopError:
        parsed = urlsplit(value)
        route = unquote(parsed.netloc + parsed.path).lower()
        return not any(part in route for part in ("auth", "login", "callback", "magic-link"))
    return False


def ownership_lost(*, held, ended, changing, needed, current, application):
    """True when a held handler was taken over while profiles still need it,
    False when there is nothing to report, None when the check cannot run yet.

    Only reports: claiming again could show the consent sheet on every launch.
    """
    if ended:
        return False
    if changing:
        return None
    if not held or needed is False:
        return False
    if needed is None:
        return None
    try:
        owner = current()
    except Exception:
        return None
    return owner is not None and owner != application


def claim_step(*, current, application, held, reconnecting, paused, noticed):
    """What the post-launch check does: "held", "claim", "lost", "notice" or "paused".

    Once held in this process, only Reconnect sign-in may claim again. A
    pause never claims; it is explained once per run unless this run already
    told the user about it.
    """
    if current == application:
        return "held"
    if paused:
        return "paused" if noticed else "notice"
    if held and not reconnecting and current is not None:
        return "lost"
    return "claim"


class MacOSLinks:
    """Native plumbing; completion handlers only enqueue results, never show UI."""
    def __init__(self, receive, launched):
        from AppKit import NSWorkspace, NSWorkspaceDidLaunchApplicationNotification, NSWorkspaceApplicationKey
        from Foundation import NSAppleEventManager, NSBundle, NSObject, NSUserDefaults

        self.workspace = NSWorkspace.sharedWorkspace()
        bundle = NSBundle.mainBundle()
        if bundle.bundleIdentifier() not in OWNER_IDS:
            raise ClaudeDesktopError("Automatic sign-in needs the OpenSwap app. Open the installed app and try again.")
        self.application = Path(bundle.bundlePath()).resolve()
        self.defaults = NSUserDefaults.standardUserDefaults()

        # Define once per process; retaining the receiver also retains callbacks.
        global _Receiver
        if "_Receiver" not in globals():
            class _Receiver(NSObject):
                def handleEvent_withReplyEvent_(self, event, reply):
                    try:
                        self.receive(event.paramDescriptorForKeyword_(fourcc("----")).stringValue())
                    except Exception:
                        pass  # never allow native exceptions to print a credential

                def applicationLaunched_(self, note):
                    app = note.userInfo().get(NSWorkspaceApplicationKey)
                    if app is not None and app.bundleIdentifier() == BUNDLE_ID:
                        self.launched()

        self.receiver = _Receiver.alloc().init()
        self.receiver.receive, self.receiver.launched = receive, launched
        NSAppleEventManager.sharedAppleEventManager().setEventHandler_andSelector_forEventClass_andEventID_(
            self.receiver, "handleEvent:withReplyEvent:", fourcc("GURL"), fourcc("GURL"))
        self.workspace.notificationCenter().addObserver_selector_name_object_(
            self.receiver, "applicationLaunched:", NSWorkspaceDidLaunchApplicationNotification, None)

    def current(self):
        from Foundation import NSURL
        value = self.workspace.URLForApplicationToOpenURL_(NSURL.URLWithString_("claude://"))
        return Path(value.path()).resolve() if value else None

    def previous(self):
        from Foundation import NSBundle
        raw = self.defaults.stringForKey_(PREVIOUS_KEY)
        path = Path(raw) if raw and len(raw) < 4096 else APP_PATH
        bundle = NSBundle.bundleWithPath_(str(path)) if path.is_absolute() and path.suffix == ".app" else None
        # Never form a forwarding loop with another OpenSwap build.
        if bundle is None or bundle.bundleIdentifier() in OWNER_IDS:
            return APP_PATH
        return path.resolve()

    def remember(self, path):
        from Foundation import NSBundle
        bundle = NSBundle.bundleWithPath_(str(path)) if path else None
        if bundle is not None and bundle.bundleIdentifier() not in OWNER_IDS:
            self.defaults.setObject_forKey_(str(path), PREVIOUS_KEY)

    def paused(self):
        return self.defaults.boolForKey_(PAUSED_KEY)

    def pause(self, value):
        self.defaults.setBool_forKey_(value, PAUSED_KEY)

    def set_handler(self, path, completed):
        from Foundation import NSURL
        restoring = Path(path).resolve() != self.application
        try:
            register_application(path)
        except ClaudeDesktopError:
            if not restoring:
                raise
            raise ClaudeDesktopError(RESTORE_REINSTALL) from None
        candidates = self.workspace.URLsForApplicationsToOpenURL_(NSURL.URLWithString_("claude://")) or []
        if Path(path).resolve() not in {Path(url.path()).resolve() for url in candidates}:
            # Launch Services records temporary bundles but excludes them from
            # URL binding. LSRegisterURL can return success in that case.
            if not restoring:
                raise ClaudeDesktopError("Move OpenSwap to Applications, reopen it, then choose Reconnect sign-in in the profile menu.")
            raise ClaudeDesktopError(RESTORE_REINSTALL)
        self.workspace.setDefaultApplicationAtURL_toOpenURLsWithScheme_completionHandler_(
            NSURL.fileURLWithPath_(str(path)), "claude", lambda error: completed(error is None))

    def forward(self, value):
        from AppKit import NSWorkspaceOpenConfiguration
        from Foundation import NSURL
        # Explicit application: using the shared scheme again would recurse.
        self.workspace.openURLs_withApplicationAtURL_configuration_completionHandler_(
            [NSURL.URLWithString_(value)], NSURL.fileURLWithPath_(str(self.previous())),
            NSWorkspaceOpenConfiguration.configuration(), None)


class DesktopSignInLinks:
    """Main-thread coordinator. No login links leave bounded, expiring memory."""
    def __init__(self, *, enabled, receive_signin, show_error, needed=lambda: True,
                 native_factory=MacOSLinks, clock=time.monotonic):
        self.enabled, self.receive_signin, self.show_error = enabled, receive_signin, show_error
        self.needed = needed
        self.clock = clock
        self.events = queue.Queue(maxsize=8)
        self.completions = queue.SimpleQueue()
        self.seen = {}
        self.prepare_at = None
        self.changing = False
        self.stopping = False
        self.dispatching = False
        self.restore_callbacks = []
        self.restore_deadline = None
        # An explicit restore (Quit or disable) is waiting on the handler
        # change; a background handback fails quietly.
        self.explicit = False
        # Quit's restore has finished: no further request or alert this run.
        self.closed = False
        # Whether this app is the handler: unknown until checked, then tracked
        # through its own claims and restores.
        self.held = None
        # Handed back because no profile needed links; claims again once one does.
        self.released = False
        self.recheck_at = 0.0
        self.verify_at = None
        # Reconnect sign-in asked for the next claim; kept until a claim runs,
        # a pause cancels it, or a quit or disable clears it.
        self.reconnecting = False
        # This run told the user why automatic sign-in is paused.
        self.noticed = False
        self.native = native_factory(self.receive, self.launched)

    def receive(self, value):
        if not claude_link(value):
            return
        try:
            self.events.put_nowait((self.clock(), value))
        except queue.Full:
            pass

    def _needed(self):
        """True, False, or None when the store cannot say; only False releases."""
        try:
            return bool(self.needed())
        except Exception:
            return None

    def launched(self):
        # Claude registers its scheme during startup. Wait for that registration
        # before requesting ownership, including when a Dock shortcut starts it.
        if self.enabled() and self._needed() and not self.stopping:
            self.released = False
            self.prepare_at = self.clock() + 2
            self.verify_at = self.clock() + OWNERSHIP_CHECK

    def prepare(self, *, retry=False):
        self.stopping = self.closed = False
        if retry:
            self.native.pause(False)
            self.reconnecting = True
        self.launched()

    def _claim(self):
        if not self.enabled() or not self._needed() or self.stopping:
            return
        reconnecting, self.reconnecting = self.reconnecting, False
        paused = self.native.paused()
        current = self.native.current()
        step = claim_step(current=current, application=self.native.application, held=self.held,
                          reconnecting=reconnecting, paused=paused, noticed=self.noticed)
        if step == "held":
            self.held = True
        elif step == "lost":
            self._lost()
        elif step == "notice":
            self._notice(PAUSED)
        if step != "claim":
            return
        self.held = False
        self.native.remember(current)
        # Denial, a crash during the OS prompt, or an uncertain result pauses
        # subsequent requests. Only an explicit Reconnect action retries it.
        self.native.pause(True)
        self.changing = True
        try:
            self.native.set_handler(self.native.application, lambda ok: self.completions.put(("claim", ok)))
        except ClaudeDesktopError as exc:
            self.changing = False
            self._notice(str(exc))
        except Exception:
            self.completions.put(("claim", False))

    def restore(self, completed=None):
        """Quit or disable: hand the handler back; queued callbacks are dropped."""
        if self.closed:
            if completed:
                completed()
            return
        self.stopping = self.explicit = True
        self.prepare_at = None
        self.reconnecting = False
        self._sift(keep_callbacks=False)
        if completed:
            self.restore_callbacks.append(completed)
            self.restore_deadline = self.clock() + RESTORE_TIMEOUT
        self._hand_back()

    def _hand_back(self):
        if self.changing:
            return  # a pending grant must be undone before quitting/disabling
        try:
            if self.native.current() != self.native.application:
                self.held = False
            elif (previous := self.native.previous()).is_dir():
                self.changing = True
                self.native.set_handler(previous, lambda ok: self.completions.put(("restore", ok)))
                return
            # A previous handler that is gone leaves nothing to restore to.
        except ClaudeDesktopError as exc:
            self.changing = False
            self._restore_failed(str(exc))
        except Exception:
            self.changing = False
            self._restore_failed(RESTORE_FAILED)
        self._restored()

    def _restore_failed(self, message):
        if self.explicit:
            self.show_error(message)

    def _restored(self):
        self.restore_deadline = None
        self.explicit = False
        callbacks, self.restore_callbacks = self.restore_callbacks, []
        if callbacks:
            self.closed = True
        for callback in callbacks:
            callback()

    def _recheck(self):
        """Hand the handler back while no profile needs links; take it again when one does.

        A Reconnect that could not claim yet claims once the profile store
        answers, or says it could not.
        """
        if self.changing or self.stopping:
            return
        if self.reconnecting and self.prepare_at is None:
            needed = self._needed() if self.enabled() else False
            if needed is None:
                self.reconnecting = False
                self.show_error(SETUP_FAILED)
                return
            if needed:
                self.launched()
                return
        if self.held is False and not self.released:
            return
        needed = self._needed() if self.enabled() else False
        if needed is None:
            return
        if needed:
            if self.released:
                self.launched()
            return
        if self.released:
            return  # one attempt per release: never re-prompt on a refusal
        if self.held is None:
            try:
                self.held = self.native.current() == self.native.application
            except Exception:
                return
        if self.held:
            self.released = True
            self._hand_back()

    def _verify(self):
        """Tell the user once that the handler was taken over; Reconnect sign-in claims it again."""
        enabled = self.enabled()
        lost = ownership_lost(held=self.held, ended=self.stopping or self.released or not enabled,
                              changing=self.changing or self.reconnecting,
                              needed=self._needed() if enabled else False,
                              current=self.native.current, application=self.native.application)
        if lost is None:
            return
        self.verify_at = None
        if lost:
            self._lost()

    def _lost(self):
        self.held = False
        self.reconnecting = False
        self.native.pause(True)
        self._notice(OWNERSHIP_LOST)

    def _notice(self, message):
        self.noticed = True
        self.show_error(message)

    def _sift(self, *, keep_callbacks):
        """Forward ordinary links now; callbacks expire, or all go when not kept.

        Ordinary links are not credentials and never wait on a pending handler
        change, so a handback stalled on its consent sheet cannot age them out.
        """
        live = []
        for _ in range(8):
            try:
                item = self.events.get_nowait()
            except queue.Empty:
                break
            if self.clock() - item[0] > MAX_AGE:
                continue
            if ordinary_link(item[1]):
                try:
                    self.native.forward(item[1])
                except Exception:
                    self.show_error("Could not open the Claude link. Start again from the intended profile.")
            elif keep_callbacks:
                live.append(item)
        for item in live:
            try:
                self.events.put_nowait(item)
            except queue.Full:
                break

    def poll(self, *, idle=True):
        try:
            operation, ok = self.completions.get_nowait()
        except queue.Empty:
            pass
        else:
            self.changing = False
            if operation == "claim":
                try:
                    ok = ok and self.native.current() == self.native.application
                except Exception:
                    ok = False
                self.held = ok
                self.native.pause(not ok)
                if self.stopping or not self.enabled():
                    self.restore()
                elif not ok:
                    self._notice(PAUSED)
            else:
                if ok:
                    self.held = False
                else:
                    self._restore_failed(RESTORE_FAILED)
                self._restored()
        if self.restore_callbacks and self.restore_deadline is not None and self.clock() >= self.restore_deadline:
            # The consent sheet stays with the user; Quit cannot wait on it.
            self.changing = False
            self._restored()
        if self.clock() >= self.recheck_at:
            self.recheck_at = self.clock() + RECHECK
            self._recheck()
        if self.prepare_at is not None and self.clock() >= self.prepare_at and not self.changing:
            self.prepare_at = None
            try:
                self._claim()
            except Exception:
                self.show_error(SETUP_FAILED)
        if self.verify_at is not None and self.clock() >= self.verify_at:
            self._verify()
        # Discard old queued credentials even while another operation is busy.
        self._sift(keep_callbacks=True)
        if not idle or self.changing or self.dispatching:
            return
        try:
            received, value = self.events.get_nowait()
        except queue.Empty:
            return
        now = self.clock()
        try:
            # An unrecognized auth route is never sent to an arbitrary profile.
            try:
                callback_link(value)
            except ClaudeDesktopError:
                self.show_error("This Claude link is not supported. Start again from the intended Claude profile.")
                return
            # One-time callbacks are deduplicated; ordinary links were forwarded by the sift.
            self.seen = {key: at for key, at in self.seen.items() if now - at < MAX_AGE}
            digest = hashlib.sha256(value.encode("ascii")).digest()
            if digest in self.seen:
                return
            self.seen[digest] = now
            if not self.enabled() or self.stopping:
                self.show_error("Open the Claude Desktop tab and start again from the intended profile.")
                return
            self.dispatching = True
            try:
                self.receive_signin(value, lambda: self.clock() - received <= MAX_AGE)
            finally:
                self.dispatching = False
        except Exception:
            self.show_error("Could not open the Claude link. Start again from the intended profile.")
