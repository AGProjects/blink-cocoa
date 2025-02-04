#!/usr/bin/env python3

import subprocess
import os
import sys
 
def get_dependencies(lib_path):
    """Get direct dependencies of a dynamic library using 'ldd'."""
    try:
        result = subprocess.run(['otool', "-L", lib_path], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        #if result.returncode != 0:
        #    raise Exception(f"ldd failed: {result.stderr}")
        
        dependencies = []
        for line in result.stdout.splitlines():
            dep_path = line.strip().split(" ")[0]
            if dep_path.endswith(":"):
                dep_path = dep_path[0:-1]
            if '/opt/local' in dep_path:
                dependencies.append(dep_path)
        return dependencies
    except Exception as e:
        print(f"Error while fetching dependencies for {lib_path}: {e}")
        return []

def recursive_dependencies(lib_path, visited=None):
    """Recursively fetch dependencies of the library."""
    if visited is None:
        visited = set()
    
    # Avoid re-visiting libraries already checked
    if lib_path in visited:
        return visited
    
    visited.add(lib_path)
    #print(f"Checking dependencies of: {lib_path}")
    
    dependencies = get_dependencies(lib_path)
    for dep in dependencies:
        if dep not in visited:
            recursive_dependencies(dep, visited)
    
    return visited

def main():
    # Specify the dynamic library you want to check
    #lib_path = input("Enter the path to the dynamic library: ").strip()
    
    #lib_path ="/Users/adigeo/Library/Python/3.9/lib/python/site-packages/sipsimple/core/_core.cpython-39-darwin.so"
    try:
        lib_path = sys.argv[1]
    except Exception as e:
        lib_path = input("Enter the path to the dynamic library: ").strip()

    if not os.path.exists(lib_path):
        print(f"The library path '{lib_path}' does not exist.")
        return

    # Get all dependencies recursively
    all_dependencies = set(list(recursive_dependencies(lib_path)))
    
    for dep in all_dependencies:
        if dep in lib_path:
            continue
        print(dep)

if __name__ == "__main__":
    main()

