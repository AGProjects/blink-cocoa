echo "Blink"
spctl --verbose=1 --assess staging/Blink.app
codesign --verify --verbose=1 staging/Blink.app/

echo

echo "SIP2SIP"
spctl --verbose=1 --assess staging_sip2sip/SIP2SIP.app
codesign --verify --verbose=1 staging_sip2sip/SIP2SIP.app/
