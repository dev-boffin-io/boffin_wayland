#include "pty_core.h"
#include "system_linker_helpers.h"

#include <pty.h>       // forkpty (bionic, requires Android API 23+)
#include <unistd.h>
#include <termios.h>
#include <sys/ioctl.h>
#include <sys/wait.h>
#include <signal.h>
#include <errno.h>
#include <cstring>
#include <cstdlib>

extern char **environ;

namespace {

// Execs `path` with `argv`, transparently redirecting through the system
// dynamic linker (see system_linker_helpers.h) when `path` is under our
// own app's writable data directory - which for this project's usage is
// always true, since we only ever spawn PREFIX/bin/bash or /bin/sh here,
// both of which live under PREFIX. The check is still performed properly
// (rather than assumed) so this stays correct if that ever changes.
//
// `data_dir_prefix` comes from the BOFFIN_EXEC_DATA_DIR entry in envp
// (set by Python's PtyCore.spawn_shell() - see main.py) - the same
// environment variable exec_shim.cpp's LD_PRELOAD hooks read, so both
// halves of this workaround agree on what counts as "our own directory"
// without hardcoding a package name into native code.
void exec_with_redirect(const char* path, char* const argv[], const char* data_dir_prefix) {
    if (!boffin_exec::needs_redirect(path, data_dir_prefix)) {
        execve(path, argv, environ);
        return;  // execve() only returns on failure
    }

    char interp[512];
    char script_arg[512];
    if (boffin_exec::parse_shebang(path, interp, sizeof(interp), script_arg, sizeof(script_arg))) {
        // Re-target the interpreter instead, with the script path spliced
        // into argv - the interpreter itself may also need redirecting
        // (checked again on the recursive call), so recurse rather than
        // duplicating the shebang-vs-ELF branch here.
        int orig_count = 0;
        while (argv[orig_count]) orig_count++;

        int extra = script_arg[0] ? 1 : 0;
        int new_count = 1 + extra + 1 + (orig_count > 0 ? orig_count - 1 : 0);
        char** new_argv = static_cast<char**>(malloc(sizeof(char*) * (new_count + 1)));
        int idx = 0;
        new_argv[idx++] = strdup(interp);
        if (extra) new_argv[idx++] = strdup(script_arg);
        new_argv[idx++] = strdup(path);
        for (int i = 1; i < orig_count; i++) new_argv[idx++] = strdup(argv[i]);
        new_argv[idx] = nullptr;

        exec_with_redirect(interp, new_argv, data_dir_prefix);
        boffin_exec::free_argv(new_argv);  // only reached if the recursive exec failed
        return;
    }

    int elf_class = boffin_exec::elf_class_of(path);
    if (elf_class == 0) {
        // Not a recognizable ELF and not a script - nothing we know how to
        // redirect; try as-is (will most likely fail with EACCES on a
        // targetSdkVersion 29+ device, but pretending to handle an unknown
        // format would be worse).
        execve(path, argv, environ);
        return;
    }

    int elf_type = boffin_exec::elf_type_of(path);
    if (elf_type == boffin_exec::kEtExec) {
        BOFFIN_LOGE("Cannot exec statically-linked binary via system linker: %s "
                    "(needs a dynamically-linked/PIE build to work under targetSdkVersion 29+)",
                    path);
        errno = ENOEXEC;
        return;
    }

    const char* linker_path = boffin_exec::pick_system_linker(elf_class);
    if (!linker_path) {
        BOFFIN_LOGE("No system linker found for ELF class %d - cannot exec %s", elf_class, path);
        errno = ENOENT;
        return;
    }

    char** new_argv = boffin_exec::build_linker_argv(linker_path, path, argv);
    BOFFIN_LOGD("Redirecting initial exec of %s through system linker %s", path, linker_path);
    execve(linker_path, new_argv, environ);
    boffin_exec::free_argv(new_argv);  // only reached if execve() failed
}

}  // namespace

extern "C" int pty_spawn(const char* path, char* const argv[], const char* cwd,
                          char* const envp[], int rows, int cols,
                          int* out_master_fd, int* out_pid) {
    if (!path || !argv || !out_master_fd || !out_pid) {
        errno = EINVAL;
        return -1;
    }

    struct winsize ws;
    memset(&ws, 0, sizeof(ws));
    ws.ws_row = static_cast<unsigned short>(rows > 0 ? rows : 24);
    ws.ws_col = static_cast<unsigned short>(cols > 0 ? cols : 80);

    int master_fd = -1;
    pid_t pid = forkpty(&master_fd, nullptr, nullptr, &ws);

    if (pid < 0) {
        // errno already set by forkpty
        return -1;
    }

    if (pid == 0) {
        // ---- Child process: now attached to the PTY slave as its controlling tty ----

        if (cwd && cwd[0] != '\0') {
            if (chdir(cwd) != 0) {
                _exit(127);
            }
        }

        // Apply extra environment variables (custom PREFIX/HOME/PATH etc.,
        // and BOFFIN_EXEC_DATA_DIR / LD_PRELOAD for the system-linker-exec
        // workaround - see main.py's PtyCore.spawn_shell()).
        const char* data_dir_prefix = nullptr;
        if (envp) {
            for (int i = 0; envp[i] != nullptr; ++i) {
                char* entry = strdup(envp[i]);
                if (!entry) continue;
                char* eq = strchr(entry, '=');
                if (eq) {
                    *eq = '\0';
                    setenv(entry, eq + 1, 1);
                    if (strcmp(entry, "BOFFIN_EXEC_DATA_DIR") == 0) {
                        data_dir_prefix = getenv("BOFFIN_EXEC_DATA_DIR");
                    }
                }
                free(entry);
            }
        }

        exec_with_redirect(path, argv, data_dir_prefix);
        // exec_with_redirect() only returns on failure
        _exit(127);
    }

    // ---- Parent process ----
    *out_master_fd = master_fd;
    *out_pid = static_cast<int>(pid);
    return 0;
}

extern "C" int pty_read(int master_fd, char* buf, int len) {
    if (master_fd < 0 || !buf || len <= 0) {
        errno = EINVAL;
        return -1;
    }
    ssize_t n = read(master_fd, buf, static_cast<size_t>(len));
    return static_cast<int>(n);
}

extern "C" int pty_write(int master_fd, const char* buf, int len) {
    if (master_fd < 0 || !buf || len <= 0) {
        errno = EINVAL;
        return -1;
    }
    ssize_t n = write(master_fd, buf, static_cast<size_t>(len));
    return static_cast<int>(n);
}

extern "C" int pty_resize(int master_fd, int rows, int cols) {
    if (master_fd < 0) {
        errno = EINVAL;
        return -1;
    }
    struct winsize ws;
    memset(&ws, 0, sizeof(ws));
    ws.ws_row = static_cast<unsigned short>(rows);
    ws.ws_col = static_cast<unsigned short>(cols);
    return ioctl(master_fd, TIOCSWINSZ, &ws);
}

extern "C" int pty_terminate(int master_fd, int pid) {
    int ret = 0;
    if (pid > 0) {
        ret = kill(static_cast<pid_t>(pid), SIGKILL);
        int status = 0;
        waitpid(static_cast<pid_t>(pid), &status, 0);
    }
    if (master_fd >= 0) {
        close(master_fd);
    }
    return ret;
}

extern "C" int pty_is_alive(int pid) {
    if (pid <= 0) {
        errno = EINVAL;
        return -1;
    }
    int status = 0;
    pid_t r = waitpid(static_cast<pid_t>(pid), &status, WNOHANG);
    if (r == 0) {
        return 1; // still running
    }
    if (r == static_cast<pid_t>(pid)) {
        return 0; // exited, already reaped
    }
    if (errno == ECHILD) {
        // already reaped elsewhere - assume dead
        return 0;
    }
    return -1;
}
