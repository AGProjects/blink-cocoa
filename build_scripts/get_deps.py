#!/usr/bin/env python3

import subprocess
import os

def get_dependencies(lib_path):
    """Get direct dependencies of a dynamic library using 'ldd'."""
    try:
        result = subprocess.run(['otool', "-L", lib_path], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if result.returncode != 0:
            raise Exception(f"ldd failed: {result.stderr}")
        
        dependencies = []
        for line in result.stdout.splitlines():
            print(line)
            if "=>" in line:
                parts = line.split("=>")
                dep_path = parts[1].strip().split()[0]
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
    print(f"Checking dependencies of: {lib_path}")
    
    dependencies = get_dependencies(lib_path)
    for dep in dependencies:
        if dep not in visited:
            recursive_dependencies(dep, visited)
    
    return visited

def main():
    # Specify the dynamic library you want to check
    #lib_path = input("Enter the path to the dynamic library: ").strip()
    
    lib_path ="../Distribution/Resources/lib/sipsimple/core/_core.cpython-39-darwin.so"
    

    if not os.path.exists(lib_path):
        print(f"The library path '{lib_path}' does not exist.")
        return

    # Get all dependencies recursively
    all_dependencies = recursive_dependencies(lib_path)
    
    print("\nList of all dependencies (recursively found):")
    for dep in all_dependencies:
        print(dep)

if __name__ == "__main__":
    main()

