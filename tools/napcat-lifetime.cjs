// The desktop bridge owns the write end. This also closes the single-process
// portable runtime if the bridge exits unexpectedly during login or an update.
process.stdin.on('end', () => process.exit(0))
process.stdin.resume()
