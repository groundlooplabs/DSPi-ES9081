# Spectrum Analyser (RTA / FFT) Specification

*Status: implemented, hardware-untested*
*Last updated: 2026-09-12 (protocol V2, single transform up to 1024 points)*

## 1. Overview

The onboard **spectrum analyser** ("RTA") is a single FFT engine that can be
pointed at any set of channels on either side of the processing chain. It
produces two products from every frame: a third-octave **band table** small
enough for any transport, and the **raw magnitude bins** for the Console's
FFT view. It exists so that S/PDIF, ADAT, and I2S inputs, every output, and
non-USB clients (the ESP32 front panel, control-surface OLEDs, UART and I2C
bridges) can all see a spectrum. For USB playback the host already holds the
samples and can transform them itself.

### Key characteristics

- **One engine, selectable tap.** The tap is either the input side (after the
  per-input PEQ, before the matrix) or the output side (after gain and delay,
  before encoding, which is exactly what the slot transmits).
- **Any set of channels at constant CPU.** The engine transforms one channel
  per frame and rotates through the selected set, keeping the latest result
  for each. Selecting more channels never adds CPU. It only lengthens the
  interval between refreshes of any one channel.
- **Out of the audio path.** The audio callback copies the current channel's
  samples into a capture buffer and nothing else. The transform runs in the
  main loop as background work, split into bounded steps, so audio always
  wins and a busy device slows the analyser rather than the audio.
- **Alignment-neutral by construction.** The tap is read-only and changes no
  sample count, so it cannot move any output slot relative to another.
- **Small.** The transform runs in place in the capture buffer, and every
  constant table lives in flash.
- **Configurable cost.** FFT size (256 to 1024 points) and averaging let the
  user trade CPU and refresh rate for resolution.
- **Transient only.** Never persisted, off at boot, not part of presets or the
  bulk-params blob, and it switches itself off when nobody is reading it.
- **Platform parity.** Identical wire protocol on both platforms. RP2350 uses a
  float kernel, RP2040 a Q15 kernel with a lower noise floor.

### Signal-chain position

```
process_input_block():
    PASS 2: per-input PEQ + metering          >>> INPUT TAP (per-input meter loop)
    PASS 2.5 / 3: leveller, upmixer
    PASS 4: matrix mix -> buf_out[]
    siggen injection
    PASS 5-7: crossover + PEQ, trim, master volume, mute, delay
              per-output metering              >>> OUTPUT TAP (per-output meter loop,
              S24 finalize / encode                 on whichever core owns the row)
```

Both taps sit inside the existing peak-meter loops, which already run once per
block per channel on the core that owns that channel, after every sample-value
change and before any conversion. The output tap therefore sees the same values
the slot transmits, and it runs before `output_block_to_s24_inplace()` so it
never reads a row that has already been converted while ADAT is active.

## 2. Products

### 2.1 Band table

Third-octave bands with IEC 61260 nominal centres from 20 Hz upward. The band
count depends on the sample rate:

| Sample rate | Bands | Top band centre |
|-------------|-------|-----------------|
| 44.1 kHz | 31 | 20 kHz |
| 48 kHz | 31 | 20 kHz |
| 96 kHz | 34 | 40 kHz |

`RTA_MAX_BANDS` is 36. A band's level is the sum of the windowed bin powers
inside the band's edges, normalised so that a full-scale sine reads 0 dBFS in
the band that contains it. Pink noise therefore reads flat across bands, and
white noise rises 3 dB per octave, which is standard RTA behaviour.

The DC bin belongs to no band. A band that contains no bin at the current
size and rate is **empty** and reads the floor (0); it is never faked from a
neighbouring bin. `RtaStatus.first_band` reports the lowest band the current
size resolves, so a display can grey out the rest. A band that holds only one
or two bins captures only part of a tone's Hann main lobe and reads a centred
tone up to 1.76 dB low; wideband levels are unaffected.

Lowest resolved band per size, at 48 kHz:

| Points | Bin width | First resolved band | Frame time |
|--------|-----------|---------------------|------------|
| 256 | 187.5 Hz | 200 Hz | 5 ms |
| 512 | 93.75 Hz | 100 Hz | 11 ms |
| 1024 | 46.9 Hz | 50 Hz | 21 ms |

At 44.1 kHz each size resolves one band lower; at 96 kHz one octave (three
bands) higher. 1024 points is the ceiling: 2048 would resolve down to 25 Hz
but costs another 4.6 KB of RAM on RP2350, and resolving the 20 Hz band would
need 16384 points. A decimated bass stream was built and rejected in favour of
this simpler single-transform design.

Two arrays are kept per channel: the **averaged** level and the **peak-hold**
level. Averaging is an exponential moving average in the power domain (not the
dB domain, which under-reads noise by up to 2.5 dB in single-bin bands). Its
coefficient is derived from `avg_ms` and the channel's actual refresh interval
so the time constant stays in real time regardless of how many channels share
the rotation. Peak hold decays at `peak_decay_db_s`, with the decay time
carried between publishes so short intervals still decay.

### 2.2 Raw bins

The most recent frame's magnitude bins, `N/2` values, normalised so a full-scale
sine at a bin centre reads 0 dBFS. Only the latest frame is kept, tagged with
its channel and a sequence number, because a bin set per channel would cost
more RAM than the whole feature. A host that wants per-channel bins for a
multichannel set polls at the frame rate; the intended use is single-channel.

### 2.3 Level encoding

Every level on the wire is one byte in 0.5 dB steps:

```
level_dBFS = (v - 243) * 0.5        v = 255 -> +6.0 dBFS
                                    v = 243 ->  0.0 dBFS
                                    v = 0   -> -121.5 dBFS or below (floor)
```

The 6 dB headroom exists because upmix-derived rows and hot EQ can legitimately
exceed 0 dBFS. `RTA_LEVEL_ZERO_DBFS` = 243 is reported in the caps so clients
never hard-code it.

## 3. The engine

### 3.1 Frame state machine

```
IDLE  --start-->  FILL  --buffer full (audio callback)-->  FFT
FFT  --main loop, one step per call-->  FINISH  -->  publish  -->  advance
advance: next channel in the selected set, wr = 0, back to FILL
```

- **FILL.** Each block, the owning core's tap copies up to the remaining
  buffer space from the tapped row and writes back how many samples it
  copied. On RP2350 the samples are copied as float. On RP2040 the Q28 samples
  are converted to saturated Q15 (`>> 13`). When the buffer fills mid-block
  the tail of that block is dropped.
- **FFT.** `rta_service()` runs from the main loop next to `siggen_service()`.
  Each call performs one bounded unit of the transform and returns: the
  prescale, one butterfly stage, the permutation, or the real-split pass. No
  unit exceeds roughly 250 µs on RP2040 at the largest size, so the main loop's
  USB and transport duties never stall.
- **FINISH.** One call applies the window, sums bin powers into bands, writes
  the bin levels, publishes, and advances the rotation. This is the longest
  single step and is charged to `last_frame_us` so the bench can see it.

Because the transform runs in place, capture is off while it runs. The gap is
the transform time, a fraction of a millisecond on RP2350 and a couple of
milliseconds on RP2040, against a 21 ms frame at 1024 points and 48 kHz.

### 3.2 Channel rotation

The selected set is `channel_mask` at the chosen tap, intersected with what is
actually live: active input rows for the input tap (including upmix-derived
rows while the upmixer runs), enabled outputs for the output tap. The rotation
walks the set in ascending bit order. Bits for channels that are not live are
skipped, so disabled outputs do not slow the refresh of the others.

Per-channel refresh interval = (fill time + transform time) x number of live
channels in the set.

| Points | Rate | Fill time | 1 channel | 2 channels | 8 channels |
|--------|------|-----------|-----------|------------|------------|
| 1024 | 48 kHz | 21.3 ms | 21 ms | 43 ms | 171 ms |
| 512 | 48 kHz | 10.7 ms | 11 ms | 21 ms | 85 ms |
| 1024 | 96 kHz | 10.7 ms | 11 ms | 21 ms | 85 ms |

The engine advances by what the tap actually copied each packet, and the main
loop moves on if the channel being filled stops being live (a USB alt change,
a disabled output, the upmixer parking), so a frame can never stall.

### 3.3 Cross-core tap

Output rows 2 and above are processed by Core 1 in EQ worker mode, so the tap
for those rows runs on Core 1. Core 0 writes the tap descriptor into
`rta_view` (`RtaPacketView`, rta.h) in `rta_packet_begin()` at the top of
`process_input_block()`, ahead of the `work_ready` barrier, so both cores use
one view per packet. This follows the same single-view rule as the loudness,
crossfeed, and subharm snapshots in `Core1EqWork`, just in its own struct.

Only the core that owns the row copies samples this packet, and it copies to
the snapshotted index. It writes back one field, `produced`, which Core 0
reads in `rta_packet_end()` after the `work_done` join. Core 0 alone advances
the index and changes phase, so the two cores never touch the same
bookkeeping. The capture buffer is written by at most one core per packet and
read by the main loop only after the fill completes, so no lock is needed
beyond the existing handshake barriers.

Every Core 0 entry into the pipeline (the USB ring drain, the S/PDIF, I2S,
and ADAT polls, the signal-generator pump) runs from the main loop, as do the
vendor handlers, so `rta_service()` and `process_input_block()` never
pre-empt each other.

### 3.4 Window, power, and normalisation

The window is Hann, applied in the frequency domain after the transform:

```
Xw[k] = 0.5 * X[k] - 0.25 * (X[k-1] + X[k+1])
```

This removes the per-sample multiply and the window table. Powers are summed
per band in the linear domain and converted to dB once per band; the bin
product converts once per bin using a fast log2 on the float exponent (RP2350)
or a leading-zero count (RP2040).

### 3.5 Restart conditions

The current frame is discarded and capture restarts from index 0 on: a sample
rate change, an input source switch, a preset load or factory reset, and any
config change that alters tap, mask, or size. `rta_service()` also restarts
on its own if it sees the device rate differ from the rate it armed with.
Averaging and peak-hold state are cleared on tap, mask, or rate change and by
`RTA_CTL_RESET_AVG`. The soft-mute envelope is not a restart condition; the
tap simply sees the faded samples.

### 3.6 Auto-off

The analyser records the time of the last band or bin read from any transport.
If `RTA_IDLE_TIMEOUT_MS` (5000) passes without one, it stops, releases the
tap, and returns to IDLE. Any band or bin read starts it again, so a client
that opens an RTA view and polls simply works, and a device nobody is watching
spends nothing. A start counts as a read, so `RTA_CTL_START` is not undone by
the next service pass. With `RTA_FLAG_MANUAL` the run state belongs to
CONTROL entirely: no auto-start on read and no auto-off, so a manual client
that starts the analyser must also stop it.

## 4. Kernel

`rta_fft.h` exposes a platform-neutral kernel used by the engine and by the
host-side test harness. Both platforms implement the same interface:

```c
// Real FFT of n = 1 << order samples, in place, via an n/2-point complex
// transform plus a split pass.  Twiddles come from a flash table sized for
// the largest order; smaller orders stride through it.  Executes one
// bounded unit per call so the caller can limit main-loop latency.
//   returns true when the last stage has completed.
bool rta_fft_step(rta_sample_t *buf, uint8_t order, uint8_t *stage);

// Frequency-domain Hann, bin power, band sums and bin magnitudes.
// bands[]/bins[] receive RTA level bytes (section 2.3).
void rta_fft_finish(const rta_sample_t *buf, uint8_t order,
                    const RtaBandTable *table, float *band_power,
                    uint8_t *bins);
```

`rta_sample_t` is `float` on RP2350 and `int16_t` on RP2040. The RP2040 kernel
scales by one half per stage and keeps bin power in 32-bit integers; its
measured dynamic range is 78.5 dB (reported as 78 in the caps). The RP2350
kernel's is 120 dB. Two real samples packed into one complex slot give a
sqrt(2) full-scale magnitude, so the Q15 path prescales by 1/sqrt(2) and the
split pass restores it; that costs 3 dB of Q15 range.

### 4.1 Generated tables

`scripts/gen_rta_tables.py` writes `firmware/DSPi/rta_tables.h`:

- complex twiddles for the largest supported transform, float and Q15;
- split-pass twiddles;
- band edge tables: for every (sample rate, order) pair, the first and last
  bin index of every band, with empty bands encoded as `lo > hi`;
- the log2 mantissa table and the band centre frequencies in Hz for the caps.

All tables are `const` and stay in flash. The generator is the single source of
truth for the band layout; the host test harness imports it.

### 4.2 Kernel acceptance (host, numpy oracle)

Compiled natively with clang and driven from Python (`tools/rta_test/run.sh`),
for every order and both sample formats:

- A full-scale sine at any frequency reads 0 dBFS within 0.5 dB in the band
  that contains it. On a band edge the Hann main lobe splits across two bands:
  the pair sums to 0 dBFS within 0.5 dB and the better single band never falls
  below -3.05 dB.
- A sine at -60 dBFS reads within 0.2 dB (RP2350) or 1.25 dB (RP2040; 1.5 dB
  where the band sums more of the Q15 floor). The measured worst case is
  1.11 dB at 1024 points.
- With a -100 dBFS sine, every bin more than three bins away reads below the
  platform's dynamic-range floor.
- Pink noise reads flat across bands within 1 dB after averaging 64 frames.
- Bin magnitudes match a numpy Hann FFT of the same input within 0.35 dB
  (RP2350) or 0.75 dB (RP2040) above the floor.

`tools/rta_test/test_engine.py` additionally compiles the real engine against
stubs and checks the protocol, all-channel cadence, bin frame, liveness
recovery, restart, config rejection, and auto-off in both formats.

## 5. Wire protocol

### 5.1 Commands

Vendor commands 0x08 to 0x0F. All GETs are `bmRequestType` 0xC1, all SETs
0x41, following the shared dispatcher, so UART and I2C get them for free.

| Code | Name | Dir | wValue | Payload / response |
|------|------|-----|--------|--------------------|
| 0x08 | `REQ_RTA_SET_CONFIG` | SET | 0 | `RtaConfig` (12 B). STALL on invalid |
| 0x09 | `REQ_RTA_GET_CONFIG` | GET | 0 | `RtaConfig` (12 B) applied config |
| 0x0A | `REQ_RTA_GET_CAPS` | GET | 0 | `RtaCaps` (16 B); 1..n = band centre table chunk |
| 0x0B | `REQ_RTA_GET_BANDS` | GET | channel | `RtaBandFrame` (80 B) for that channel at the current tap |
| 0x0C | `REQ_RTA_GET_BINS` | GET | byte offset | chunk of the bin frame, `wLength` bytes |
| 0x0D | `REQ_RTA_GET_STATUS` | GET | 0 | `RtaStatus` (24 B) |
| 0x0E | `REQ_RTA_CONTROL` | GET | action | one status byte, like `REQ_SIGGEN_CONTROL`. `RTA_CTL_*` |
| 0x0F | `REQ_RTA_GET_BANDS_ALL` | GET | 0 | every live channel's `RtaBandFrame` back to back, USB only |

`REQ_RTA_GET_BANDS`, `GET_BINS` and `GET_BANDS_ALL` count as reads for the
auto-off timer and auto-start the analyser. `GET_STATUS` does not.

`REQ_RTA_GET_BANDS_ALL` is refused on UART and I2C the same way 0xA0 is; those
clients read per channel with 0x0B, which fits their 132-byte GET limit. On
USB it is built in `bulk_param_buf` under the bulk lock, acquired and released
at the same three sites as 0xA0 through `vendor_is_bulk_get()`.

`REQ_RTA_GET_BINS` follows the 0xA2 chunk pattern with `wValue` as the byte
offset into the bin frame. The frame is `16 + n_bins + 1` bytes: the header
carries the sequence number, and the same number sits in the final byte, so a
host that reads several chunks validates that both match and re-reads if the
frame turned over in between. The engine clears the tail to 0xFF before it
writes and sets it last; sequence numbers skip 0xFF. The layout is fixed for
the life of a config. No lock is taken. The bin frame is never larger than
1,041 bytes.

### 5.2 Structures

```c
#define RTA_CFG_VERSION      2
#define RTA_MAX_BANDS        36
#define RTA_LEVEL_ZERO_DBFS  243

#define RTA_TAP_INPUT   0     // after per-input PEQ, before matrix
#define RTA_TAP_OUTPUT  1     // after gain + delay, before encode

#define RTA_FLAG_MANUAL 0x01  // CONTROL owns run state: no auto-start, no auto-off

typedef struct __attribute__((packed)) {
    uint8_t  version;          // RTA_CFG_VERSION
    uint8_t  tap;              // RTA_TAP_*
    uint16_t channel_mask;     // bit i = channel i at that tap; 0 = STALL
    uint8_t  fft_order;        // 8..10 (256 to 1024 points)
    uint8_t  reserved0;
    uint16_t avg_ms;           // power-domain EMA time constant, 0 = none
    uint8_t  peak_decay_db_s;  // 0 = peak hold off, else dB per second
    uint8_t  flags;            // RTA_FLAG_*
    uint16_t reserved;
} RtaConfig;                   // 12 bytes

typedef struct __attribute__((packed)) {
    uint8_t  version;
    uint8_t  input_channels;   // NUM_INPUT_CHANNELS
    uint8_t  output_channels;  // NUM_OUTPUT_CHANNELS
    uint8_t  fft_order_min;    // 8
    uint8_t  fft_order_max;    // 10
    uint8_t  fft_order_default;// 10 on RP2350, 9 on RP2040
    uint8_t  reserved0;
    uint8_t  max_bands;        // RTA_MAX_BANDS
    uint8_t  level_zero;       // RTA_LEVEL_ZERO_DBFS
    uint8_t  dynamic_range_db; // 78 on RP2040 (measured), 120 on RP2350
    uint16_t idle_timeout_ms;  // RTA_IDLE_TIMEOUT_MS
    uint16_t max_bin_frame;    // largest bin frame in bytes (529)
    uint16_t reserved;
} RtaCaps;                     // 16 bytes

// GET_CAPS wValue 1..: uint16 band centre frequencies in Hz, 32 per chunk.

typedef struct __attribute__((packed)) {
    uint8_t  version;
    uint8_t  channel;          // channel index at the current tap
    uint8_t  seq;              // increments per frame of this channel
    uint8_t  n_bands;          // bands valid at the current sample rate
    uint16_t age_ms;           // since this channel's last frame; 0xFFFF = never
    uint16_t reserved;
    uint8_t  avg[RTA_MAX_BANDS];
    uint8_t  peak[RTA_MAX_BANDS];
} RtaBandFrame;                // 80 bytes

typedef struct __attribute__((packed)) {
    uint8_t  version;
    uint8_t  channel;
    uint8_t  seq;              // frame sequence, repeated as the final byte
    uint8_t  fft_order;
    uint32_t sample_rate_hz;
    uint16_t n_bins;           // (1 << fft_order) / 2
    uint16_t reserved[3];
    // uint8_t bins[n_bins]; uint8_t seq_tail;
} RtaBinFrameHeader;           // 16 bytes

#define RTA_STATE_IDLE          0
#define RTA_STATE_CAPTURING     1
#define RTA_STATE_TRANSFORMING  2

typedef struct __attribute__((packed)) {
    uint8_t  version;
    uint8_t  state;            // RTA_STATE_*
    uint8_t  tap;              // applied tap
    uint8_t  channel;          // being captured / transformed, 0xFF = none
    uint8_t  reserved0;
    uint8_t  live_count;       // channels in the rotation right now
    uint16_t live_mask;        // selected AND live
    uint16_t frames_per_s;     // frames completed per second
    uint16_t busy_us_per_s;    // main-loop microseconds per second spent transforming (EMA)
    uint16_t last_frame_us;    // wall time of the last complete transform, for the bench
    uint16_t idle_ms;          // since the last data read; 0xFFFF = never
    uint32_t sample_rate_hz;
    uint8_t  first_band;       // lowest band resolved at this size and rate
    uint8_t  reserved1;
    uint16_t reserved2;
} RtaStatus;                   // 24 bytes

#define RTA_CTL_STOP       0
#define RTA_CTL_START      1
#define RTA_CTL_RESET_AVG  2   // clear averaging and peak hold, keep running
```

`busy_us_per_s` exists because the existing CPU load figure measures only the
packet callback and cannot see main-loop work.

### 5.3 Validation

`REQ_RTA_SET_CONFIG` STALLs on wrong version or length, unknown tap, an
`fft_order` outside the caps range, or an empty `channel_mask` after masking
to the tap's channel count. `avg_ms` is clamped to 0..10000 and
`peak_decay_db_s` to 0..100. A config that is valid but changes tap, mask, or
size restarts the frame (section 3.5). A change of `avg_ms`,
`peak_decay_db_s`, or `flags` alone applies at the next publish.

## 6. Cost

### 6.1 RAM (static, BSS), measured against HEAD

| Item | RP2350 | RP2040 |
|------|--------|--------|
| Capture buffer, 1024 points | 4,096 B | 2,048 B |
| Bin frame (header + 512 + tail) | 529 B | 529 B |
| Per-channel band state: 9 (RP2350) / 5 (RP2040) channels | 2,052 B | 1,140 B |
| Engine state, config, staged config, statistics, response staging | ~170 B | ~170 B |
| **Total BSS** | **6,848 B (~6.7 KB)** | **3,888 B (~3.8 KB)** |
| RAM image (RAM-pinned tap and packet bookkeeping) | ~0.3 KB | ~0.2 KB |

The capture buffer is sized for the largest order whatever the applied
config, which is why the ceiling is 1024 rather than 2048. If RAM ever gets
tight it fits inside the idle 8 KB `bulk_param_buf`, which already has a
claim-and-release lock shared by every transport; that overlay is a follow-up,
not part of this spec.

### 6.2 CPU

| Configuration | RP2350 | RP2040 |
|---------------|--------|--------|
| 1024 points (RP2350 default) | < 0.5% of Core 0 | ~1.7% (about 110k cycles per frame, measured on the host) |
| 512 points (RP2040 default) | < 0.5% | ~1% |
| Tap copy in the audio callback | a few µs per block | a few µs per block |

Everything here runs in the main loop, so an over-budget frame shows up as a
slower refresh, never as an audio fault. The bench figure via
`RtaStatus.last_frame_us` is still pending; on RP2040 the `FINISH` step (a
libgcc count-leading-zeros call per bin) is the item most likely to exceed
the 250 µs step guideline at 1024 points.

### 6.3 Flash

Twiddle and band tables: about 7.5 KB on RP2350 and 4.5 KB on RP2040. Kernel,
engine, and handler code: about 6 KB. The tap and the packet begin/end
bookkeeping are `DSP_TIME_CRITICAL`, because they run inside the meter loops
on both cores during flash writes. The transform, the band sums, and all
control paths stay in flash.

## 7. Files

| File | Content |
|------|---------|
| `firmware/DSPi/rta.h`, `rta.c` | engine: state machine, rotation, tap, publish, averaging, service, auto-off, wire helpers |
| `firmware/DSPi/rta_fft.h`, `rta_fft.c` | kernel: FFT, split, window/power/bands (both platforms) |
| `firmware/DSPi/rta_tables.h` | generated twiddles and band tables |
| `scripts/gen_rta_tables.py` | table generator, single source of truth for band layout |
| `tools/rta_test/` | host harness: numpy oracle for the kernel, stubbed engine smoke test |
| `tools/dspi_test/tests/rta.py` | device tests (section 8) |

Touch points in existing files: `config.h` (command codes), `audio_pipeline.c`
and `pdm_generator.c` (tap calls inside the meter loops, packet begin/end),
`main.c` (`rta_init()`, `rta_service()` in the main loop, `rta_restart()` at
the existing reset sites), `vendor_commands.c` (handlers, `vendor_is_bulk_get`),
`CMakeLists.txt`, `scripts/check_ram_placement.py` (hot symbols).

## 8. Device tests

Using the signal generator on the output tap and USB playback on the input tap:

- A 1 kHz sine at -20 dBFS on one output reads -20 dBFS within 1 dB in the
  1 kHz band of that output and below -60 dBFS in every band two or more away.
- Pink noise on all outputs reads flat within 2 dB across resolved bands after
  2 s of averaging, on every selected channel.
- With all outputs selected, `seq` advances on every one of them and `age_ms`
  never exceeds 1.5x the expected rotation interval.
- `busy_us_per_s` stays under budget at every order.
- With the analyser running, the existing output alignment tests pass
  unchanged.
- Stop polling for 6 s: `state` returns to IDLE. One band read restarts it.
- A chunked bin read validates seq head against tail and re-reads on mismatch.
- An empty mask or an out-of-range order is rejected.

## 9. Deferred

- Transfer-function mode (input versus output, live measured response).
- A third tap point between the matrix and the output EQ.
- A control-surface OLED spectrum page.
- Capture-buffer overlay onto `bulk_param_buf`.
- Block floating point in the Q15 kernel, which would recover up to 6 dB per
  bit on quiet captures (needs an out-parameter on `rta_fft_step`).
- DSPi Console RTA and FFT views (separate repository, matched release).
