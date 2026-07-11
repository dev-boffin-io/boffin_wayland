#ifndef BOFFIN_PTY_CORE_H
#define BOFFIN_PTY_CORE_H

#ifdef __cplusplus
extern "C" {
#endif

/*
 * pty_spawn
 * ---------
 * Forks a child process attached to a freshly-created pseudo-terminal and
 * execve()s `path` inside it.
 *
 * path   : absolute path to the executable (e.g. PREFIX/bin/bash)
 * argv   : NULL-terminated argv array (argv[0] is conventionally the program name)
 * cwd    : working directory to chdir() into before exec (can be NULL)
 * envp   : NULL-terminated array of "KEY=VALUE" strings that will be set in
 *          the child's environment before exec (existing env is inherited,
 *          these entries override/extend it)
 * rows/cols : initial terminal window size
 * out_master_fd : receives the PTY master file descriptor (parent side)
 * out_pid       : receives the child PID
 *
 * Returns 0 on success, -1 on failure (check errno).
 */
int pty_spawn(const char* path, char* const argv[], const char* cwd,
              char* const envp[], int rows, int cols,
              int* out_master_fd, int* out_pid);

/* Reads up to `len` bytes from the PTY master fd into buf.
 * Returns bytes read (>0), 0 on EOF (child exited / PTY closed), -1 on error. */
int pty_read(int master_fd, char* buf, int len);

/* Writes `len` bytes from buf to the PTY master fd.
 * Returns bytes written, -1 on error. */
int pty_write(int master_fd, const char* buf, int len);

/* Updates the PTY window size (equivalent to a terminal resize / SIGWINCH). */
int pty_resize(int master_fd, int rows, int cols);

/* Sends SIGKILL to the child, reaps it, and closes the master fd. */
int pty_terminate(int master_fd, int pid);

/* Returns 1 if the child process is still alive, 0 if it has exited, -1 on error. */
int pty_is_alive(int pid);

#ifdef __cplusplus
}
#endif

#endif /* BOFFIN_PTY_CORE_H */
