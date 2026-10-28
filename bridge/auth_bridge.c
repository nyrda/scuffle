/* Windowless DPAPI and game-start bridge for the native Linux launcher. */
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <wincrypt.h>
#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include <fcntl.h>
#include <io.h>
#include <time.h>
#include "device_id.h"

static BYTE entropy_bytes[16] = {
    0xc8, 0x76, 0xf4, 0xae, 0x4c, 0x95, 0x2e, 0xfe,
    0xf2, 0xfa, 0x0f, 0x54, 0x19, 0xc0, 0x9c, 0x43
};

/* Append one argument to a Windows command line. Waystone passes only plain
 * launch switches, so an argument containing a quote is rejected. */
static int append_argument(WCHAR *command, size_t capacity, const char *argument) {
    WCHAR wide[1024];
    if (!MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, argument, -1, wide, 1024)) return 0;
    if (wcschr(wide, L'"')) return 0;
    size_t used = wcslen(command);
    int quoted = wcschr(wide, L' ') != NULL;
    int written = swprintf(command + used, capacity - used, quoted ? L" \"%ls\"" : L" %ls", wide);
    return written >= 0;
}

/* Commit the protected game credential from token_file to the launch
 * registry key, start the game, acknowledge it in ack_file, and wait for it so
 * that UMU keeps tracking the game's runtime. */
static int launch_protected(const char *token_file, const char *ack_file, const char *executable,
                            int argument_count, char **arguments) {
    BYTE buffer[8192];
    DWORD size;
    HKEY key = NULL;
    DATA_BLOB plain = {0}, entropy = {16, entropy_bytes};
    int result = 1;
    FILE *file = fopen(token_file, "rb");
    if (!file) goto done;
    size = (DWORD)fread(buffer, 1, sizeof(buffer), file);
    int extra = fgetc(file) != EOF;
    fclose(file);
    if (!size || extra) goto done;
    DATA_BLOB protected = {size, buffer};
    if (!CryptUnprotectData(&protected, NULL, &entropy, NULL, NULL,
                           CRYPTPROTECT_UI_FORBIDDEN, &plain)) goto done;
    if (!plain.cbData || plain.cbData > 2048) goto done;
    for (DWORD i = 0; i < plain.cbData; i++)
        if (plain.pbData[i] < 32 || plain.pbData[i] >= 127) goto done;
    if (RegCreateKeyExA(HKEY_CURRENT_USER,
        "Software\\Blizzard Entertainment\\Battle.net\\Launch Options\\WoW", 0, NULL,
        0, KEY_QUERY_VALUE | KEY_SET_VALUE, NULL, &key, NULL)) goto done;
    /* Preserve the selected WoW account, region and locale. Commit only the
     * freshly issued game credential and launch timestamp. */
    if (RegSetValueExA(key, "WEB_TOKEN", 0, REG_BINARY, buffer, size)) goto done;
    char timestamp[32];
    snprintf(timestamp, sizeof(timestamp), "%lld", (long long)time(NULL));
    if (RegSetValueExA(key, "ACCOUNT_TS", 0, REG_SZ, (BYTE *)timestamp,
                      (DWORD)strlen(timestamp) + 1)) goto done;
    if (RegFlushKey(key)) goto done;
    WCHAR game_path[32768], directory[32768], command[32768];
    if (!MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS,
        executable, -1, game_path, 32000)) goto done;
    wcscpy(directory, game_path);
    WCHAR *separator = wcsrchr(directory, L'\\');
    if (!separator) goto done;
    *separator = 0;
    if (swprintf(command, 32768, L"\"%ls\"", game_path) < 0) goto done;
    for (int i = 0; i < argument_count; i++)
        if (!append_argument(command, 32768, arguments[i])) goto done;
    STARTUPINFOW startup = {.cb = sizeof(startup)};
    PROCESS_INFORMATION child = {0};
    SECURITY_ATTRIBUTES security = {.nLength = sizeof(security), .bInheritHandle = TRUE};
    HANDLE null_io = CreateFileW(L"NUL", GENERIC_READ | GENERIC_WRITE,
        FILE_SHARE_READ | FILE_SHARE_WRITE, &security, OPEN_EXISTING, 0, NULL);
    if (null_io == INVALID_HANDLE_VALUE) goto done;
    startup.dwFlags = STARTF_USESTDHANDLES;
    startup.hStdInput = startup.hStdOutput = startup.hStdError = null_io;
    SetHandleInformation(GetStdHandle(STD_INPUT_HANDLE), HANDLE_FLAG_INHERIT, 0);
    SetHandleInformation(GetStdHandle(STD_OUTPUT_HANDLE), HANDLE_FLAG_INHERIT, 0);
    SetHandleInformation(GetStdHandle(STD_ERROR_HANDLE), HANDLE_FLAG_INHERIT, 0);
    if (!CreateProcessW(game_path, command,
        NULL, NULL, TRUE, DETACHED_PROCESS, NULL, directory,
        &startup, &child)) { CloseHandle(null_io); goto done; }
    CloseHandle(null_io);
    file = fopen(ack_file, "wbx");
    if (file) {
        fprintf(file, "{\"game_started\":true,\"windows_pid\":%lu}\n", child.dwProcessId);
        fclose(file);
    }
    /* Release all decrypted data and registry handles before waiting. */
    RegCloseKey(key); key = NULL;
    SecureZeroMemory(plain.pbData, plain.cbData); LocalFree(plain.pbData);
    plain.pbData = NULL;
    SecureZeroMemory(buffer, sizeof(buffer));
    WaitForSingleObject(child.hProcess, INFINITE);
    CloseHandle(child.hThread);
    CloseHandle(child.hProcess);
    result = 0;
done:
    if (key) RegCloseKey(key);
    if (plain.pbData) { SecureZeroMemory(plain.pbData, plain.cbData); LocalFree(plain.pbData); }
    SecureZeroMemory(buffer, sizeof(buffer));
    if (result) fputs("Game credential handoff or process creation failed.\n", stderr);
    return result;
}

/* Protect or unprotect one length-prefixed stdin value and write it as a
 * WBC1 frame. Plaintext is at most 2048 bytes, protected data 8192. */
static int pipe_dpapi(int protect) {
    BYTE buffer[8192];
    DWORD size, input_limit = protect ? 2048 : 8192, output_limit = protect ? 8192 : 2048;
    DATA_BLOB output = {0}, entropy = {16, entropy_bytes};
    int result = 1;
    _setmode(_fileno(stdin), _O_BINARY);
    _setmode(_fileno(stdout), _O_BINARY);
    if (fread(&size, 1, 4, stdin) != 4 || !size || size > input_limit) goto done;
    if (fread(buffer, 1, size, stdin) != size) goto done;
    DATA_BLOB input = {size, buffer};
    BOOL ok = protect
        ? CryptProtectData(&input, NULL, &entropy, NULL, NULL, CRYPTPROTECT_UI_FORBIDDEN, &output)
        : CryptUnprotectData(&input, NULL, &entropy, NULL, NULL, CRYPTPROTECT_UI_FORBIDDEN, &output);
    if (!ok || !output.cbData || output.cbData > output_limit) goto done;
    if (fwrite("WBC1", 1, 4, stdout) != 4 ||
        fwrite(&output.cbData, 1, 4, stdout) != 4 ||
        fwrite(output.pbData, 1, output.cbData, stdout) != output.cbData) goto done;
    fflush(stdout);
    result = 0;
done:
    if (output.pbData) { SecureZeroMemory(output.pbData, output.cbData); LocalFree(output.pbData); }
    SecureZeroMemory(buffer, sizeof(buffer));
    return result;
}

static int opted_in(void) {
    const char *value = getenv("WOW_BROKER_PIPE");
    return value && strcmp(value, "1") == 0;
}

int main(int argc, char **argv) {
    /* Refuse accidental invocation outside the launcher's captured pipes. */
    if (!opted_in()) return 2;
    /* --launch-protected-file <token> <ack> <executable> [game arguments...] */
    if (argc >= 5 && strcmp(argv[1], "--launch-protected-file") == 0)
        return launch_protected(argv[2], argv[3], argv[4], argc - 5, argv + 5);
    if (argc == 2 && strcmp(argv[1], "--protect-auth-stdin") == 0) return pipe_dpapi(1);
    if (argc == 2 && strcmp(argv[1], "--pipe-device") == 0) return pipe_device();
    if (argc == 2 && strcmp(argv[1], "--unprotect-auth-stdin") == 0) return pipe_dpapi(0);
    return 2;
}
