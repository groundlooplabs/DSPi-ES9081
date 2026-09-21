#ifndef ES9081_H
#define ES9081_H

#include <stdint.h>
#include <stdbool.h>
#include "pico/stdlib.h"
#include "hardware/i2c.h"

// Default ES9081 I2C Address (7-bit address 0x48)
#ifndef ES9081_I2C_ADDR
#define ES9081_I2C_ADDR             0x48
#endif

// ES9081 Register Definitions
#define ES9081_REG_SYS_CONFIG       0x00    // Register 0 - Sys Config
#define ES9081_REG_CLOCK_CONTROL    0x02    // Register 2 - Clock Control
#define ES9081_REG_TDM_CONFIG_2     0x07    // Register 7 - TDM / Frame Config
#define ES9081_REG_PLL_CLOCK_SEL    0x79    // Register 121 - PLL Clock Select
#define ES9081_REG_PLL_VCO          0x7A    // Register 122 - PLL VCO & CP
#define ES9081_REG_PLL_REGULATOR    0x7B    // Register 123 - PLL Regulator
#define ES9081_REG_PLL_IN_OUT_DIV   0x80    // Register 128 - PLL IN & OUT DIV

// System Configuration Flags
#define ES9081_ENABLE_DAC           (1 << 0)

/**
 * @brief Write a single byte to an ES9081 register over I2C.
 */
static inline void es9081_write_reg(i2c_inst_t *i2c, uint8_t reg, uint8_t val) {
    uint8_t buf[2] = {reg, val};
    i2c_write_blocking(i2c, ES9081_I2C_ADDR, buf, 2, false);
}

/**
 * @brief Configure the internal APLL to derive MCLK from BCK.
 */
static inline void es9081_config_apll(i2c_inst_t *i2c) {
    // Step 1: Enable the PLL internal regulator
    es9081_write_reg(i2c, ES9081_REG_PLL_REGULATOR, 0x0A);

    // Step 2: Enable CP, VCO, clock sampler, and release digital reset
    es9081_write_reg(i2c, ES9081_REG_PLL_VCO, 0x3F);

    // Step 3: Strobe PLL_FB_DIV_LOAD to latch FBDIV
    es9081_write_reg(i2c, ES9081_REG_PLL_IN_OUT_DIV, 0x32); // LOAD = 1
    es9081_write_reg(i2c, ES9081_REG_PLL_IN_OUT_DIV, 0x30); // LOAD = 0 (latch edge)

    // Step 4: Route DATA_CLK (BCK) into APLL, route APLL into digital core as MCLK
    es9081_write_reg(i2c, ES9081_REG_PLL_CLOCK_SEL, 0x2D);

    // Step 5: Enable DAC path
    es9081_write_reg(i2c, ES9081_REG_SYS_CONFIG, ES9081_ENABLE_DAC);
}

/**
 * @brief Configure ES9081 for 256fs MCLK (external MCLK input).
 */
static inline void es9081_config_256fs(i2c_inst_t *i2c) {
    // Clock Control: Set 24.576MHz base rate family and MCLK_128FS divider
    es9081_write_reg(i2c, ES9081_REG_CLOCK_CONTROL, 0x01);

    // Enable Auto FS Detection and DAC Interpolation path
    es9081_write_reg(i2c, ES9081_REG_SYS_CONFIG, 0x05);
}

/**
 * @brief Configure default DAC mode (512fs MCLK, standard interpolation).
 */
static inline void es9081_config_default(i2c_inst_t *i2c) {
    es9081_write_reg(i2c, ES9081_REG_SYS_CONFIG, ES9081_ENABLE_DAC);
}

/**
 * @brief Initialize I2C peripheral and perform staged hardware enable sequence.
 *
 * @param i2c I2C instance (e.g., i2c0 or i2c1)
 * @param sda_pin GPIO pin number for I2C SDA
 * @param scl_pin GPIO pin number for I2C SCL
 * @param chip_en_pin GPIO pin connected to ES9081 CHIP_EN
 */
static inline void es9081_init(i2c_inst_t *i2c, uint sda_pin, uint scl_pin, uint chip_en_pin) {
    // Initialize I2C peripheral at 100 kHz
    i2c_init(i2c, 100 * 1000);
    gpio_set_function(sda_pin, GPIO_FUNC_I2C);
    gpio_set_function(scl_pin, GPIO_FUNC_I2C);
    gpio_pull_up(sda_pin);
    gpio_pull_up(scl_pin);

    // Initialize CHIP_EN pin and enforce soft-start delay sequence
    gpio_init(chip_en_pin);
    gpio_set_dir(chip_en_pin, GPIO_OUT);
    gpio_put(chip_en_pin, 0);

    // Allow supply rails to stabilize before enabling (>200us required)
    sleep_ms(50);

    gpio_put(chip_en_pin, 1);
    sleep_ms(20);
}

#endif // ES9081_H
