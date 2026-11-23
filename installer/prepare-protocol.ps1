param([string]$Archive, [string]$ExpectedHash, [string]$Stage)
$ErrorActionPreference = 'Stop'
# Keep the same archive locked for reading during verification and extraction.
$stream = [IO.File]::OpenRead($Archive)
try {
    $sha = [Security.Cryptography.SHA256]::Create()
    try { $digest = [BitConverter]::ToString($sha.ComputeHash($stream)).Replace('-', '') }
    finally { $sha.Dispose() }
    if ($digest -ne $ExpectedHash) { throw 'Invalid component SHA-256.' }
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [IO.Compression.ZipFile]::ExtractToDirectory($Archive, $Stage)
    foreach ($name in 'node.exe','index.js','wrapper.node','napcat/napcat.mjs') {
        if (!(Test-Path -LiteralPath (Join-Path $Stage $name) -PathType Leaf)) { throw "Missing component: $name" }
    }
} finally { $stream.Dispose() }
