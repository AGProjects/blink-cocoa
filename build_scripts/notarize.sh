#!/bin/bash

#https://developer.apple.com/documentation/security/customizing-the-notarization-workflow
export EXPORT_PATH=~/work/blink/Distribution/Notary
export PRODUCT_NAME=Blink

APP_PATH="$EXPORT_PATH/$PRODUCT_NAME.app"
ZIP_PATH="$EXPORT_PATH/$PRODUCT_NAME.zip"

# Create a ZIP archive suitable for notarization.
/usr/bin/ditto -c -k --keepParent "$APP_PATH" "$ZIP_PATH"

# As a convenience, open the export folder in Finder.
/usr/bin/open "$EXPORT_PATH"

codesign --verify --verbose=1 $EXPORT_PATH/Blink.app
xcrun notarytool submit $EXPORT_PATH/Blink.zip --keychain-profile "notarytool-password" --wait
xcrun stapler staple $EXPORT_PATH/Blink.app
codesign --verify --verbose=1 $EXPORT_PATH/Blink.app
