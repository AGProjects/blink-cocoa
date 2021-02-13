echo "Blink"

#https://developer.apple.com/library/archive/technotes/tn2206/_index.html#//apple_ref/doc/uid/DTS40007919-CH1-TNTAG211
codesign --verify --deep --strict --verbose=3 Frameworks/Python.framework/
codesign --verify --deep --strict --verbose=3 Frameworks/Sparkle.framework
codesign --verify --deep --strict --verbose=3 Frameworks/*.dylib
codesign --verify --deep --strict --verbose=3 staging/Blink.app/

echo "Gatekeeper"
#spctl --verbose=1 --assess staging/Blink.app
#codesign --verify --verbose=1 staging/Blink.app/
spctl -a -t exec -vvv staging/Blink.app
