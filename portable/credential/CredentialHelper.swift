import Foundation
import Security

private let service = "io.github.hirorylabo.codex-openrouter-desktop"
private let account = NSUserName()

private func baseQuery() -> [String: Any] {
    return [
        kSecClass as String: kSecClassGenericPassword,
        kSecAttrService as String: service,
        kSecAttrAccount as String: account,
    ]
}

private func fail(_ message: String, status: OSStatus? = nil) -> Never {
    if let status,
       let text = SecCopyErrorMessageString(status, nil) as String? {
        FileHandle.standardError.write(Data("credential helper: \(message): \(text)\n".utf8))
    } else {
        FileHandle.standardError.write(Data("credential helper: \(message)\n".utf8))
    }
    exit(1)
}

private func readSecret() -> Data {
    let raw = FileHandle.standardInput.readDataToEndOfFile()
    guard let value = String(data: raw, encoding: .utf8)?
        .trimmingCharacters(in: .whitespacesAndNewlines),
          value.hasPrefix("sk-or-"), value.count >= 32 else {
        fail("stdin does not contain a valid-looking OpenRouter API key")
    }
    return Data(value.utf8)
}

private func store() {
    let secret = readSecret()
    let query = baseQuery()
    let attributes: [String: Any] = [kSecValueData as String: secret]
    let updateStatus = SecItemUpdate(query as CFDictionary, attributes as CFDictionary)
    if updateStatus == errSecSuccess { return }
    if updateStatus != errSecItemNotFound {
        fail("cannot update the Keychain item", status: updateStatus)
    }
    var add = query
    add[kSecValueData as String] = secret
    add[kSecAttrLabel as String] = "Codex OpenRouter Desktop API key"
    add[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlock
    let addStatus = SecItemAdd(add as CFDictionary, nil)
    if addStatus != errSecSuccess {
        fail("cannot add the Keychain item", status: addStatus)
    }
}

private func get() {
    var query = baseQuery()
    query[kSecReturnData as String] = true
    query[kSecMatchLimit as String] = kSecMatchLimitOne
    var result: CFTypeRef?
    let status = SecItemCopyMatching(query as CFDictionary, &result)
    guard status == errSecSuccess, let data = result as? Data, !data.isEmpty else {
        fail("OpenRouter API key is not available", status: status)
    }
    FileHandle.standardOutput.write(data)
}

private func exists() -> Bool {
    var query = baseQuery()
    query[kSecMatchLimit as String] = kSecMatchLimitOne
    return SecItemCopyMatching(query as CFDictionary, nil) == errSecSuccess
}

private func remove() {
    let status = SecItemDelete(baseQuery() as CFDictionary)
    if status != errSecSuccess && status != errSecItemNotFound {
        fail("cannot delete the Keychain item", status: status)
    }
}

guard CommandLine.arguments.count == 2 else {
    fail("usage: codex-openrouter-credential get|store|status|delete")
}

switch CommandLine.arguments[1] {
case "get":
    get()
case "store":
    store()
case "status":
    exit(exists() ? 0 : 1)
case "delete":
    remove()
default:
    fail("unknown command")
}
