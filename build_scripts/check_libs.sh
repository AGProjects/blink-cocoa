for f in Frameworks/*.dylib; do \
      if [ -L $f ]; then
          continue
      fi
      if ! otool -l $f \
         | grep -B1 -A3 LC_VERSION_MIN_MACOSX >/dev/null;
         #echo "$f is good"
      then \
          echo $f
          b=`basename $f`
          p=`port provides /opt/local/lib/$b`
          echo "$p: must be recompiled"; \
      fi;
done

slibs=`find Resources/lib -name \*.so`
dlibs=`find Resources/lib -name \*.dylib`
for f in $slibs $dlibs; do \
      if ! otool -l $f | grep -B1 -A3 LC_VERSION_MIN_MACOSX > /dev/null;
         #echo "$f is good"
      then \
          echo "$f: must be recompiled"; \
      fi;
done
  