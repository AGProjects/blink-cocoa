#import "Python.h"
#import <Cocoa/Cocoa.h>
#include <stdio.h>
#include <unistd.h>
#include <pthread.h>
#include <string.h>
#include <stdlib.h>

// Lines whose contents include any of these substrings are dropped from
// stderr. Used to silence noisy Core Data faulting warnings that come
// from Apple's Contacts framework when its internal NSManagedObject
// fault-firing fails after iCloud has already moved/deleted a row.
// Blink itself doesn't use Core Data, so any "CoreData: error:" line
// originates outside our code.
static const char *kBlinkStderrNoise[] = {
    "CoreData: error: Unhandled error occurred during faulting",
    "Rethrowing (from nts_ValueForProperty:)",
    // os_log throttling self-report — emitted when the unified-logging
    // pipeline drops messages because the process is logging faster
    // than it can drain. We can't act on it from inside the process
    // and it's not telling us which message was lost (just a count),
    // so it's pure noise.
    "Logging Error: Failed to receive",
    NULL
};

static int gBlinkOriginalStderr = -1;

static int blinkLineIsNoise(const char *line) {
    for (int i = 0; kBlinkStderrNoise[i] != NULL; i++) {
        if (strstr(line, kBlinkStderrNoise[i]) != NULL) {
            return 1;
        }
    }
    return 0;
}

static int blinkLineIsContinuation(const char *line) {
    // CoreData's "Unhandled error occurred during faulting" prints as
    // two physical lines: the message ending with "({" and a closing
    // "})" on its own. After we drop the message we also want to drop
    // the bare-brace continuation. A "continuation" line here is one
    // whose stripped contents look like "})" / "})," / similar — never
    // a useful standalone log line.
    while (*line == ' ' || *line == '\t') line++;
    if (line[0] == '}' && (line[1] == ')' || line[1] == ']' || line[1] == '}')) return 1;
    if (line[0] == '(' && (line[1] == '{' || line[1] == '\n' || line[1] == 0)) return 1;
    return 0;
}

static void *blinkStderrFilterThread(void *arg) {
    int readFd = (int)(long)arg;
    char buf[4096];
    char line[8192];
    size_t lineLen = 0;
    int dropContinuation = 0;

    for (;;) {
        ssize_t n = read(readFd, buf, sizeof(buf));
        if (n <= 0) break;

        for (ssize_t i = 0; i < n; i++) {
            char c = buf[i];
            if (lineLen < sizeof(line) - 1) {
                line[lineLen++] = c;
            }
            if (c != '\n' && lineLen < sizeof(line) - 1) continue;

            line[lineLen] = '\0';

            int drop = 0;

            // Drop bare empty lines (just whitespace, possibly with a
            // trailing newline). They're never informative in a log
            // stream, and Apple's logger emits them as separators
            // between blocks — the visible "tons of empty lines" the
            // CoreData filter would otherwise leave behind.
            {
                int hasContent = 0;
                for (size_t k = 0; k < lineLen; k++) {
                    char ch = line[k];
                    if (ch != ' ' && ch != '\t' && ch != '\n' && ch != '\r') {
                        hasContent = 1;
                        break;
                    }
                }
                if (!hasContent) drop = 1;
            }

            if (!drop && blinkLineIsNoise(line)) {
                drop = 1;
                // CoreData fault errors print one bare-brace follow-up;
                // arm a one-shot continuation drop to swallow it.
                dropContinuation = 1;
            } else if (!drop && dropContinuation && blinkLineIsContinuation(line)) {
                drop = 1;
                dropContinuation = 0;
            } else if (!drop) {
                dropContinuation = 0;
            }

            if (!drop && gBlinkOriginalStderr >= 0) {
                ssize_t written = 0;
                while (written < (ssize_t)lineLen) {
                    ssize_t w = write(gBlinkOriginalStderr, line + written, lineLen - written);
                    if (w < 0) break;
                    written += w;
                }
            }
            lineLen = 0;
        }
    }
    return NULL;
}

static void blinkInstallStderrFilter(void) {
    int pipefd[2];
    if (pipe(pipefd) != 0) return;

    int saved = dup(STDERR_FILENO);
    if (saved < 0) {
        close(pipefd[0]); close(pipefd[1]);
        return;
    }

    if (dup2(pipefd[1], STDERR_FILENO) < 0) {
        close(pipefd[0]); close(pipefd[1]); close(saved);
        return;
    }
    close(pipefd[1]);

    // Make sure any libc stdio stream over the new fd flushes per line
    // so the filter thread sees output promptly (pipes default to fully
    // buffered, which would hide the noise we're trying to filter).
    setvbuf(stderr, NULL, _IOLBF, 0);

    gBlinkOriginalStderr = saved;

    pthread_t thread;
    if (pthread_create(&thread, NULL, blinkStderrFilterThread,
                       (void *)(long)pipefd[0]) == 0) {
        pthread_detach(thread);
    }
}

int main(int argc, char *argv[])
{
    // Install the stderr filter before anything else so every libc /
    // Foundation / Core Data write on fd 2 is funnelled through the
    // pipe → filter → original-stderr chain. Must precede
    // Py_Initialize() so Python's sys.__stderr__ inherits the same
    // redirected fd.
    blinkInstallStderrFilter();

    NSAutoreleasePool *pool = [[NSAutoreleasePool alloc] init];

    NSBundle *mainBundle = [NSBundle mainBundle];
    NSString *libraryPath = [mainBundle privateFrameworksPath];
    NSString *libsPath = [libraryPath stringByAppendingPathComponent:@"libs"];
    NSString *resourcePath = [mainBundle resourcePath];
    NSArray *pythonPathArray = [NSArray arrayWithObjects: resourcePath, [resourcePath stringByAppendingPathComponent:@"lib"], nil];
    NSString *pythonPath = [pythonPathArray componentsJoinedByString:@":"];
    NSString *pythonHome = [libraryPath stringByAppendingPathComponent:@"Python.framework/Versions/Current"];

    NSString *dyldPath = [NSString stringWithFormat:@"%@:%@", libsPath, libraryPath];
    setenv("DYLD_LIBRARY_PATH", [dyldPath UTF8String], 1);
    setenv("PYTHONPATH", [pythonPath UTF8String], 1);
    setenv("PYTHONHOME", [pythonHome UTF8String], 1);
    setenv("PYTHONDONTWRITEBYTECODE", "1", 1);

    NSArray *possibleMainExtensions = [NSArray arrayWithObjects: @"py", @"pyc", @"pyo", nil];
    NSString *mainFilePath = nil;

    for (NSString *possibleMainExtension in possibleMainExtensions) {
        mainFilePath = [mainBundle pathForResource: @"Main" ofType: possibleMainExtension];
        if ( mainFilePath != nil ) break;
    }

    if ( !mainFilePath ) {
        [NSException raise: NSInternalInconsistencyException format: @"%s:%d main() Failed to find the Main.{py,pyc,pyo} file in the application wrapper's Resources directory.", __FILE__, __LINE__];
    }

    Py_Initialize();
    /* PySys_SetArgv(argc, (char **)argv); */

    const char *mainFilePathPtr = [mainFilePath UTF8String];
    FILE *mainFile = fopen(mainFilePathPtr, "r");
    int result = PyRun_SimpleFile(mainFile, (char *)[[mainFilePath lastPathComponent] UTF8String]);

    if ( result != 0 )
        [NSException raise: NSInternalInconsistencyException
                    format: @"%s:%d main() Running Python file '%@ failed'", __FILE__, __LINE__, mainFilePath];

    [pool drain];

    return result;
}

