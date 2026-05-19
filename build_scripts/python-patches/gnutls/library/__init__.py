# gnutls/library/__init__.py  —  PATCHED for .app bundles + diagnostic logging
#
# Differences vs. upstream python3-gnutls 3.1.10:
#   1. _library_locations() prepends <bundle>/Contents/Frameworks/libs/ to the
#      search list when running inside a .app bundle, so the loader does not
#      depend on the launcher setting DYLD_LIBRARY_PATH.
#   2. _load_library() logs to stderr which candidate it actually loaded, the
#      absolute on-disk path dyld mapped, and any candidates it skipped
#      (with the OSError that caused the skip).
#
# This file is applied by build_scripts/06-copy-python-packages.sh after the
# stock site-packages tree is copied into Resources/lib/. Until upstream
# python3-gnutls ships an equivalent fix, every build re-applies it here.
#
# Mirror path: build_scripts/python-patches/<package>/<...>/<file>.py
# Target path: Distribution/Resources/lib/<package>/<...>/<file>.py

from itertools import chain

__all__ = ["constants", "errors", "functions", "types"]


def _get_system_name():
    import platform

    system = platform.system().lower()
    if system.startswith("cygwin"):
        system = "cygwin"
    return system


def _library_locations(abi_version):
    import os
    import sys

    system = _get_system_name()
    if system == "darwin":
        library_names = ["libgnutls.%d.dylib" % abi_version]
        dynamic_loader_env_vars = ["DYLD_LIBRARY_PATH", "LD_LIBRARY_PATH"]
        additional_paths = ["/usr/local/lib", "/opt/local/lib", "/sw/lib"]
        # PATCH: if we are running inside a .app bundle, also look in
        # Contents/Frameworks/libs/ directly, so we do not rely on the
        # launcher having set DYLD_LIBRARY_PATH.
        try:
            exe_dir = os.path.dirname(os.path.realpath(sys.executable))
            if "/Contents/MacOS" in exe_dir + os.sep:
                bundle_libs = os.path.normpath(
                    os.path.join(exe_dir, "..", "Frameworks", "libs")
                )
                if os.path.isdir(bundle_libs):
                    additional_paths.insert(0, bundle_libs)
        except Exception:
            pass
    elif system == "windows":
        library_names = ["libgnutls-%d.dll" % abi_version]
        dynamic_loader_env_vars = ["PATH"]
        additional_paths = ["."]
    elif system == "cygwin":
        library_names = ["cyggnutls-%d.dll" % abi_version]
        dynamic_loader_env_vars = ["LD_LIBRARY_PATH"]
        additional_paths = ["/usr/bin"]
    else:
        # Debian uses libgnutls-deb0.so.28, go figure
        library_names = [
            "libgnutls.so.%d" % abi_version,
            "libgnutls-deb0.so.%d" % abi_version,
        ]
        dynamic_loader_env_vars = ["LD_LIBRARY_PATH"]
        additional_paths = ["/usr/local/lib"]
    for library_name in library_names:
        for path in (
            path
            for env_var in dynamic_loader_env_vars
            for path in os.environ.get(env_var, "").split(":")
            if os.path.isdir(path)
        ):
            yield os.path.join(path, library_name)
        yield library_name
        for path in additional_paths:
            yield os.path.join(path, library_name)


def _resolve_loaded_path(candidate):
    """Best-effort: ask dyld where it actually mapped a just-loaded image.

    Returns the absolute on-disk path, or the original candidate string if
    we cannot determine it (e.g. on non-Darwin platforms or if the dyld
    private API moves).
    """
    try:
        import ctypes
        import os
        libdyld = ctypes.CDLL("/usr/lib/system/libdyld.dylib")
        libdyld._dyld_image_count.restype = ctypes.c_uint32
        libdyld._dyld_get_image_name.restype = ctypes.c_char_p
        libdyld._dyld_get_image_name.argtypes = [ctypes.c_uint32]
        wanted = os.path.basename(candidate)
        for i in range(libdyld._dyld_image_count()):
            name = libdyld._dyld_get_image_name(i)
            if not name:
                continue
            decoded = name.decode("utf-8", "replace")
            if os.path.basename(decoded) == wanted:
                return decoded
    except Exception:
        pass
    return candidate


def _log(msg):
    """Write a diagnostic line to stderr. Never raises."""
    try:
        import sys
        sys.stderr.write("python-gnutls: " + msg + "\n")
        sys.stderr.flush()
    except Exception:
        pass


def _load_library(abi_versions):
    from ctypes import CDLL

    tried = []
    for library in chain.from_iterable(
        _library_locations(abi_version)
        for abi_version in sorted(abi_versions, reverse=True)
    ):
        try:
            handle = CDLL(library)
        except OSError as e:
            tried.append((library, str(e)))
            continue
        resolved = _resolve_loaded_path(library)
        if resolved == library:
            _log("loaded libgnutls from %s" % library)
        else:
            _log("loaded libgnutls from %s (resolved to %s)" % (library, resolved))
        if tried:
            _log("  (%d earlier candidate(s) failed:)" % len(tried))
            for path, err in tried:
                _log("    - %s  [%s]" % (path, err))
        return handle
    _log("FAILED to load libgnutls. Candidates tried:")
    for path, err in tried:
        _log("  - %s  [%s]" % (path, err))
    raise RuntimeError(
        "cannot find a supported version of libgnutls on this system"
    )


libgnutls = _load_library(
    abi_versions=(28, 30)
)  # will use the highest of the available ABI versions


from gnutls.library import constants, errors, functions, types

__need_version__ = "3.2.0"

if functions.gnutls_check_version(__need_version__.encode()) is None:
    version = functions.gnutls_check_version(None)
    raise RuntimeError(
        "Found GNUTLS library version %s, but at least version %s is required"
        % (version, __need_version__)
    )

# Log the actual gnutls C library version we resolved against. Useful for
# distinguishing a bundle-load from a stray system load.
try:
    _v = functions.gnutls_check_version(None)
    _log("libgnutls C library version: %s"
         % (_v.decode("utf-8", "replace") if isinstance(_v, bytes) else _v))
except Exception:
    pass

# calling gnutls_global_init is no longer required starting with gnutls 3.3
if functions.gnutls_check_version("3.3".encode()) is None:
    libgnutls.gnutls_global_init()
