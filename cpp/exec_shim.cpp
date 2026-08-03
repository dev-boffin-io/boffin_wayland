/*
 * exec_shim.cpp
 * =============
 *
 * LD_PRELOAD shared library implementing Termux's "system_linker_exec"
 * workaround (see system_linker_helpers.h for the full explanation of
 * *why* this is needed and its limitations) for every exec() call a
 * shell session performs - not just the initial bash spawn.
 *
 * WHY THIS SEPARATE LIBRARY EXISTS (don't skip this if you're wondering
 * why pty_core.cpp's own redirect isn't enough): patching only the single
 * execve() call that starts bash fixes bash *itself* starting, but does
 * nothing for every command bash then runs internally - `ls`, `cat`,
 * `nano`, `python3`, etc. all reach libc's execve()-family functions
 * directly from bash's own code, never touching our pty_core.cpp at all.
 * Those calls need to be intercepted too, and LD_PRELOAD is the standard
 * mechanism for that: loaded into bash's process image (via the
 * BOFFIN_EXEC_LD_PRELOAD env var main.py's PtyCore.spawn_shell() sets),
 * this library's exported execve/execv/execvp/... symbols take priority
 * over bionic libc's own during dynamic symbol resolution, and because
 * LD_PRELOAD is inherited across exec() (as long as each redirected exec
 * goes through the system linker successfully with the environment
 * intact), it keeps applying to every subsequent command in the session
 * automatically, including e.g. a Python script bash launches.
 *
 * HOW SYMBOL INTERPOSITION IS AVOIDED FOR OUR OWN INTERNAL CALLS: naively
 * calling "execve(...)" from within this same shared object COULD resolve
 * back to our own override (infinite recursion) rather than the real
 * bionic implementation, depending on how the dynamic linker orders
 * symbol resolution. To sidestep this entirely, every function in this
 * file that needs to perform a *real* exec goes through real_execve(),
 * which caches a dlsym(RTLD_NEXT, "execve") pointer - RTLD_NEXT
 * explicitly means "the next definition of this symbol after this one",
 * i.e. bionic's actual implementation, never our own.
 */

#include "system_linker_helpers.h"

#include <cerrno>
#include <cstdarg>
#include <cstdlib>
#include <cstring>
#include <dlfcn.h>
#include <unistd.h>

extern char **environ;

namespace {

using execve_fn = int (*)(const char*, char* const[], char* const[]);

execve_fn real_execve() {
    static execve_fn fn = reinterpret_cast<execve_fn>(dlsym(RTLD_NEXT, "execve"));
    return fn;
}

// The directory prefix that means "needs the system-linker-exec
// redirect" - read once from BOFFIN_EXEC_DATA_DIR, which main.py's
// PtyCore.spawn_shell() sets in bash's environment (and which therefore
// stays set for every child process bash spawns, since env vars are
// inherited across exec unless explicitly cleared).
const char* data_dir_prefix() {
    static const char* prefix = getenv("BOFFIN_EXEC_DATA_DIR");
    return prefix;
}

// The actual redirect-aware execve, used by every public wrapper below.
// Mirrors pty_core.cpp's exec_with_redirect() logic exactly (see
// system_linker_helpers.h for the shared analysis functions both use) but
// calls real_execve() instead of the plain libc execve, since this code
// itself lives inside the LD_PRELOAD-shimmed symbol table.
int redirected_execve(const char* path, char* const argv[], char* const envp[]) {
    const char* prefix = data_dir_prefix();
    if (!boffin_exec::needs_redirect(path, prefix)) {
        return real_execve()(path, argv, envp);
    }

    char interp[512];
    char script_arg[512];
    if (boffin_exec::parse_shebang(path, interp, sizeof(interp), script_arg, sizeof(script_arg))) {
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

        int ret = redirected_execve(interp, new_argv, envp);  // interp may itself need redirecting
        boffin_exec::free_argv(new_argv);
        return ret;
    }

    int elf_class = boffin_exec::elf_class_of(path);
    if (elf_class == 0) {
        return real_execve()(path, argv, envp);
    }

    int elf_type = boffin_exec::elf_type_of(path);
    if (elf_type == boffin_exec::kEtExec) {
        BOFFIN_LOGE("Cannot exec statically-linked binary via system linker: %s "
                    "(needs a dynamically-linked/PIE build to work under targetSdkVersion 29+)",
                    path);
        errno = ENOEXEC;
        return -1;
    }

    const char* linker_path = boffin_exec::pick_system_linker(elf_class);
    if (!linker_path) {
        BOFFIN_LOGE("No system linker found for ELF class %d - cannot exec %s", elf_class, path);
        errno = ENOENT;
        return -1;
    }

    char** new_argv = boffin_exec::build_linker_argv(linker_path, path, argv);
    BOFFIN_LOGD("Redirecting exec of %s through system linker %s", path, linker_path);
    int ret = real_execve()(linker_path, new_argv, envp);
    boffin_exec::free_argv(new_argv);  // only reached if real_execve() failed
    return ret;
}

// PATH search for the execvp/execlp family - mirrors what bionic's own
// execvp() does, reimplemented here so it funnels through
// redirected_execve() (rather than calling the real execvp(), which
// would call the *real* execve() internally and skip our redirect).
int redirected_execvpe(const char* file, char* const argv[], char* const envp[]) {
    if (strchr(file, '/')) {
        return redirected_execve(file, argv, envp);
    }

    const char* path_env = getenv("PATH");
    if (!path_env || !*path_env) path_env = "/system/bin:/system/xbin";
    char* path_copy = strdup(path_env);

    char* saveptr = nullptr;
    char* dir = strtok_r(path_copy, ":", &saveptr);
    int last_errno = ENOENT;
    while (dir) {
        char full_path[1024];
        snprintf(full_path, sizeof(full_path), "%s/%s", dir, file);
        if (access(full_path, X_OK) == 0) {
            int ret = redirected_execve(full_path, argv, envp);
            last_errno = errno;
            if (errno != ENOENT) {
                free(path_copy);
                errno = last_errno;
                return ret;
            }
        }
        dir = strtok_r(nullptr, ":", &saveptr);
    }
    free(path_copy);
    errno = last_errno;
    return -1;
}

}  // namespace

// ---------------------------------------------------------------------------
// Public overrides - these symbol names shadow bionic libc's own via
// LD_PRELOAD. Bash (and anything it spawns) calls these transparently,
// believing them to be the normal libc functions.
// ---------------------------------------------------------------------------

extern "C" int execve(const char* path, char* const argv[], char* const envp[]) {
    return redirected_execve(path, argv, envp);
}

extern "C" int execv(const char* path, char* const argv[]) {
    return redirected_execve(path, argv, environ);
}

extern "C" int execvpe(const char* file, char* const argv[], char* const envp[]) {
    return redirected_execvpe(file, argv, envp);
}

extern "C" int execvp(const char* file, char* const argv[]) {
    return redirected_execvpe(file, argv, environ);
}

extern "C" int execl(const char* path, const char* arg0, ...) {
    va_list args;
    va_start(args, arg0);
    int count = 1;
    va_list count_args;
    va_copy(count_args, args);
    while (va_arg(count_args, const char*)) count++;
    va_end(count_args);

    char** argv = static_cast<char**>(malloc(sizeof(char*) * (count + 1)));
    argv[0] = const_cast<char*>(arg0);
    for (int i = 1; i < count; i++) argv[i] = va_arg(args, char*);
    argv[count] = nullptr;
    va_end(args);

    int ret = redirected_execve(path, argv, environ);
    free(argv);
    return ret;
}

extern "C" int execlp(const char* file, const char* arg0, ...) {
    va_list args;
    va_start(args, arg0);
    int count = 1;
    va_list count_args;
    va_copy(count_args, args);
    while (va_arg(count_args, const char*)) count++;
    va_end(count_args);

    char** argv = static_cast<char**>(malloc(sizeof(char*) * (count + 1)));
    argv[0] = const_cast<char*>(arg0);
    for (int i = 1; i < count; i++) argv[i] = va_arg(args, char*);
    argv[count] = nullptr;
    va_end(args);

    int ret = redirected_execvpe(file, argv, environ);
    free(argv);
    return ret;
}

extern "C" int execle(const char* path, const char* arg0, ...) {
    va_list args;
    va_start(args, arg0);
    int count = 1;
    va_list count_args;
    va_copy(count_args, args);
    while (va_arg(count_args, char*)) count++;
    va_end(count_args);

    char** argv = static_cast<char**>(malloc(sizeof(char*) * (count + 1)));
    argv[0] = const_cast<char*>(arg0);
    for (int i = 1; i < count; i++) argv[i] = va_arg(args, char*);
    argv[count] = nullptr;
    va_arg(args, char*);  // consume the NULL sentinel before reading envp
    char** envp = va_arg(args, char**);
    va_end(args);

    int ret = redirected_execve(path, argv, envp);
    free(argv);
    return ret;
}
