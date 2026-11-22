// Copyright (C) 2026 AG Projects. See LICENSE for details.
//
// See BlinkRTCAudioDevice.h. Built with ARC (-fobjc-arc on this file in the project).
//
// libwebrtc's side (sdk/objc/native/src/objc_audio_device_module.mm) calls the RTCAudioDevice
// methods on its own audio device thread; Blink's device choices come from Python on the main
// thread and are handed over to that thread (the delegate's dispatchAsync:), so the units are only
// ever made, started, stopped and torn down there. The I/O procs run on CoreAudio's threads and
// touch nothing but their IOContext.

#import "BlinkRTCAudioDevice.h"

#import <AudioToolbox/AudioToolbox.h>
#import <CoreAudio/CoreAudio.h>

// WebRTC.framework ships no headers. These are RTCAudioDevice.h's (sdk/objc/components/audio),
// as built into our framework (RTC_OBJC_TYPE with no prefix): the runtime knows the protocols by
// these names.

typedef OSStatus (^RTCAudioDeviceGetPlayoutDataBlock)(AudioUnitRenderActionFlags *_Nonnull actionFlags,
                                                      const AudioTimeStamp *_Nonnull timestamp,
                                                      NSInteger inputBusNumber,
                                                      UInt32 frameCount,
                                                      AudioBufferList *_Nonnull outputData);

typedef OSStatus (^RTCAudioDeviceRenderRecordedDataBlock)(AudioUnitRenderActionFlags *_Nonnull actionFlags,
                                                          const AudioTimeStamp *_Nonnull timestamp,
                                                          NSInteger inputBusNumber,
                                                          UInt32 frameCount,
                                                          AudioBufferList *_Nonnull inputData,
                                                          void *_Nullable renderContext);

typedef OSStatus (^RTCAudioDeviceDeliverRecordedDataBlock)(AudioUnitRenderActionFlags *_Nonnull actionFlags,
                                                           const AudioTimeStamp *_Nonnull timestamp,
                                                           NSInteger inputBusNumber,
                                                           UInt32 frameCount,
                                                           const AudioBufferList *_Nullable inputData,
                                                           void *_Nullable renderContext,
                                                           NS_NOESCAPE RTCAudioDeviceRenderRecordedDataBlock _Nullable renderBlock);

@protocol RTCAudioDeviceDelegate <NSObject>
@property(readonly, nonnull) RTCAudioDeviceDeliverRecordedDataBlock deliverRecordedData;
@property(readonly) double preferredInputSampleRate;
@property(readonly) NSTimeInterval preferredInputIOBufferDuration;
@property(readonly) double preferredOutputSampleRate;
@property(readonly) NSTimeInterval preferredOutputIOBufferDuration;
@property(readonly, nonnull) RTCAudioDeviceGetPlayoutDataBlock getPlayoutData;
- (void)notifyAudioInputParametersChange;
- (void)notifyAudioOutputParametersChange;
- (void)notifyAudioInputInterrupted;
- (void)notifyAudioOutputInterrupted;
- (void)dispatchAsync:(dispatch_block_t _Nonnull)block;
- (void)dispatchSync:(dispatch_block_t _Nonnull)block;
@end

@protocol RTCAudioDevice <NSObject>
@property(readonly) double deviceInputSampleRate;
@property(readonly) NSTimeInterval inputIOBufferDuration;
@property(readonly) NSInteger inputNumberOfChannels;
@property(readonly) NSTimeInterval inputLatency;
@property(readonly) double deviceOutputSampleRate;
@property(readonly) NSTimeInterval outputIOBufferDuration;
@property(readonly) NSInteger outputNumberOfChannels;
@property(readonly) NSTimeInterval outputLatency;
@property(readonly) BOOL isInitialized;
- (BOOL)initializeWithDelegate:(id<RTCAudioDeviceDelegate> _Nonnull)delegate;
- (BOOL)terminateDevice;
@property(readonly) BOOL isPlayoutInitialized;
- (BOOL)initializePlayout;
@property(readonly) BOOL isPlaying;
- (BOOL)startPlayout;
- (BOOL)stopPlayout;
@property(readonly) BOOL isRecordingInitialized;
- (BOOL)initializeRecording;
@property(readonly) BOOL isRecording;
- (BOOL)startRecording;
- (BOOL)stopRecording;
@end


// kAudioObjectPropertyElementMain (kAudioObjectPropertyElementMaster before the macOS 12 SDK): 0
#define BLINK_ELEMENT_MAIN 0

#define MAX_FRAMES 8192         // the most a device hands over in one go (AUHAL's default slice is 4096)
#define MAX_CHANNELS 2

static void BlinkLog(NSString *format, ...) NS_FORMAT_FUNCTION(1, 2);
static void BlinkLog(NSString *format, ...)
{
    va_list arguments;
    va_start(arguments, format);
    NSString *text = [[NSString alloc] initWithFormat:format arguments:arguments];
    va_end(arguments);
    NSLog(@"[videoroom] audio device: %@", text);
}


#pragma mark - CoreAudio devices

static BOOL IsDefaultName(NSString *name)
{
    return name == nil || name.length == 0 || [name isEqualToString:@"system_default"] || [name isEqualToString:@"None"];
}

static AudioDeviceID DefaultDevice(BOOL input)
{
    AudioObjectPropertyAddress address = {input ? kAudioHardwarePropertyDefaultInputDevice : kAudioHardwarePropertyDefaultOutputDevice,
                                          kAudioObjectPropertyScopeGlobal, BLINK_ELEMENT_MAIN};
    AudioDeviceID device = kAudioObjectUnknown;
    UInt32 size = sizeof(device);
    if (AudioObjectGetPropertyData(kAudioObjectSystemObject, &address, 0, NULL, &size, &device) != noErr)
        return kAudioObjectUnknown;
    return device;
}

static NSString *DeviceName(AudioDeviceID device)
{
    AudioObjectPropertyAddress address = {kAudioObjectPropertyName, kAudioObjectPropertyScopeGlobal, BLINK_ELEMENT_MAIN};
    CFStringRef name = NULL;
    UInt32 size = sizeof(name);
    if (device == kAudioObjectUnknown || AudioObjectGetPropertyData(device, &address, 0, NULL, &size, &name) != noErr || name == NULL)
        return nil;
    return (__bridge_transfer NSString *)name;
}

static UInt32 ChannelCount(AudioDeviceID device, BOOL input)
{
    AudioObjectPropertyAddress address = {kAudioDevicePropertyStreamConfiguration,
                                          input ? kAudioObjectPropertyScopeInput : kAudioObjectPropertyScopeOutput,
                                          BLINK_ELEMENT_MAIN};
    UInt32 size = 0;
    if (AudioObjectGetPropertyDataSize(device, &address, 0, NULL, &size) != noErr || size == 0)
        return 0;
    AudioBufferList *list = (AudioBufferList *)malloc(size);
    UInt32 channels = 0;
    if (AudioObjectGetPropertyData(device, &address, 0, NULL, &size, list) == noErr) {
        for (UInt32 i = 0; i < list->mNumberBuffers; i++)
            channels += list->mBuffers[i].mNumberChannels;
    }
    free(list);
    return channels;
}

// Devices hidden from the system's list (kAudioDevicePropertyIsHidden), reached by UID only.
static NSArray<NSString *> *HiddenDeviceUIDs(void)
{
    return @[@"com.ag-projects.podcast-mixer.input"];      // AG Podcast Mixer (ag-cute-music macos/driver)
}

static AudioDeviceID DeviceForUID(NSString *uid)
{
    AudioObjectPropertyAddress address = {kAudioHardwarePropertyTranslateUIDToDevice, kAudioObjectPropertyScopeGlobal, BLINK_ELEMENT_MAIN};
    CFStringRef qualifier = (__bridge CFStringRef)uid;
    AudioDeviceID device = kAudioObjectUnknown;
    UInt32 size = sizeof(device);
    if (AudioObjectGetPropertyData(kAudioObjectSystemObject, &address, sizeof(qualifier), &qualifier, &size, &device) != noErr)
        return kAudioObjectUnknown;
    return device;
}

static AudioDeviceID FindDevice(NSString *name, BOOL input)
{
    if (IsDefaultName(name))
        return DefaultDevice(input);
    AudioObjectPropertyAddress address = {kAudioHardwarePropertyDevices, kAudioObjectPropertyScopeGlobal, BLINK_ELEMENT_MAIN};
    UInt32 size = 0;
    if (AudioObjectGetPropertyDataSize(kAudioObjectSystemObject, &address, 0, NULL, &size) == noErr && size > 0) {
        UInt32 count = size / sizeof(AudioDeviceID);
        AudioDeviceID *devices = (AudioDeviceID *)malloc(size);
        AudioDeviceID found = kAudioObjectUnknown;
        if (AudioObjectGetPropertyData(kAudioObjectSystemObject, &address, 0, NULL, &size, devices) == noErr) {
            for (UInt32 i = 0; i < count && found == kAudioObjectUnknown; i++) {
                if (ChannelCount(devices[i], input) > 0 && [DeviceName(devices[i]) isEqualToString:name])
                    found = devices[i];
            }
        }
        free(devices);
        if (found != kAudioObjectUnknown)
            return found;
    }
    // hidden devices are not in the list: AG CuteMusic's "AG Podcast Mixer" (its driver hides it from other apps,
    // the SDK opens it by UID, coreaudio_dev.m), found by UID here too
    for (NSString *uid in HiddenDeviceUIDs()) {
        AudioDeviceID device = DeviceForUID(uid);
        if (device != kAudioObjectUnknown && ChannelCount(device, input) > 0 && [DeviceName(device) isEqualToString:name])
            return device;
    }
    BlinkLog(@"%@ device %@ not found, using the system's default", input ? @"input" : @"output", name);
    return DefaultDevice(input);
}

static double NominalRate(AudioDeviceID device)
{
    AudioObjectPropertyAddress address = {kAudioDevicePropertyNominalSampleRate, kAudioObjectPropertyScopeGlobal, BLINK_ELEMENT_MAIN};
    Float64 rate = 0;
    UInt32 size = sizeof(rate);
    if (AudioObjectGetPropertyData(device, &address, 0, NULL, &size, &rate) != noErr || rate <= 0)
        return 48000.0;
    return rate;
}

static UInt32 DeviceUInt32(AudioDeviceID device, AudioObjectPropertySelector selector, BOOL input)
{
    AudioObjectPropertyAddress address = {selector, input ? kAudioObjectPropertyScopeInput : kAudioObjectPropertyScopeOutput, BLINK_ELEMENT_MAIN};
    UInt32 value = 0;
    UInt32 size = sizeof(value);
    if (AudioObjectGetPropertyData(device, &address, 0, NULL, &size, &value) != noErr)
        return 0;
    return value;
}


#pragma mark - I/O

// What an I/O proc needs, nothing it must retain: the blocks are held by the device object for as
// long as a unit runs.
typedef struct {
    AudioUnit unit;
    UInt32 channels;
    int16_t *buffer;                    // input: AudioUnitRender's destination, MAX_FRAMES * MAX_CHANNELS
    __unsafe_unretained RTCAudioDeviceDeliverRecordedDataBlock deliver;
    __unsafe_unretained RTCAudioDeviceGetPlayoutDataBlock playout;
} IOContext;

static OSStatus InputProc(void *reference, AudioUnitRenderActionFlags *flags, const AudioTimeStamp *timestamp,
                          UInt32 bus, UInt32 frames, AudioBufferList *unused)
{
    IOContext *io = (IOContext *)reference;
    if (io->deliver == nil || io->unit == NULL || frames > MAX_FRAMES)
        return noErr;
    // libwebrtc renders into its own buffer through the block: that path sizes it frames x channels. Handing it
    // our buffer (inputData) is sized by frames alone in libwebrtc (objc_audio_device.mm, OnDeliverRecordedData,
    // M155): a stereo input (AG Podcast Mixer) lost half of every buffer and was sent garbled. The block captures
    // nothing (a global block, no allocation on the I/O thread): the unit comes as renderContext.
    return io->deliver(flags, timestamp, 1, frames, NULL, io,
                       ^OSStatus(AudioUnitRenderActionFlags *renderFlags, const AudioTimeStamp *renderTimestamp,
                                 NSInteger bus, UInt32 count, AudioBufferList *data, void *context) {
        IOContext *renderIO = (IOContext *)context;
        if (renderIO == NULL || renderIO->unit == NULL)
            return kAudioUnitErr_Uninitialized;
        UInt32 wanted = data->mBuffers[0].mNumberChannels;      // libwebrtc's channels: what it last took in
        if (wanted == renderIO->channels)
            return AudioUnitRender(renderIO->unit, renderFlags, renderTimestamp, 1, count, data);
        // libwebrtc kept its channels when the input changed under it (the mono microphone, then AG Podcast Mixer's
        // two, for the next podcast in the same run): rendered into our buffer, as the device gives it, then
        // converted (stereo to mono averaged, mono to stereo in both); rendered straight, the render failed and
        // nothing at all was sent
        if (renderIO->buffer == NULL || count > MAX_FRAMES || wanted < 1 || wanted > 2 || renderIO->channels > 2)
            return kAudioUnitErr_FormatNotSupported;
        AudioBufferList own;
        own.mNumberBuffers = 1;
        own.mBuffers[0].mNumberChannels = renderIO->channels;
        own.mBuffers[0].mDataByteSize = count * renderIO->channels * sizeof(int16_t);
        own.mBuffers[0].mData = renderIO->buffer;
        OSStatus status = AudioUnitRender(renderIO->unit, renderFlags, renderTimestamp, 1, count, &own);
        if (status != noErr)
            return status;
        int16_t *from = renderIO->buffer, *to = (int16_t *)data->mBuffers[0].mData;
        if (to == NULL || data->mBuffers[0].mDataByteSize < count * wanted * sizeof(int16_t))
            return kAudioUnitErr_TooManyFramesToProcess;
        if (wanted == 1) {
            for (UInt32 i = 0; i < count; i++)
                to[i] = (int16_t)(((int32_t)from[2 * i] + (int32_t)from[2 * i + 1]) / 2);
        } else {
            for (UInt32 i = 0; i < count; i++)
                to[2 * i] = to[2 * i + 1] = from[i];
        }
        return noErr;
    });
}

static OSStatus OutputProc(void *reference, AudioUnitRenderActionFlags *flags, const AudioTimeStamp *timestamp,
                           UInt32 bus, UInt32 frames, AudioBufferList *data)
{
    IOContext *io = (IOContext *)reference;
    if (io->playout == nil) {
        for (UInt32 i = 0; i < data->mNumberBuffers; i++)
            memset(data->mBuffers[i].mData, 0, data->mBuffers[i].mDataByteSize);
        *flags |= kAudioUnitRenderAction_OutputIsSilence;
        return noErr;
    }
    return io->playout(flags, timestamp, 0, frames, data);
}

// An AUHAL bound to device, 16-bit interleaved PCM at rate with channels on our side.
static AudioUnit MakeUnit(BOOL input, AudioDeviceID device, double rate, UInt32 channels, IOContext *io)
{
    AudioComponentDescription description = {kAudioUnitType_Output, kAudioUnitSubType_HALOutput, kAudioUnitManufacturer_Apple, 0, 0};
    AudioComponent component = AudioComponentFindNext(NULL, &description);
    AudioUnit unit = NULL;
    OSStatus status;
    if (component == NULL || (status = AudioComponentInstanceNew(component, &unit)) != noErr) {
        BlinkLog(@"cannot make an AUHAL unit");
        return NULL;
    }

#define CHECK(call, what) if ((status = (call)) != noErr) { BlinkLog(@"%@: %@ failed (OSStatus %d)", input ? @"input" : @"output", what, (int)status); AudioComponentInstanceDispose(unit); return NULL; }

    UInt32 on = 1, off = 0;
    if (input) {
        CHECK(AudioUnitSetProperty(unit, kAudioOutputUnitProperty_EnableIO, kAudioUnitScope_Input, 1, &on, sizeof(on)), @"enabling input");
        CHECK(AudioUnitSetProperty(unit, kAudioOutputUnitProperty_EnableIO, kAudioUnitScope_Output, 0, &off, sizeof(off)), @"disabling output");
    }
    CHECK(AudioUnitSetProperty(unit, kAudioOutputUnitProperty_CurrentDevice, kAudioUnitScope_Global, 0, &device, sizeof(device)), @"choosing the device");

    AudioStreamBasicDescription format = {0};
    format.mSampleRate = rate;
    format.mFormatID = kAudioFormatLinearPCM;
    format.mFormatFlags = kAudioFormatFlagIsSignedInteger | kAudioFormatFlagIsPacked;
    format.mBitsPerChannel = 16;
    format.mChannelsPerFrame = channels;
    format.mFramesPerPacket = 1;
    format.mBytesPerFrame = channels * sizeof(int16_t);
    format.mBytesPerPacket = format.mBytesPerFrame;
    if (input) {
        CHECK(AudioUnitSetProperty(unit, kAudioUnitProperty_StreamFormat, kAudioUnitScope_Output, 1, &format, sizeof(format)), @"setting the format");
        AURenderCallbackStruct callback = {InputProc, io};
        CHECK(AudioUnitSetProperty(unit, kAudioOutputUnitProperty_SetInputCallback, kAudioUnitScope_Global, 0, &callback, sizeof(callback)), @"setting the input callback");
    } else {
        CHECK(AudioUnitSetProperty(unit, kAudioUnitProperty_StreamFormat, kAudioUnitScope_Input, 0, &format, sizeof(format)), @"setting the format");
        AURenderCallbackStruct callback = {OutputProc, io};
        CHECK(AudioUnitSetProperty(unit, kAudioUnitProperty_SetRenderCallback, kAudioUnitScope_Input, 0, &callback, sizeof(callback)), @"setting the render callback");
    }
    UInt32 maximum = MAX_FRAMES;
    AudioUnitSetProperty(unit, kAudioUnitProperty_MaximumFramesPerSlice, kAudioUnitScope_Global, 0, &maximum, sizeof(maximum));
    CHECK(AudioUnitInitialize(unit), @"initializing");
#undef CHECK
    return unit;
}


#pragma mark - The device

// One direction: its device and what libwebrtc is told about it.
@interface BlinkRTCAudioDirection : NSObject {
@public
    BOOL input;
    NSString *name;                 // Blink's choice (nil: the default)
    AudioDeviceID device;
    double rate;
    UInt32 channels;
    NSTimeInterval ioBuffer;
    NSTimeInterval latency;
    AudioUnit unit;
    IOContext io;
    BOOL initialized;
    BOOL running;
}
@end

@implementation BlinkRTCAudioDirection

- (void)resolve
{
    device = FindDevice(name, input);
    rate = NominalRate(device);
    UInt32 count = ChannelCount(device, input);
    channels = count >= 2 ? 2 : 1;
    UInt32 frames = DeviceUInt32(device, kAudioDevicePropertyBufferFrameSize, input);
    ioBuffer = frames > 0 ? frames / rate : 0.01;
    latency = (DeviceUInt32(device, kAudioDevicePropertyLatency, input) + DeviceUInt32(device, kAudioDevicePropertySafetyOffset, input) + frames) / rate;
}

- (NSString *)describe
{
    return [NSString stringWithFormat:@"%@ %.0f Hz %u ch", DeviceName(device) ?: @"(none)", rate, (unsigned)channels];
}

- (void)dealloc
{
    if (io.buffer != NULL)
        free(io.buffer);
}

@end


@interface BlinkRTCAudioDevice () <RTCAudioDevice>
@end

@implementation BlinkRTCAudioDevice {
    id<RTCAudioDeviceDelegate> _delegate;
    RTCAudioDeviceDeliverRecordedDataBlock _deliver;
    RTCAudioDeviceGetPlayoutDataBlock _playout;
    BlinkRTCAudioDirection *_input;
    BlinkRTCAudioDirection *_output;
    BOOL _initialized;
}

+ (instancetype)sharedDevice
{
    static BlinkRTCAudioDevice *device = nil;
    static dispatch_once_t once;
    dispatch_once(&once, ^{
        device = [[BlinkRTCAudioDevice alloc] init];
    });
    return device;
}

- (instancetype)init
{
    self = [super init];
    if (self == nil)
        return nil;
    _input = [BlinkRTCAudioDirection new];
    _input->input = YES;
    _output = [BlinkRTCAudioDirection new];
    _output->input = NO;
    [_input resolve];
    [_output resolve];

    // following the system's default devices while they are the choice
    __weak BlinkRTCAudioDevice *weakSelf = self;
    AudioObjectPropertyAddress inputAddress = {kAudioHardwarePropertyDefaultInputDevice, kAudioObjectPropertyScopeGlobal, BLINK_ELEMENT_MAIN};
    AudioObjectAddPropertyListenerBlock(kAudioObjectSystemObject, &inputAddress, dispatch_get_main_queue(), ^(UInt32 count, const AudioObjectPropertyAddress *addresses) {
        BlinkRTCAudioDevice *strongSelf = weakSelf;
        if (strongSelf != nil && IsDefaultName(strongSelf->_input->name))
            [strongSelf rebind:strongSelf->_input name:nil];
    });
    AudioObjectPropertyAddress outputAddress = {kAudioHardwarePropertyDefaultOutputDevice, kAudioObjectPropertyScopeGlobal, BLINK_ELEMENT_MAIN};
    AudioObjectAddPropertyListenerBlock(kAudioObjectSystemObject, &outputAddress, dispatch_get_main_queue(), ^(UInt32 count, const AudioObjectPropertyAddress *addresses) {
        BlinkRTCAudioDevice *strongSelf = weakSelf;
        if (strongSelf != nil && IsDefaultName(strongSelf->_output->name))
            [strongSelf rebind:strongSelf->_output name:nil];
    });
    return self;
}

#pragma mark Blink's choices (main thread)

- (void)setInputDeviceName:(NSString *)name
{
    [self rebind:_input name:IsDefaultName(name) ? nil : [name copy]];
}

- (void)setOutputDeviceName:(NSString *)name
{
    [self rebind:_output name:IsDefaultName(name) ? nil : [name copy]];
}

- (NSString *)inputDescription
{
    return [_input describe];
}

- (NSString *)outputDescription
{
    return [_output describe];
}

// The direction moved to another device: on libwebrtc's thread, a running unit is remade on it and
// libwebrtc told of the new rate and channels.
- (void)rebind:(BlinkRTCAudioDirection *)direction name:(NSString *)name
{
    id<RTCAudioDeviceDelegate> delegate = _delegate;
    void (^change)(void) = ^{
        direction->name = name;
        AudioDeviceID device = FindDevice(name, direction->input);
        if (device == direction->device && direction->device != kAudioObjectUnknown)
            return;
        BOOL wasInitialized = direction->initialized, wasRunning = direction->running;
        [self teardown:direction];
        [direction resolve];
        BlinkLog(@"%@ now %@", direction->input ? @"input" : @"output", [direction describe]);
        if (self->_delegate != nil) {
            if (direction->input)
                [self->_delegate notifyAudioInputParametersChange];
            else
                [self->_delegate notifyAudioOutputParametersChange];
        }
        if (wasInitialized && [self setup:direction] && wasRunning)
            [self start:direction];
    };
    if (delegate != nil)
        [delegate dispatchAsync:change];
    else
        change();       // not in use: only the choice is kept (and the device looked up)
}

#pragma mark Units (libwebrtc's thread)

- (BOOL)setup:(BlinkRTCAudioDirection *)direction
{
    if (direction->initialized)
        return YES;
    [direction resolve];
    IOContext *io = &direction->io;
    io->channels = direction->channels;
    if (direction->input && io->buffer == NULL)
        io->buffer = (int16_t *)calloc(MAX_FRAMES * MAX_CHANNELS, sizeof(int16_t));
    io->deliver = direction->input ? _deliver : nil;
    io->playout = direction->input ? nil : _playout;
    io->unit = NULL;
    AudioUnit unit = MakeUnit(direction->input, direction->device, direction->rate, direction->channels, io);
    if (unit == NULL)
        return NO;
    io->unit = unit;
    direction->unit = unit;
    direction->initialized = YES;
    BlinkLog(@"%@ on %@", direction->input ? @"recording" : @"playing", [direction describe]);
    return YES;
}

- (BOOL)start:(BlinkRTCAudioDirection *)direction
{
    if (!direction->initialized && ![self setup:direction])
        return NO;
    if (direction->running)
        return YES;
    OSStatus status = AudioOutputUnitStart(direction->unit);
    if (status != noErr) {
        BlinkLog(@"cannot start %@ (OSStatus %d)", direction->input ? @"recording" : @"playing", (int)status);
        return NO;
    }
    direction->running = YES;
    return YES;
}

- (void)stop:(BlinkRTCAudioDirection *)direction
{
    if (direction->running && direction->unit != NULL)
        AudioOutputUnitStop(direction->unit);
    direction->running = NO;
}

- (void)teardown:(BlinkRTCAudioDirection *)direction
{
    [self stop:direction];
    if (direction->unit != NULL) {
        AudioUnitUninitialize(direction->unit);
        AudioComponentInstanceDispose(direction->unit);
    }
    direction->unit = NULL;
    direction->io.unit = NULL;
    direction->initialized = NO;
}

#pragma mark RTCAudioDevice

- (double)deviceInputSampleRate { return _input->rate; }
- (NSTimeInterval)inputIOBufferDuration { return _input->ioBuffer; }
- (NSInteger)inputNumberOfChannels { return _input->channels; }
- (NSTimeInterval)inputLatency { return _input->latency; }
- (double)deviceOutputSampleRate { return _output->rate; }
- (NSTimeInterval)outputIOBufferDuration { return _output->ioBuffer; }
- (NSInteger)outputNumberOfChannels { return _output->channels; }
- (NSTimeInterval)outputLatency { return _output->latency; }
- (BOOL)isInitialized { return _initialized; }
- (BOOL)isPlayoutInitialized { return _output->initialized; }
- (BOOL)isPlaying { return _output->running; }
- (BOOL)isRecordingInitialized { return _input->initialized; }
- (BOOL)isRecording { return _input->running; }

- (BOOL)initializeWithDelegate:(id<RTCAudioDeviceDelegate>)delegate
{
    _delegate = delegate;
    _deliver = delegate.deliverRecordedData;
    _playout = delegate.getPlayoutData;
    [_input resolve];
    [_output resolve];
    _initialized = YES;
    BlinkLog(@"input %@, output %@", [_input describe], [_output describe]);
    return YES;
}

- (BOOL)terminateDevice
{
    [self teardown:_input];
    [self teardown:_output];
    _initialized = NO;
    _delegate = nil;
    _deliver = nil;
    _playout = nil;
    return YES;
}

- (BOOL)initializePlayout { return [self setup:_output]; }
- (BOOL)startPlayout { return [self start:_output]; }
- (BOOL)stopPlayout { [self teardown:_output]; return YES; }
- (BOOL)initializeRecording { return [self setup:_input]; }
- (BOOL)startRecording { return [self start:_input]; }
- (BOOL)stopRecording { [self teardown:_input]; return YES; }

@end
