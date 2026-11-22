// CrashKiller Windows C++17 WinHTTP Crash Dump & Log Uploader Reference.
// Streams a .dmp minidump and optional log files via multipart/form-data
// to POST /api/v1/crashes/upload using X-API-Key authentication.

#ifdef _WIN32
#define NOMINMAX
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <winhttp.h>
#endif

#include <algorithm>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <sstream>
#include <string>
#include <vector>

namespace fs = std::filesystem;

namespace {

struct UploadConfig {
    std::wstring host = L"localhost";
    uint16_t port = 8080;
    bool use_https = false;
    std::wstring base_path = L"/api/v1/crashes/upload";
    std::string project_key;
    std::string api_key;
    std::string app_version;
    fs::path dump_path;
    std::vector<fs::path> log_paths;
};

#ifdef _WIN32

std::wstring ToWide(const std::string& s) {
    if (s.empty()) return L"";
    int len = MultiByteToWideChar(CP_UTF8, 0, s.c_str(), static_cast<int>(s.size()), nullptr, 0);
    std::wstring out(static_cast<size_t>(len), L'\0');
    MultiByteToWideChar(CP_UTF8, 0, s.c_str(), static_cast<int>(s.size()), &out[0], len);
    return out;
}

bool ParseServerUrl(const std::string& url, UploadConfig& cfg) {
    std::wstring wurl = ToWide(url);
    URL_COMPONENTS parts{};
    parts.dwStructSize = sizeof(parts);
    parts.dwHostNameLength = static_cast<DWORD>(-1);
    parts.dwUrlPathLength = static_cast<DWORD>(-1);
    if (!WinHttpCrackUrl(wurl.c_str(), static_cast<DWORD>(wurl.size()), 0, &parts)) {
        return false;
    }
    cfg.use_https = (parts.nScheme == INTERNET_SCHEME_HTTPS);
    cfg.port = parts.nPort;
    cfg.host.assign(parts.lpszHostName, parts.dwHostNameLength);
    std::wstring prefix;
    if (parts.lpszUrlPath && parts.dwUrlPathLength > 0) {
        prefix.assign(parts.lpszUrlPath, parts.dwUrlPathLength);
        while (!prefix.empty() && prefix.back() == L'/') {
            prefix.pop_back();
        }
    }
    cfg.base_path = prefix + L"/api/v1/crashes/upload";
    return true;
}

struct PartSource {
    std::string header;
    bool is_file = false;
    std::string inline_body;
    fs::path file_path;
    uint64_t file_size = 0;
};

bool UploadCrashWinHttp(const UploadConfig& cfg) {
    const std::string boundary = "----CrashKillerBoundary7f3a9d2c1b";
    std::vector<PartSource> parts;

    auto add_field = [&](const std::string& name, const std::string& value) {
        std::ostringstream oss;
        oss << "--" << boundary << "\r\n"
            << "Content-Disposition: form-data; name=\"" << name << "\"\r\n\r\n"
            << value << "\r\n";
        parts.push_back(PartSource{oss.str(), false, "", {}, 0});
    };

    auto add_file = [&](const std::string& field, const fs::path& path, const std::string& ctype) {
        std::error_code ec;
        uint64_t sz = fs::file_size(path, ec);
        if (ec) {
            std::cerr << "Failed to stat file: " << path << "\n";
            return false;
        }
        std::ostringstream oss;
        oss << "--" << boundary << "\r\n"
            << "Content-Disposition: form-data; name=\"" << field
            << "\"; filename=\"" << path.filename().string() << "\"\r\n"
            << "Content-Type: " << ctype << "\r\n\r\n";
        parts.push_back(PartSource{oss.str(), true, "", path, sz});
        return true;
    };

    add_field("project_key", cfg.project_key);
    add_field("app_version", cfg.app_version);
    if (!add_file("dump_file", cfg.dump_path, "application/octet-stream")) {
        return false;
    }
    for (const auto& lp : cfg.log_paths) {
        if (!add_file("log_files", lp, "text/plain")) {
            return false;
        }
    }

    const std::string closing_boundary = "--" + boundary + "--\r\n";
    uint64_t total_len = closing_boundary.size();
    for (const auto& p : parts) {
        total_len += p.header.size();
        if (p.is_file) {
            total_len += p.file_size + 2;  // trailing \r\n
        }
    }

    HINTERNET hSession = WinHttpOpen(
        L"CrashKillerUploader/1.0",
        WINHTTP_ACCESS_TYPE_DEFAULT_PROXY,
        WINHTTP_NO_PROXY_NAME,
        WINHTTP_NO_PROXY_BYPASS,
        0);
    if (!hSession) return false;

    HINTERNET hConnect = WinHttpConnect(hSession, cfg.host.c_str(), cfg.port, 0);
    if (!hConnect) {
        WinHttpCloseHandle(hSession);
        return false;
    }

    DWORD flags = cfg.use_https ? WINHTTP_FLAG_SECURE : 0;
    HINTERNET hRequest = WinHttpOpenRequest(
        hConnect,
        L"POST",
        cfg.base_path.c_str(),
        nullptr,
        WINHTTP_NO_REFERER,
        WINHTTP_DEFAULT_ACCEPT_TYPES,
        flags);
    if (!hRequest) {
        WinHttpCloseHandle(hConnect);
        WinHttpCloseHandle(hSession);
        return false;
    }

    std::wstring headers =
        L"Content-Type: multipart/form-data; boundary=" + ToWide(boundary) + L"\r\n" +
        L"X-API-Key: " + ToWide(cfg.api_key) + L"\r\n";
    WinHttpAddRequestHeaders(
        hRequest,
        headers.c_str(),
        static_cast<DWORD>(headers.size()),
        WINHTTP_ADDREQ_FLAG_ADD | WINHTTP_ADDREQ_FLAG_REPLACE);

    if (!WinHttpSendRequest(
            hRequest,
            WINHTTP_NO_ADDITIONAL_HEADERS,
            0,
            WINHTTP_NO_REQUEST_DATA,
            0,
            static_cast<DWORD>(total_len),
            0)) {
        WinHttpCloseHandle(hRequest);
        WinHttpCloseHandle(hConnect);
        WinHttpCloseHandle(hSession);
        return false;
    }

    auto write_all = [&](const char* buf, size_t len) -> bool {
        size_t offset = 0;
        while (offset < len) {
            DWORD written = 0;
            DWORD chunk = static_cast<DWORD>(std::min<size_t>(len - offset, 65536));
            if (!WinHttpWriteData(hRequest, buf + offset, chunk, &written)) {
                return false;
            }
            offset += written;
        }
        return true;
    };

    bool ok = true;
    std::vector<char> io_buf(65536);
    for (const auto& p : parts) {
        if (!write_all(p.header.data(), p.header.size())) {
            ok = false;
            break;
        }
        if (p.is_file) {
            std::ifstream in(p.file_path, std::ios::binary);
            if (!in) {
                ok = false;
                break;
            }
            while (in) {
                in.read(io_buf.data(), static_cast<std::streamsize>(io_buf.size()));
                std::streamsize n = in.gcount();
                if (n > 0 && !write_all(io_buf.data(), static_cast<size_t>(n))) {
                    ok = false;
                    break;
                }
            }
            if (!ok || !write_all("\r\n", 2)) {
                ok = false;
                break;
            }
        }
    }

    if (ok) {
        ok = write_all(closing_boundary.data(), closing_boundary.size());
    }

    if (ok && WinHttpReceiveResponse(hRequest, nullptr)) {
        DWORD status_code = 0;
        DWORD sz = sizeof(status_code);
        WinHttpQueryHeaders(
            hRequest,
            WINHTTP_QUERY_STATUS_CODE | WINHTTP_QUERY_FLAG_NUMBER,
            WINHTTP_HEADER_NAME_BY_INDEX,
            &status_code,
            &sz,
            WINHTTP_NO_HEADER_INDEX);

        std::string response_body;
        DWORD avail = 0;
        while (WinHttpQueryDataAvailable(hRequest, &avail) && avail > 0) {
            std::vector<char> rbuf(avail);
            DWORD read_bytes = 0;
            if (WinHttpReadData(hRequest, rbuf.data(), avail, &read_bytes) && read_bytes > 0) {
                response_body.append(rbuf.data(), read_bytes);
            }
        }
        std::cout << "HTTP " << status_code << ": " << response_body << "\n";
        ok = (status_code >= 200 && status_code < 300);
    } else {
        ok = false;
    }

    WinHttpCloseHandle(hRequest);
    WinHttpCloseHandle(hConnect);
    WinHttpCloseHandle(hSession);
    return ok;
}

#endif

}  // namespace

int main(int argc, char* argv[]) {
    if (argc < 6) {
        std::cerr << "Usage: crashkiller_uploader <server_url> <project_key> <api_key> "
                  << "<app_version> <dump_path> [log_path ...]\n";
        return 2;
    }

#ifdef _WIN32
    UploadConfig cfg;
    if (!ParseServerUrl(argv[1], cfg)) {
        std::cerr << "Invalid server URL: " << argv[1] << "\n";
        return 2;
    }
    cfg.project_key = argv[2];
    cfg.api_key = argv[3];
    cfg.app_version = argv[4];
    cfg.dump_path = argv[5];
    for (int i = 6; i < argc; ++i) {
        cfg.log_paths.emplace_back(argv[i]);
    }
    return UploadCrashWinHttp(cfg) ? 0 : 1;
#else
    (void)argv;
    std::cerr << "WinHTTP uploader is Windows-only. On POSIX/CI use cli/crashkiller_cli.py.\n";
    return 1;
#endif
}
