#import "Python.h"
#import <Cocoa/Cocoa.h>

int main(int argc, char *argv[])
{
    NSAutoreleasePool *pool = [[NSAutoreleasePool alloc] init];

    NSBundle *mainBundle = [NSBundle mainBundle];
    NSString *libraryPath = [mainBundle privateFrameworksPath];
    NSString *resourcePath = [mainBundle resourcePath];
    NSArray *pythonPathArray = [NSArray arrayWithObjects: resourcePath, [resourcePath stringByAppendingPathComponent:@"lib"], nil];
    NSString *pythonPath = [pythonPathArray componentsJoinedByString:@":"];
    NSString *pythonHome = [libraryPath stringByAppendingPathComponent:@"Python.framework/Versions/Current"];

    setenv("DYLD_LIBRARY_PATH", [libraryPath UTF8String], 1);
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

