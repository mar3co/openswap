use framework "Foundation"
use framework "AppKit"
use scripting additions

property targetApplication : missing value
property closingAutomatically : false
property openingProfile : false
property focusPending : false

on run
    set targetApplication to missing value
    set closingAutomatically to false
    set openingProfile to false
    set focusPending to false
    set attachOnly to (current application's NSProcessInfo's processInfo()'s arguments()'s containsObject:"--openswap-attach") as boolean
    my openProfile(attachOnly, not attachOnly)
end run

on reopen
    -- A queued click must not relaunch a session that just quit or crashed.
    -- A fresh launch of this app opens it again through the run handler.
    my openProfile(true, true)
end reopen

on openProfile(attachOnly, focusRequested)
    if openingProfile then return
    set openingProfile to true
    try
        if attachOnly then
            set launchCommand to "ATTACH_COMMAND"
        else
            set launchCommand to "OPEN_COMMAND"
        end if
        with timeout of 120 seconds
            set replyText to do shell script launchCommand
        end timeout
        set replyData to (current application's NSString's stringWithString:replyText)'s dataUsingEncoding:(current application's NSUTF8StringEncoding)
        set replyObject to current application's NSJSONSerialization's JSONObjectWithData:replyData options:0 |error|:(missing value)
        set targetPID to (replyObject's objectForKey:"pid") as integer
        -- Electron can have a PID before it registers as a running Mac app.
        -- Wait for that same process; never fall back to another Claude window.
        set candidate to missing value
        repeat 100 times
            if candidate is missing value then set candidate to current application's NSRunningApplication's runningApplicationWithProcessIdentifier:targetPID
            if candidate is not missing value then
                if (candidate's isTerminated()) as boolean then exit repeat
                if (candidate's isFinishedLaunching()) as boolean then exit repeat
            end if
            delay 0.1
        end repeat
        if candidate is missing value then error "The Claude session has closed. Open the profile again."
        if (candidate's isTerminated()) as boolean then error "The Claude session has closed. Open the profile again."
        if not ((candidate's isFinishedLaunching()) as boolean) then error "Claude is still starting. Click this profile again in a moment."
        if (candidate's bundleIdentifier() as text) is not "TARGET_BUNDLE" then error "Could not verify the Claude session."
        if (candidate's bundleURL()'s |path|() as text) is not "TARGET_PATH" then error "Could not verify the Claude session."
        set targetApplication to candidate
        if focusRequested then
            set focusPending to true
            targetApplication's activateWithOptions:2
        end if
    on error messageText number errorNumber
        if not attachOnly then display alert "Could not open Claude profile" message messageText as warning
        if not my sessionRunning() then
            set closingAutomatically to true
            set openingProfile to false
            quit
            return
        end if
    end try
    set openingProfile to false
end openProfile

on sessionRunning()
    if targetApplication is missing value then return false
    return not ((targetApplication's isTerminated()) as boolean)
end sessionRunning

on idle
    if not openingProfile and not my sessionRunning() then
        set closingAutomatically to true
        quit
    end if
    if focusPending and not openingProfile then
        set focusPending to false
        -- Launch Services may activate the applet after its reopen handler.
        -- Hand focus back once it settles, but never take it from a third app.
        if my sessionRunning() then
            set frontApp to current application's NSWorkspace's sharedWorkspace()'s frontmostApplication()
            set thisApp to current application's NSRunningApplication's currentApplication()
            if frontApp is not missing value then
                if (frontApp's processIdentifier()) = (thisApp's processIdentifier()) then targetApplication's activateWithOptions:2
            end if
        end if
    end if
    return 1
end idle

on quit
    if closingAutomatically or not my sessionRunning() then
        set targetApplication to missing value
        continue quit
    else
        -- A normal quit lets Claude handle unsaved work. Never force it closed.
        set accepted to targetApplication's terminate()
        if not (accepted as boolean) then display alert "Could not quit this Claude profile" message "Quit it from its Claude window. Other profiles will stay open." as warning
    end if
end quit
