#ifndef BOFFIN_SYSTEM_LINKER_HELPERS_H
#define BOFFIN_SYSTEM_LINKER_HELPERS_H

/*
 * system_linker_helpers.h
 * =======================
 *
 * Shared analysis helpers for Android's "system_linker_exec" workaround -
 * the same technique Termux's own termux-exec uses to work around Android
 * 10+'s W^X restriction (targetSdkVersion >= 29 apps cannot execve() a
 * file inside their own writable data directory; see
 * https://developer.android.com/about/versions/10/behavior-changes-all#execute-permission).
 *
 * THE TRICK: execve() the *system's own dynamic linker*
 * (/system/bin/linker64, which lives on a read-only system partition and
 * is therefore exempt from the restriction) and pass the real target
 * binary as its first argument. The linker supports being invoked this
 * way directly (it's exactly what the kernel does implicitly via a
 * binary's PT_INTERP entry, just made explicit) and loads/runs the target
 * ELF itself - so the *file the kernel actually execve()s* is always the
 * system linker, never anything under our own writable data directory.
 *
 * HONEST, IMPORTANT LIMITATION (confirmed via Termux's own GitHub issues):
 * this ONLY works for dynamically-linked (ET_DYN / PIE) ELF binaries,
 * which is what Termux's own official package binaries are built as.
 * It does NOT work for statically-linked executables (ET_EXEC) - the
 * linker errors out trying to load one ("has unexpected e_type"). Our
 * bundled BusyBox binary (see BusyboxManager in main.py) is statically
 * linked, so it CANNOT be exec'd via this mechanism - see the ET_EXEC
 * branch below, which fails clearly with ENOEXEC and a log line rather
 * than silently doing nothing.
 *
 * This header only contains pure analysis/construction functions - it
 * never calls execve() itself. Each caller (pty_core.cpp for the initial
 * bash spawn; exec_shim.cpp for every exec bash performs afterward) wires
 * the result up to whichever underlying "real" execve it has access to,
 * since that differs between the two contexts (see exec_shim.cpp's
 * dlsym(RTLD_NEXT, ...) vs pty_core.cpp's direct libc call).
 */

#include <android/log.h>
#include <cerrno>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fcntl.h>
#include <unistd.h>

#define BOFFIN_LOG_TAG "BoffinSystemLinkerExec"
#define BOFFIN_LOGD(...) __android_log_print(ANDROID_LOG_DEBUG, BOFFIN_LOG_TAG, __VA_ARGS__)
#define BOFFIN_LOGE(...) __android_log_print(ANDROID_LOG_ERROR, BOFFIN_LOG_TAG, __VA_ARGS__)

namespace boffin_exec {

// ELF e_ident[EI_CLASS] values.
constexpr int kElfClass32 = 1;
constexpr int kElfClass64 = 2;

// ELF e_type values we care about.
constexpr int kEtExec = 2;  // static / non-PIE - NOT loadable via this trick
constexpr int kEtDyn = 3;   // PIE / shared object - has PT_INTERP, loadable

// Returns true if `path` starts with `prefix` (both assumed non-null,
// non-empty, absolute paths). Only paths under our own app's writable
// data directory need the redirect - everything else (e.g. /system/bin/sh)
// execs normally and should be passed straight through untouched.
inline bool needs_redirect(const char* path, const char* prefix) {
    if (!path || !prefix || !*prefix) return false;
    size_t plen = strlen(prefix);
    return strncmp(path, prefix, plen) == 0;
}

// Reads e_ident[EI_CLASS] (offset 4). Returns kElfClass32/64, or 0 if the
// file isn't readable or doesn't start with the ELF magic bytes.
inline int elf_class_of(const char* path) {
    int fd = open(path, O_RDONLY);
    if (fd < 0) return 0;
    unsigned char ident[5] = {0};
    ssize_t n = read(fd, ident, sizeof(ident));
    close(fd);
    if (n < 5) return 0;
    if (ident[0] != 0x7f || ident[1] != 'E' || ident[2] != 'L' || ident[3] != 'F') {
        return 0;
    }
    return ident[4];
}

// Reads e_type (2 bytes, little-endian, at offset 16). Returns -1 if
// unreadable. Caller should already know this is an ELF file (check
// elf_class_of() first) before trusting this.
inline int elf_type_of(const char* path) {
    int fd = open(path, O_RDONLY);
    if (fd < 0) return -1;
    unsigned char hdr[18] = {0};
    ssize_t n = read(fd, hdr, sizeof(hdr));
    close(fd);
    if (n < 18) return -1;
    return hdr[16] | (hdr[17] << 8);
}

// Detects a `#!interpreter [arg]` shebang line. On success, fills
// `interp_out` (interpreter path) and `arg_out` (optional single argument,
// empty string if none) and returns true.
inline bool parse_shebang(const char* path, char* interp_out, size_t interp_out_len,
                           char* arg_out, size_t arg_out_len) {
    int fd = open(path, O_RDONLY);
    if (fd < 0) return false;
    char buf[256] = {0};
    ssize_t n = read(fd, buf, sizeof(buf) - 1);
    close(fd);
    if (n < 2 || buf[0] != '#' || buf[1] != '!') return false;
    buf[n] = '\0';

    char* line_end = strchr(buf, '\n');
    if (line_end) *line_end = '\0';

    char* p = buf + 2;
    while (*p == ' ' || *p == '\t') p++;

    char* space = strchr(p, ' ');
    if (space) {
        *space = '\0';
        char* arg = space + 1;
        while (*arg == ' ' || *arg == '\t') arg++;
        snprintf(arg_out, arg_out_len, "%s", arg);
    } else {
        arg_out[0] = '\0';
    }
    snprintf(interp_out, interp_out_len, "%s", p);
    return interp_out[0] != '\0';
}

// Picks the best available system linker path for the given ELF class.
// Checks the Android 12+ APEX runtime location first (some OEM builds
// only ship the real binary there with /system/bin/linker* as a mere
// symlink that may or may not resolve depending on mount namespace),
// falling back to the classic /system/bin/ path. Returns nullptr if
// neither candidate is executable.
inline const char* pick_system_linker(int elf_class) {
    static const char* candidates64[] = {
        "/apex/com.android.runtime/bin/linker64",
        "/system/bin/linker64",
        nullptr,
    };
    static const char* candidates32[] = {
        "/apex/com.android.runtime/bin/linker",
        "/system/bin/linker",
        nullptr,
    };
    const char** candidates = (elf_class == kElfClass64) ? candidates64 : candidates32;
    for (int i = 0; candidates[i]; i++) {
        if (access(candidates[i], X_OK) == 0) {
            return candidates[i];
        }
    }
    return nullptr;
}

// Builds a NULL-terminated argv suitable for execve(linker_path, ...):
// [linker_path, target_path, orig_argv[1], orig_argv[2], ..., NULL]
// (orig_argv[0] - the caller's chosen argv[0] for the target - is
// intentionally dropped since the linker itself becomes argv[0] and the
// target path becomes argv[1]; this matches how the kernel's own implicit
// PT_INTERP invocation behaves.) Caller must free with free_argv().
inline char** build_linker_argv(const char* linker_path, const char* target_path,
                                 char* const orig_argv[]) {
    int orig_count = 0;
    while (orig_argv && orig_argv[orig_count]) orig_count++;

    int new_count = 2 + (orig_count > 0 ? orig_count - 1 : 0);
    char** new_argv = static_cast<char**>(malloc(sizeof(char*) * (new_count + 1)));
    if (!new_argv) return nullptr;

    int idx = 0;
    new_argv[idx++] = strdup(linker_path);
    new_argv[idx++] = strdup(target_path);
    for (int i = 1; i < orig_count; i++) {
        new_argv[idx++] = strdup(orig_argv[i]);
    }
    new_argv[idx] = nullptr;
    return new_argv;
}

inline void free_argv(char** argv) {
    if (!argv) return;
    for (int i = 0; argv[i]; i++) free(argv[i]);
    free(argv);
}

}  // namespace boffin_exec

#endif  // BOFFIN_SYSTEM_LINKER_HELPERS_H
