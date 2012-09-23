//
// Copyright 2009 Positive Team
//

#import <Cocoa/Cocoa.h>

@interface PTMapView : NSView {
	NSMutableDictionary *worldMap;
	NSMutableDictionary *values;
	NSColor *backgroundColor;
	NSColor *color;
	NSColor *textColor;
	id delegate;
	id dataSource;
}

@property (nonatomic, retain) id delegate;
@property (nonatomic, retain) id dataSource;
@property (nonatomic, retain) NSColor *backgroundColor;
@property (nonatomic, retain) NSColor *color;
@property (nonatomic, retain) NSColor *textColor;
@property (nonatomic, retain) NSMutableDictionary *worldMap;

- (void)draw;
- (NSColor *)colorForValue:(float)value;

@end

@protocol PTMapViewDelegate

@required

- (NSNumber *)valueForCountry:(NSString *)code;

@end