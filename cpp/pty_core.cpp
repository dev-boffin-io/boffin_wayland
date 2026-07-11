#include "pty_core.h"

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

        // Apply extra environment variables (custom PREFIX/HOME/PATH etc.)
        if (envp) {
            for (int i = 0; envp[i] != nullptr; ++i) {
                char* entry = strdup(envp[i]);
                if (!entry) continue;
                char* eq = strchr(entry, '=');
                if (eq) {
                    *eq = '\0';
                    setenv(entry, eq + 1, 1);
                }
                free(entry);
            }
        }

        execve(path, argv, environ);
        // execve() only returns on failure
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
