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
from urllib.parse import urlsplit

from openswap.claude_desktop import APP_PATH, BUNDLE_ID, ClaudeDesktopError
from openswap.claude_desktop_signin import _fourcc, signin_link

OWNER_IDS = {"com.opensoft.openswap", "com.opensoft.openswap.dev"}
PREVIOUS_KEY = "ClaudeLinksPreviousApplication"
PAUSED_KEY = "ClaudeLinksPermissionPaused"
MAX_AGE = 120


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
    """Keep the registered copy alive to receive a cold-start URL event."""
    try:
        from AppKit import NSWorkspace
        from Foundation import NSBundle, NSURL
        bundle = NSBundle.mainBundle()
        target = NSWorkspace.sharedWorkspace().URLForApplicationToOpenURL_(NSURL.URLWithString_("claude://"))
        return (bundle.bundleIdentifier() in OWNER_IDS and target is not None
                and Path(target.path()).resolve() == Path(bundle.bundlePath()).resolve())
    except Exception:
        return False


def restore_for_uninstall():
    """Undo only this packaged copy's registration before it is removed."""
    if not owns_system_handler():
        return False
    from Foundation import NSDate, NSRunLoop
    native = MacOSLinks(lambda _value: None, lambda: None)
    result = []
    try:
        native.set_handler(native.previous(), result.append)
        deadline = time.monotonic() + 10
        while not result and time.monotonic() < deadline:
            NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.05))
        if not result or not result[0] or native.current() == native.application:
            raise ValueError
    except Exception:
        raise ClaudeDesktopError("Could not restore Claude links. Open Claude once to restore its link handling.") from None
    return True


def claude_link(value):
    """Bound all external input, including ordinary links passed through."""
    if not isinstance(value, str) or not 0 < len(value) <= 16384:
        return False
    if any(ord(c) <= 32 or ord(c) >= 127 or c == "\\" for c in value):
        return False
    try:
        return urlsplit(value).scheme == "claude"
    except ValueError:
        return False


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
                        self.receive(event.paramDescriptorForKeyword_(_fourcc("----")).stringValue())
                    except Exception:
                        pass  # never allow native exceptions to print a credential

                def applicationLaunched_(self, note):
                    app = note.userInfo().get(NSWorkspaceApplicationKey)
                    if app is not None and app.bundleIdentifier() == BUNDLE_ID:
                        self.launched()

        self.receiver = _Receiver.alloc().init()
        self.receiver.receive, self.receiver.launched = receive, launched
        NSAppleEventManager.sharedAppleEventManager().setEventHandler_andSelector_forEventClass_andEventID_(
            self.receiver, "handleEvent:withReplyEvent:", _fourcc("GURL"), _fourcc("GURL"))
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
        register_application(path)
        candidates = self.workspace.URLsForApplicationsToOpenURL_(NSURL.URLWithString_("claude://")) or []
        if Path(path).resolve() not in {Path(url.path()).resolve() for url in candidates}:
            # Launch Services records temporary bundles but excludes them from
            # URL binding. LSRegisterURL can return success in that case.
            raise ClaudeDesktopError("Move OpenSwap to Applications, reopen it, then choose Reconnect sign-in in the profile menu.")
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
        self.native = native_factory(self.receive, self.launched)

    def receive(self, value):
        if not claude_link(value):
            return
        try:
            self.events.put_nowait((self.clock(), value))
        except queue.Full:
            pass

    def launched(self):
        # Claude registers its scheme during startup. Wait for that registration
        # before requesting ownership, including when a Dock shortcut starts it.
        if self.enabled() and self.needed() and not self.stopping:
            self.prepare_at = self.clock() + 2

    def prepare(self, *, retry=False):
        self.stopping = False
        if retry:
            self.native.pause(False)
        self.launched()

    def _claim(self):
        if not self.enabled() or not self.needed() or self.stopping or self.native.paused():
            return
        current = self.native.current()
        if current == self.native.application:
            return
        self.native.remember(current)
        # Denial, a crash during the OS prompt, or an uncertain result pauses
        # subsequent requests. Only an explicit Reconnect action retries it.
        self.native.pause(True)
        self.changing = True
        try:
            self.native.set_handler(self.native.application, lambda ok: self.completions.put(("claim", ok)))
        except ClaudeDesktopError as exc:
            self.changing = False
            self.show_error(str(exc))
        except Exception:
            self.completions.put(("claim", False))

    def restore(self, completed=None):
        self.stopping = True
        self.prepare_at = None
        while not self.events.empty():
            try:
                self.events.get_nowait()
            except queue.Empty:
                break
        if completed:
            self.restore_callbacks.append(completed)
        if self.changing:
            return  # a pending grant must be undone before quitting/disabling
        try:
            if self.native.current() == self.native.application:
                self.changing = True
                self.native.set_handler(self.native.previous(), lambda ok: self.completions.put(("restore", ok)))
                return
        except Exception:
            self.show_error("Could not restore Claude links. Open Claude once before signing in again.")
        self._restored()

    def _restored(self):
        callbacks, self.restore_callbacks = self.restore_callbacks, []
        for callback in callbacks:
            callback()

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
                self.native.pause(not ok)
                if self.stopping or not self.enabled():
                    self.restore()
                elif not ok:
                    self.show_error("Automatic sign-in is paused. Choose Reconnect sign-in in the profile menu to allow OpenSwap to open Claude links.")
            else:
                if not ok:
                    self.show_error("Could not restore Claude links. Open Claude once before signing in again.")
                self._restored()
        if self.prepare_at is not None and self.clock() >= self.prepare_at and not self.changing:
            self.prepare_at = None
            try:
                self._claim()
            except Exception:
                self.show_error("Could not set up automatic sign-in. Choose Reconnect sign-in in the profile menu to retry.")
        # Discard old queued credentials even while another operation is busy.
        live = []
        for _ in range(8):
            try:
                item = self.events.get_nowait()
            except queue.Empty:
                break
            if self.clock() - item[0] <= MAX_AGE:
                live.append(item)
        for item in live:
            try:
                self.events.put_nowait(item)
            except queue.Full:
                break
        if not idle or self.changing or self.dispatching:
            return
        try:
            received, value = self.events.get_nowait()
        except queue.Empty:
            return
        now = self.clock()
        self.seen = {key: at for key, at in self.seen.items() if now - at < MAX_AGE}
        digest = hashlib.sha256(value.encode("ascii")).digest()
        if now - received > MAX_AGE or digest in self.seen:
            return
        self.seen[digest] = now
        try:
            # An unrecognized auth route is never sent to an arbitrary profile.
            parsed = urlsplit(value)
            try:
                signin_link(value)
            except ClaudeDesktopError:
                if any(part in (parsed.netloc + parsed.path).lower() for part in ("auth", "login", "callback", "magic-link")):
                    self.show_error("This Claude sign-in link is not supported. Restart sign-in from the intended Claude profile.")
                else:
                    self.native.forward(value)
                return
            if not self.enabled() or self.stopping:
                self.show_error("Open the Claude Desktop tab and restart sign-in in the intended profile.")
                return
            self.dispatching = True
            try:
                self.receive_signin(value, lambda: self.clock() - received <= MAX_AGE)
            finally:
                self.dispatching = False
        except Exception:
            self.show_error("Could not finish Claude sign-in. Restart sign-in in the intended profile.")
