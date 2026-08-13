import AppKit

// CLIがstdinを読む前に死ぬと、payloadの書き込みがSIGPIPEでこのプロセスごと
// 落ちる。エラーはalertで見せたいので、ここで無視してwrite側のエラーにする。
signal(SIGPIPE, SIG_IGN)

// top-level codeは main.swift にしか置けない。実体は LauncherApp にある。
let application = NSApplication.shared
let launcherDelegate = LauncherApp()
application.delegate = launcherDelegate
application.run()
