# Control Surfaces: Auxiliary Outputs

*Spec version 1; caps v17, directory V20. Companion to
`control_surfaces_spec.md`, which remains authoritative for bindings,
components, actions and the validity model.*

Auxiliary outputs are eight device-global user values the firmware attaches
no meaning to. Each is an on/off `state` and a 0..100 `level`. Nothing in the
audio path reads them. They exist so a front panel can switch and dim things
the firmware does not know about. An amplifier trigger, a speaker relay, a
panel lamp, a fan. The device does not claim a GPIO for an aux output. A pin
follows one only when the user binds an ordinary LED or PWM LED to the aux
noun, which means every existing indicator feature (invert, on/off delays,
brightness ceiling, custom range) applies unchanged.

## 1. Concepts

### 1.1 What an aux output is

Eight slots, indexed 0-7, each holding two runtime values and one config
record. The runtime values are `state` (0/1) and `level` (0-100 %). They live
in RAM only, change instantly, and are never written to flash on change. The
config record carries a 32-byte name and what the output does at boot.

Two nouns address them, both using the new target kind `CS_TARGET_AUX` (5)
with `target` = aux index 0-7 and `index` = 0.

| Noun | Value | Kind | Unit | Actions |
|------|-------|------|------|---------|
| `CS_NOUN_AUX` | 68 | BOOL | - | BOOL-RW (`TOGGLE`, `SET`, `FOLLOW`, `MOMENTARY`, `IND_EQUALS`) |
| `CS_NOUN_AUX_LEVEL` | 69 | CONT | PERCENT | CONT-RW (`ADJUST`, `STEP`, `INC`, `DEC`, `SET`, `IND_ABOVE`, `IND_LEVEL`) |

### 1.2 How a physical pin follows one

An aux output is a value, not a pin. To drive hardware from it, bind an
output component to the noun in a second slot:

- `CS_TYPE_LED` + `IND_EQUALS` with `value = 1` drives a plain GPIO high
  while the output is on. `CS_FLAG_INVERT` makes it active-low, which is what
  most relay and MOSFET boards want. `on_delay` / `off_delay` work normally,
  so a trigger can be given a warm-up or a hold-off.
- `CS_TYPE_LED_PWM` + `IND_LEVEL` on `CS_NOUN_AUX_LEVEL` dims a load from the
  level percentage. The existing squared perceptual curve, the `base_bright`
  ceiling and the `range_min` / `range_max` span all apply unchanged.

The two halves are independent. An aux output with no LED bound to it is
still a perfectly usable value. A host can read it, a display page can show
it, and a macro can set it.

### 1.3 What can drive one

Anything that already drives a noun. A button (`TOGGLE`, `MOMENTARY`), a
switch (`FOLLOW`), an encoder or pot on the level, an IR command, a macro
step, or a display page in edit mode. Hosts write the values directly with
the commands in section 3.

Typical uses:

- **Amplifier trigger relay.** A button toggles aux 0. An LED binding with
  `INVERT` drives an opto-relay module that closes the amp's 12 V trigger.
- **Speaker A/B.** One aux, one relay, one panel button. `IND_EQUALS`
  with `value = 1` on one LED and `value = 0` on a second LED gives an A/B
  indicator pair for free.
- **Panel lamp dimmer.** An encoder on `AUX_LEVEL` with a PWM LED following
  it, so the lamp brightness is a stored, named front-panel control.
- **Momentary pulse.** A button with `MOMENTARY` and `value = 1` holds the
  output on only while pressed, which is the shape a garage-door style pulse
  input expects.

### 1.4 What aux nouns do not do

- **No groups.** A binding that sets `CS_FLAG_GROUP` on an aux noun is
  rejected with `CS_STATUS_INVALID_GROUP`, and a `CsGroup` created with
  `target_kind = 5` is rejected the same way. Groups exist for channel
  spaces; the eight aux outputs are already a flat set a macro can cover.
- **No `CYCLE_ALL` display pages.** That mode walks untargeted nouns only, so
  aux outputs appear on the display as explicit pages.

---

## 2. Wire reference

### 2.1 `CsAuxCfg` (36 bytes)

Identical on the wire (`REQ_SET`/`GET_CS_AUX_CFG` payload) and in flash.

| Off | Size | Field | Meaning |
|----|------|-------|---------|
| 0 | 1 | `boot_mode` | 0 = `CS_AUX_BOOT_FIXED`, 1 = `CS_AUX_BOOT_SAVED` |
| 1 | 1 | `boot_state` | 0/1, applied at boot |
| 2 | 1 | `boot_level` | 0..100, applied at boot |
| 3 | 1 | `reserved` | must be 0 |
| 4 | 32 | `name[32]` | NUL-terminated user label (`CS_NAME_LEN`) |

An all-zero record is the safe default. It reads as off, 0 %, unnamed and
boot-fixed.

### 2.2 `CsAuxConfig` (292 bytes, flash)

| Off | Size | Field | Meaning |
|----|------|-------|---------|
| 0 | 1 | `version` | `CS_AUX_CONFIG_VERSION` (1) |
| 1 | 3 | `reserved[3]` | 0 |
| 4 | 288 | `aux[8]` | eight `CsAuxCfg` |

The whole block all-zero means every output off, 0 %, unnamed and
boot-fixed, so a fresh or migrated directory needs no seeding.

### 2.3 Boot modes

- **`CS_AUX_BOOT_FIXED` (0).** The output boots to whatever `boot_state` and
  `boot_level` the host stored. Use it for an output that must always come up
  in a known position (an amp trigger that should start off).
- **`CS_AUX_BOOT_SAVED` (1).** `REQ_CS_SAVE` copies the *live* state and level
  into `boot_state` / `boot_level` before persisting, so the output comes back
  the way it was at the last save.

Both modes boot from the same two fields; the mode only decides whether a
save rewrites them.

### 2.4 Caps v17

`caps_version` reads 17. No header field is added or moved: a host learns
the aux outputs exist from the version, and learns there are eight of them
from `target_count` in the descriptors for nouns 68 and 69. There is no
`max_aux` field.

---

## 3. Commands (`0x02`-`0x07`)

| Command | Code | Dir | wValue | wLength / payload | Response |
|---------|------|-----|--------|-------------------|----------|
| `REQ_SET_CS_AUX_CFG` | `0x02` | OUT | aux (0-7) | 36-byte `CsAuxCfg` | none (deferred; poll `0x87`) |
| `REQ_GET_CS_AUX_CFG` | `0x03` | IN | aux (0-7) | - | 36-byte `CsAuxCfg` (live) |
| `REQ_SET_CS_AUX_STATE` | `0x04` | OUT | aux (0-7) | 1 byte (non-zero = on) | none (applied immediately) |
| `REQ_GET_CS_AUX_STATE` | `0x05` | IN | aux (0-7), or `0xFFFF` | - | 1 byte, or 16 bytes |
| `REQ_SET_CS_AUX_LEVEL` | `0x06` | OUT | aux (0-7) | 1 byte 0..100 | none (applied immediately) |
| `REQ_GET_CS_AUX_LEVEL` | `0x07` | IN | aux (0-7) | - | 1 byte |

All six work over USB, UART and I2C, and are the same commands the engine
itself dispatches (section 6.1).

### 3.1 Config SET (`0x02`)

Deferred to the main loop exactly like `REQ_SET_CS_GROUP`. The apply is a
live-only preview under the shared Control Surfaces dirty flag; `REQ_CS_SAVE`
persists it and `REQ_CS_REVERT` discards it. The result lands in the shared
status channel read by `REQ_GET_CS_STATUS`, with `cs_last_slot` tagged
`0x70 | aux` to keep aux slots distinct from plain binding slots and from
groups (`0x40`), display (`0x50`), macros (`0x60`) and IR sub-slots (`0x80`).

| Status | Cause |
|--------|-------|
| `CS_STATUS_INVALID_AUX` (`0x26`) | aux index >= 8 |
| `CS_STATUS_BUSY` (`0x1B`) | a previous cfg SET is still queued for apply |
| `CS_STATUS_INVALID_VALUE` (`0x14`) | payload shorter than 36 bytes, `boot_mode` > 1, `boot_state` > 1, `boot_level` > 100, or `reserved` != 0 |
| `CS_STATUS_PENDING` (`0x16`) | accepted; the main-loop apply has not run yet |
| `PIN_CONFIG_SUCCESS` (`0x00`) | applied |

A validation failure leaves the stored record intact; nothing is
half-written.

### 3.2 Runtime SET (`0x04`, `0x06`)

Applied in the handler, with no flash write, no dirty flag and no deferral.
`0x04` treats any non-zero byte as on. `0x06` clamps values above 100 to 100.
A bad index or an empty payload stalls the control transfer (external
transports get `CTRL_DISPATCH_ERROR`) and also records
`CS_STATUS_INVALID_AUX` with slot `0x70 | (aux & 0x0F)` in the CS status
channel, so a host polling `REQ_GET_CS_STATUS` sees it either way. A write
that leaves the value unchanged is accepted silently and sends no
notification, matching how parameter writes behave elsewhere.

### 3.3 Bulk state read (`0x05` with `wValue = 0xFFFF`)

Returns 16 bytes: `state[0..7]` followed by `level[0..7]`. One transfer gives
a host its whole initial aux picture, after which the notification in
section 5 keeps it current.

---

## 4. Boot, save and revert

**Boot.** Every slot's `state` and `level` are set from `boot_state` and
`boot_level`, in both boot modes. A stored record that fails validation is
zeroed first, so a corrupt slot boots off at 0 %. There is no per-slot status
report for this (aux slots have no "down" state), so a host that cares should
read the record back after boot.

**Save (`REQ_CS_SAVE`).** Before the directory is written, every
`CS_AUX_BOOT_SAVED` slot copies its live `state` and `level` into
`boot_state` / `boot_level`. `FIXED` slots are left alone. The aux table then
persists with the rest of the Control Surfaces config in the same single
directory write.

**Revert (`REQ_CS_REVERT`).** Reloads the stored config (names, boot mode and
boot values) and does **not** touch the live `state` or `level`. Those are
runtime values, not configuration, and a revert must not click a relay or
blink a lamp. For the same reason the live values are not part of the dirty
flag: toggling an output never makes the config look unsaved. One consequence
follows for `SAVED` slots. A save can change the stored boot values while the
dirty flag reads clear, and a later revert restores those newly saved values,
not the ones from before the save. Hosts that show "unsaved changes" should
treat `SAVED` slots as always potentially different from flash.

**Never on change.** There is deliberately no "remember on every change"
mode. A flash write freezes the audio clocks for roughly 44 ms, so a mode
that persisted on every toggle would put a flash write behind a front-panel
button. Boot-saved covers the same intent at save time instead.

---

## 5. Notification (`NOTIFY_EVT_CS_AUX`, `0x0C`)

Every `0x04` and `0x06` dispatch that changes the value pushes an 8-byte packet:

```
[ver=2, evt=0x0C, flags=0, seq, aux, state, level, src]
```

Both values are carried on every event, so a host never has to read back to
learn which of the pair moved. `src` is the `ParamSource` of the dispatch.
`PARAM_SRC_GPIO` (5) means a bound control, IR command or macro step,
`PARAM_SRC_HOST_SET` (1) a USB write, and `PARAM_SRC_UART` (8) /
`PARAM_SRC_I2C` (9) the external transports. This is how the Console learns
that a panel button changed an aux output.

Config changes (`0x02`) push nothing; they follow the existing poll-the-status
model of the other Control Surfaces SETs.

---

## 6. Engine and display behaviour

### 6.1 Dispatch

`CS_NOUN_AUX` and `CS_NOUN_AUX_LEVEL` go through
`vendor_dispatch_set(CTRL_SOURCE_GPIO, ...)` with `0x04` / `0x06`, exactly
like every other noun. Nothing about aux outputs bypasses the command
surface, which is why a panel toggle produces the same notification a host
write does, tagged as GPIO. The level is rounded to whole percent on the way
out, never truncated, so a pot sweep does not sit a percent low.

Reading a value back (for an indicator, a display page or `IND_ABOVE`) is a
single-byte load from the live array, so aux nouns cost nothing in the 8 ms
indicator tick.

### 6.2 Validation

`target` must be < 8 and `index` must be 0, else `CS_STATUS_INVALID_TARGET`.
Grouped references are rejected with `CS_STATUS_INVALID_GROUP` (1.4). Action
masks are the standard BOOL-RW and CONT-RW sets, so the usual type/noun
intersection rules apply: an encoder on `CS_NOUN_AUX` is rejected, a pot on
`CS_NOUN_AUX_LEVEL` is fine.

`AUX_LEVEL` is whole percent, so a binding, IR command or macro step on it
must use a whole-percent `step` (a multiple of 256 in 8.8, or 0 for the
default 1 %). Fractional steps are refused with `CS_STATUS_INVALID_VALUE`
because they would round back onto the live value on one side and stall.

### 6.3 Display

An aux page's label is the slot's name when one is set, otherwise `Aux N`
with N = target + 1. A level page appends ` Level` (` Lvl` on the tight
two-row bar layout) so the two pages of one output stay distinguishable.
Values render as `On` / `Off` for the bool noun and `NN%` for the level,
which supports the caps v13 level bar. Both nouns are writable, so a page
can be edited from the panel through `DISPLAY_EDIT` / `PAGE_VALUE`, and a
host-driven change pops the event overlay like any other watched page.

---

## 7. Wiring examples

### 7.1 Amp trigger relay on a button

| Slot | Type | Noun | Action | Fields |
|------|------|------|--------|--------|
| 0 | `BUTTON` (GPIO 16) | `AUX` (68) | `TOGGLE` | `target = 0`, `event = CS_EVT_PRESS` |
| 1 | `LED` (GPIO 21) | `AUX` (68) | `IND_EQUALS` | `target = 0`, `value = 1`, `CS_FLAG_INVERT` for an active-low relay board |

Name aux 0 "Amp" through `REQ_SET_CS_AUX_CFG` and set
`boot_mode = CS_AUX_BOOT_FIXED`, `boot_state = 0` so the amp never wakes up
with the device. Add an `off_delay` on the LED binding if the relay should
hold in through a quick power blip.

### 7.2 Panel lamp dimmer on an encoder

| Slot | Type | Noun | Action | Fields |
|------|------|------|--------|--------|
| 2 | `ENCODER` (GPIO 18/19) | `AUX_LEVEL` (69) | `STEP` | `target = 1`, `step = 5 %` |
| 3 | `LED_PWM` (GPIO 22) | `AUX_LEVEL` (69) | `IND_LEVEL` | `target = 1`, optional `range_min`/`range_max` to keep the lamp inside its usable span, `base_bright` to cap the top |

Set aux 1 to `CS_AUX_BOOT_SAVED` so the lamp returns to the brightness it had
at the last save.

### 7.3 Aux as a menu item

Add a display page with `noun = 68`, `target = 0` and a second with
`noun = 69`, `target = 0`, then bind a button to `PAGE_VALUE` with `TOGGLE`
(or an encoder with `STEP`). The pages read "Amp On" and "Amp Level 40%" if
aux 0 is named "Amp", and the same button edits whichever page is shown.

---

## 8. Electrical notes

A Pico GPIO is a 3.3 V pin that can source a few milliamps. It can drive an
LED through a resistor and the gate of a small MOSFET, and nothing else.
Anything real (a relay coil, an amplifier trigger, a lamp, a fan) needs a
logic-level MOSFET, a transistor with a flyback diode, or an opto-isolated
relay module between the pin and the load. Most such modules are active-low,
which is what `CS_FLAG_INVERT` is for.

Two further cautions belong in a host UI next to these controls:

- A PWM-dimmed load sharing the 3.3 V rail can couple switching noise into
  the DAC. Power the load from its own supply and use the GPIO only as a
  control signal.
- Amplifier trigger inputs vary. Some expect 12 V, some a dry contact. A
  3.3 V pin is not a trigger output on its own.

---

## 9. Persistence and migration (directory V20)

`PresetDirectory` appends a 292-byte `CsAuxConfig` after `cs_display`, and
`DIR_VERSION_CURRENT` becomes 20. The block is board-level like the rest of
the Control Surfaces config: it survives preset changes and a factory reset,
and it is not part of `WireBulkParams`.

The V19 layout is frozen as `PresetDirectory_v19` (3035 bytes, pinned by a
`_Static_assert` alongside the `cs_display` offset check), so the V19->V20
step is a prefix copy with the new blob left zeroed, which reads as every
output off, unnamed and boot-fixed. Every older migration reaches V20 the
same way.

`dir_sanitize_cs_aux()` runs on every load. An implausible blob version or a
dirty reserved field resets the whole table; a record with `boot_mode` > 1,
`boot_state` > 1, `boot_level` > 100 or a non-zero `reserved` is cleared to
the safe default, and every name is force-terminated so hand-edited flash
cannot leak an unterminated string to a `REQ_GET_CS_AUX_CFG` reader.

The live directory is now 3327 bytes of the 4096-byte sector, so the flash
layout is unchanged. RAM cost is roughly 650 B of BSS on both platforms. That
is the 292-byte live table, the same again in the `dir_cache` mirror, 16 bytes
of state and level, and the 36-byte deferred SET handoff. No new code sits on a
hot path, and the audio path and output slot alignment are untouched.
