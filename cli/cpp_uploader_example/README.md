# CrashKiller Windows C++ Uploader Reference

Reference C++17 Windows client using `WinHTTP` (`winhttp.lib`) to stream `.dmp` minidumps and optional crash logs via `multipart/form-data` to `POST /api/v1/crashes/upload` with `X-API-Key` authentication.

## Build on Windows (CMake + MSVC)

```powershell
cmake -S . -B build -A x64
cmake --build build --config Release
```

## Usage

```powershell
.\build\Release\crashkiller_uploader.exe `
  http://localhost:8080 `
  demo-game `
  ck_your_project_api_key `
  1.4.2 `
  C:\crashes\crash.dmp `
  C:\crashes\game.log
```

## WinDbg Symbol Server Integration

Configure WinDbg or Visual Studio Symbol Settings to fetch archived PDBs/PEs from CrashKiller's `/symsrv` endpoint:

```text
SRV*C:\symcache*http://localhost:8080/symsrv
```
