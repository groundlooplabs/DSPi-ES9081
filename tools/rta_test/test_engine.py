#!/usr/bin/env python3
"""Host smoke tests of the actual engine, with hardware/time stubs only."""
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]
DSP = ROOT / 'firmware/DSPi'
HARNESS = r'''
#include <assert.h>
#include <math.h>
#include <stdint.h>
#include <stdbool.h>
#define CONFIG_H
#define AUDIO_PIPELINE_H
#define USB_AUDIO_H
#define UPMIX_H
#define DSP_TIME_CRITICAL
#define __not_in_flash_func(x) x
#if PICO_RP2350
#define NUM_INPUT_CHANNELS 8
#define NUM_OUTPUT_CHANNELS 9
#else
#define NUM_INPUT_CHANNELS 2
#define NUM_OUTPUT_CHANNELS 5
#endif
#define NUM_STEREO_INPUTS 2
static struct { uint32_t freq; } audio_state = {48000};
static struct { struct { bool enabled; } outputs[NUM_OUTPUT_CHANNELS]; } matrix_mixer;
typedef struct { uint8_t n_derived; } UpmixCoeffs;
static const UpmixCoeffs *current_upmix_coeffs;
static uint8_t active_input_channel_count(void) { return 2; }
static uint32_t now;
static uint32_t time_us_32(void) { return now; }
static void __dmb(void) {}
#include "rta.c"
static void packet(bool tone) {
    rta_pipe_t buf[48];
    for (int i=0; i<48; ++i) {
        double x = tone ? .5*sin(6.283185307179586*100*((double)now/1000000+i/48000.)) : 0;
#if PICO_RP2350
        buf[i] = x;
#else
        buf[i] = x*268435456.;
#endif
    }
    rta_packet_begin(48);
    for (int ch=0; ch<NUM_OUTPUT_CHANNELS; ++ch)
        if (matrix_mixer.outputs[ch].enabled) rta_tap(RTA_TAP_OUTPUT, ch, buf, 48);
    rta_packet_end(48);
    now += 1000;
    rta_service();
}
int main(void) {
    (void)current_upmix_coeffs;
    for (int ch=0; ch<NUM_OUTPUT_CHANNELS; ++ch) matrix_mixer.outputs[ch].enabled=true;
    rta_init();
    RtaCaps caps; rta_get_caps(&caps);
    assert(caps.version==2 && caps.fft_order_max==11 && caps.max_bin_frame==16+1024+1);
    RtaConfig cfg; rta_get_config(&cfg);
    cfg.version=1; assert(!rta_set_config(&cfg,sizeof(cfg)));
    cfg.version=2; cfg.fft_order=12; assert(!rta_set_config(&cfg,sizeof(cfg)));
    cfg.fft_order=caps.fft_order_default; cfg.avg_ms=0;
    assert(rta_set_config(&cfg,sizeof(cfg))); rta_service();
    rta_note_read(); rta_service();
    for (int i=0;i<300;i++) packet(true);
    for (int ch=0;ch<NUM_OUTPUT_CHANNELS;ch++) {
        RtaBandFrame f; assert(rta_get_band_frame(ch,&f));
        assert(f.age_ms<=400 && f.avg[7]>220);
    }
    uint16_t len; const uint8_t *bytes=rta_bin_frame(&len);
    const RtaBinFrameHeader *h=(const RtaBinFrameHeader *)bytes;
    assert(len==16+h->n_bins+1 && h->n_bins==(1u<<caps.fft_order_default)/2);
    assert(h->seq==bytes[len-1] && h->seq!=255);
    RtaStatus st; rta_get_status(&st); assert(st.state!=RTA_STATE_IDLE && st.first_band!=0xFF);
    // A channel that goes dead mid-fill must not stall the rotation.
    matrix_mixer.outputs[0].enabled=false;
    for (int i=0;i<300;i++) packet(false);
    matrix_mixer.outputs[0].enabled=true;
    for (int i=0;i<300;i++) packet(false);
    RtaBandFrame f; rta_get_band_frame(0,&f); assert(f.avg[7]==0 && f.age_ms<=400);
    // Reset and rate/source/preset restart use the existing common restart hook.
    rta_restart(); rta_service();
    for (int i=0;i<300;i++) packet(false);
    for (int ch=0;ch<NUM_OUTPUT_CHANNELS;ch++) {
        rta_get_band_frame(ch,&f); assert(f.avg[7]==0 && f.age_ms<=400);
    }
    cfg.fft_order=11; assert(rta_set_config(&cfg,sizeof(cfg))); rta_service();
    for (int i=0;i<600;i++) packet(true);
    rta_get_band_frame(0,&f); assert(f.n_bands==31 && f.avg[7]>220);
    bytes=rta_bin_frame(&len); assert(len==16+1024+1);
    now+=6000000; rta_service(); rta_get_status(&st); assert(st.state==RTA_STATE_IDLE);
    return 0;
}
'''
with tempfile.TemporaryDirectory() as temp:
    tmp = Path(temp)
    for name in ('pico/time.h', 'hardware/sync.h'):
        p = tmp / name; p.parent.mkdir(exist_ok=True); p.write_text('')
    source = tmp / 'engine.c'; source.write_text(HARNESS)
    for flt in (0, 1):
        exe = tmp / ('engine%d' % flt)
        subprocess.run(['clang','-O2','-Wall','-Wextra','-Werror',
                        '-DRTA_HOST','-DPICO_RP2350=%d'%flt,'-DRTA_SAMPLE_FLOAT=%d'%flt,
                        '-I'+str(tmp),'-I'+str(DSP),str(source),
                        str(DSP/'rta_fft.c'),'-o',str(exe)],check=True)
        subprocess.run([str(exe)],check=True)
        print('PASS engine %s: protocol, all-channel cadence, bins, liveness, restart, off, timeout' % ('float' if flt else 'Q28'))
