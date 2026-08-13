import AppKit

// top-level codeは main.swift にしか置けない。実体は LauncherApp にある。
let application = NSApplication.shared
let launcherDelegate = LauncherApp()
application.delegate = launcherDelegate
application.run()
