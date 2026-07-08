/* Pocket Lab-Bench Power Supply
* Created by "Ben Makes Everything"
* Youtube video about this project: https://www.youtube.com/watch?v=i2HRpcJS6Vk
* Written for use on ATTiny 3216 MCU - program using UPDI via pin header on PCB
* The PCB designed for this project is meant for use with 4S packs. DO NOT USE with lower-cell count, unless adjustments are made
* Revision date: July 1, 2026
* This version of the code does not feature battery "fuel-gauging", that will be added later
* AI Disclaimer: Yes, this code was partially written using generative AI. However, I have tested it extensively with no issues so far.
* Safety Disclaimer: Litium Ion-Batteries can be dangerous. Use at your own risk.
*/

#include <Wire.h>
#include <EEPROM.h>
#include <LedDisplay.h>


// Pin assignments - ATtiny3216
// Display Pins
#define HCMS_DATA_PIN     PIN_PA1
#define HCMS_CS_PIN       PIN_PA2
#define HCMS_CLOCK_PIN    PIN_PA3
#define HCMS_ENABLE_PIN   PIN_PA4
#define HCMS_RESET_PIN    PIN_PA5
// Encoder Pins
#define ENC_A_PIN         PIN_PA6
#define ENC_B_PIN         PIN_PA7
#define ENC_SW_PIN        PIN_PB3
// I2C Pins
#define I2C_SCL_PIN       PIN_PB0
#define I2C_SDA_PIN       PIN_PB1
// Power Switch Pin
#define SWITCH_STATE_PIN  PIN_PB2   // HIGH = main output OFF, LOW = main output ON

// Character length (8 for HCMS297X models)
#define DISPLAY_LENGTH    8

LedDisplay myDisplay = LedDisplay(
  HCMS_DATA_PIN,
  HCMS_CS_PIN,
  HCMS_CLOCK_PIN,
  HCMS_ENABLE_PIN,
  HCMS_RESET_PIN,
  DISPLAY_LENGTH
);

// ----------------------------------------------------
// TPS55289 settings
// ----------------------------------------------------
#define TPS_ADDR_1 0x74
#define TPS_ADDR_2 0x75

uint8_t tpsAddr = TPS_ADDR_1;
bool tpsFound = false;

#define REG_REF_LSB     0x00
#define REG_REF_MSB     0x01
#define REG_IOUT_LIMIT  0x02
#define REG_VOUT_SR     0x03
#define REG_VOUT_FS     0x04
#define REG_MODE        0x06
#define REG_STATUS      0x07

// TPS55289 operating configuration:
// 0x31 = longest OCP response delay (~12.3 ms), 2.5 mV/us VOUT slew
// 0x82 = output enabled, forced-PWM enabled
#define TPS_VOUT_SR_VALUE     0x31
#define TPS_MODE_VALUE        0x82

// ----------------------------------------------------
// INA226 settings
// ----------------------------------------------------
#define INA_BATTERY_ADDR  0x40   // binary 1000000
#define INA_OUTPUT_ADDR   0x41   // binary 1000001

#define INA_REG_SHUNT_VOLTAGE 0x01
#define INA_REG_BUS_VOLTAGE   0x02

// Resistor Values: 
// 10 milliohms = 0.010 ohms
const float BATTERY_SHUNT_OHMS = 0.01;
const float OUTPUT_SHUNT_OHMS  = 0.01;

const float SHUNT_LSB_VOLTS = 0.0000025; // 2.5 uV per bit
const float BUS_LSB_VOLTS   = 0.00125;   // 1.25 mV per bit

const bool INVERT_BATTERY_CURRENT = false;
const bool INVERT_OUTPUT_CURRENT  = false;

const bool SHOW_DRAW_AS_POSITIVE = true;

// ----------------------------------------------------
// Charging display settings
// ----------------------------------------------------
// If true, the normal display loop is replaced with:
// Charging -> battery voltage -> repeat (when net current flows into battery)
const bool SHOW_CHARGING_STATUS = true;
const bool BATTERY_CHARGING_CURRENT_IS_POSITIVE = false;

// Ignore small currents/noise around zero so the display does not flicker
const float BATTERY_CHARGING_CURRENT_THRESHOLD_A = 0.050; // 50 mA

// ----------------------------------------------------
// Output voltage settings
// ----------------------------------------------------
// Initial startup voltage - the device will power on with this number (in mV)
const int STARTUP_MV = 5000; //5V by default, can be changed

// Do you want it to remember the last voltage set before power-off?
// false = default to STARTUP_MV
// true  = save the selected voltage to EEPROM on power-off and restore it on startup
const bool REMEMBER_OUTPUT_VOLTAGE_ON_POWER_OFF = false;

int appliedTargetMv = STARTUP_MV;

//Minimum voltage the system can be set to - Regulator lower limit is 800mV
const int MIN_MV = 1000;
//Maximum voltage the system can be set to - Regulator upper limit is 2200mV
const int MAX_MV = 20000;
const int STEP_MV = 100;

// EEPROM storage for the optional remembered output voltage
// Save only on power-off event
const int EEPROM_MAGIC_ADDR = 0;
const int EEPROM_VOLTAGE_ADDR = EEPROM_MAGIC_ADDR + sizeof(uint16_t);
const uint16_t EEPROM_MAGIC_VALUE = 0x5529;

// Default current-limit setting (The TPS55289 uses 50mA  steps)
const int DEFAULT_CURRENT_LIMIT_MA = 5000;
// The lower-end of the current limit feature
const int MIN_CURRENT_LIMIT_MA = 100;
//The maximum current allowed - theoretical max is 6.35A (6350mA). But this is likely more than the board can handle
const int MAX_CURRENT_LIMIT_MA = 5000;
//Step size per click of the wheel
const int CURRENT_LIMIT_STEP_MA = 50;

int appliedCurrentLimitMa = DEFAULT_CURRENT_LIMIT_MA;

// false = encoder adjusts voltage; true = encoder adjusts current limit.
bool adjustingCurrentLimit = false;

// ----------------------------------------------------
// Encoder state
// ----------------------------------------------------
int lastEncState = 0;
int encAccum = 0;

bool lastButtonState = HIGH;
bool previousStableButtonState = HIGH;
unsigned long lastButtonChangeMs = 0;
const unsigned long BUTTON_DEBOUNCE_MS = 40;

// ----------------------------------------------------
// Display timing
// ----------------------------------------------------
const unsigned long KNOB_DISPLAY_TIMEOUT_MS = 1500;
const unsigned long NORMAL_DISPLAY_INTERVAL_MS = 2000;

unsigned long knobDisplayUntil = 0;
unsigned long lastNormalDisplayChange = 0;

int normalDisplayMode = 0;
bool chargingDisplayMode = false; // false = battery voltage, true = "Charging"

// Latched TPS55289 fault flags for debugging
// Reading REG_STATUS clears the TPS55289 fault bits, so save them here
uint8_t latchedTpsFaults = 0;
unsigned long lastTpsStatusCheckMs = 0;
const unsigned long TPS_STATUS_CHECK_INTERVAL_MS = 20;

// Normal display loop:
// 0 = battery voltage
// 1 = real output voltage
// 2 = connected load current from output INA226
// 3 = total system power draw from battery INA226 (Watts, includes display, sensors, etc.)

// ----------------------------------------------------
// Main switch state
// ----------------------------------------------------
bool mainOutputIsOn() {
  return digitalRead(SWITCH_STATE_PIN) == LOW;
}

// ----------------------------------------------------
// Basic I2C helpers
// ----------------------------------------------------
bool writeReg8(uint8_t addr, uint8_t reg, uint8_t value) {
  Wire.beginTransmission(addr);
  Wire.write(reg);
  Wire.write(value);
  return Wire.endTransmission() == 0;
}

bool readReg8(uint8_t addr, uint8_t reg, uint8_t &value) {
  Wire.beginTransmission(addr);
  Wire.write(reg);

  if (Wire.endTransmission(false) != 0) {
    return false;
  }

  Wire.requestFrom(addr, (uint8_t)1);

  if (Wire.available() < 1) {
    return false;
  }

  value = Wire.read();
  return true;
}

bool readReg16(uint8_t addr, uint8_t reg, int16_t &value) {
  Wire.beginTransmission(addr);
  Wire.write(reg);

  if (Wire.endTransmission(false) != 0) {
    return false;
  }

  Wire.requestFrom(addr, (uint8_t)2);

  if (Wire.available() < 2) {
    return false;
  }

  uint8_t msb = Wire.read();
  uint8_t lsb = Wire.read();

  value = (int16_t)((msb << 8) | lsb);
  return true;
}

// ----------------------------------------------------
// HCMS display helpers
// ----------------------------------------------------
void hcmsPinsHiZ() {
  pinMode(HCMS_DATA_PIN, INPUT);
  pinMode(HCMS_CS_PIN, INPUT);
  pinMode(HCMS_CLOCK_PIN, INPUT);
  pinMode(HCMS_ENABLE_PIN, INPUT);
  pinMode(HCMS_RESET_PIN, INPUT);
}

void hcmsBegin() {
  pinMode(HCMS_DATA_PIN, OUTPUT);
  pinMode(HCMS_CS_PIN, OUTPUT);
  pinMode(HCMS_CLOCK_PIN, OUTPUT);
  pinMode(HCMS_ENABLE_PIN, OUTPUT);
  pinMode(HCMS_RESET_PIN, OUTPUT);

  myDisplay.begin();
  myDisplay.clear();
  myDisplay.home();
}

void showText(const char *msg) {
  if (!mainOutputIsOn()) {
    return;
  }

  myDisplay.clear();
  myDisplay.home();
  myDisplay.print(msg);
}

// Draws an 8-character startup loading bar from left to right.
void showLoadingBar(uint8_t filledBlocks) {
  if (!mainOutputIsOn()) {
    return;
  }

  filledBlocks = constrain(filledBlocks, 0, DISPLAY_LENGTH);

  char bar[DISPLAY_LENGTH + 1];

  for (uint8_t i = 0; i < DISPLAY_LENGTH; i++) {
    bar[i] = (i < filledBlocks) ? '>' : ' ';
  }

  bar[DISPLAY_LENGTH] = '\0';
  showText(bar);
}

void showVoltagePrefix(char prefix, float volts) {
  char numberBuffer[8];
  char displayString[9];

  dtostrf(volts, 4, 1, numberBuffer);

  displayString[0] = prefix;
  displayString[1] = ':';
  displayString[2] = ' ';
  displayString[3] = numberBuffer[0];
  displayString[4] = numberBuffer[1];
  displayString[5] = numberBuffer[2];
  displayString[6] = numberBuffer[3];
  displayString[7] = 'V';
  displayString[8] = '\0';

  showText(displayString);
}

void showSetVoltage() {
  showVoltagePrefix('S', appliedTargetMv / 1000.0);
}

void showAppliedVoltage() {
  showVoltagePrefix('A', appliedTargetMv / 1000.0);
}

void showCurrentLimit() {
  char displayString[9];

  int wholeAmps = appliedCurrentLimitMa / 1000;
  int hundredths = (appliedCurrentLimitMa % 1000) / 10;

  snprintf(
    displayString,
    sizeof(displayString),
    "I:%d.%02dA",
    wholeAmps,
    hundredths
  );

  showText(displayString);
}

void showBatteryVoltage(float volts) {
  showVoltagePrefix('B', volts);
}

void showOutputVoltage(float volts) {
  showVoltagePrefix('O', volts);
}

void showLoadCurrent(float amps) {
  char displayString[9];

  if (SHOW_DRAW_AS_POSITIVE && amps < 0) {
    amps = -amps;
  }

  if (amps < 1.0) {
    int milliamps = round(amps * 1000.0);

    // Example - L:250mA
    snprintf(displayString, sizeof(displayString), "L:%3dmA", milliamps);
  } else {
    // Example - L:2.5A
    char numberBuffer[8];
    dtostrf(amps, 3, 1, numberBuffer);

    displayString[0] = 'L';
    displayString[1] = ':';
    displayString[2] = numberBuffer[0];
    displayString[3] = numberBuffer[1];
    displayString[4] = numberBuffer[2];
    displayString[5] = 'A';
    displayString[6] = '\0';
  }

  showText(displayString);
}

void showTotalPower(float watts) {
  char numberBuffer[8];
  char displayString[9];

  if (SHOW_DRAW_AS_POSITIVE && watts < 0) {
    watts = -watts;
  }


  // Example: (P: 0.5W, P:12.3W, etc)
  dtostrf(watts, 4, 1, numberBuffer);

  displayString[0] = 'P';
  displayString[1] = ':';
  displayString[2] = numberBuffer[0];
  displayString[3] = numberBuffer[1];
  displayString[4] = numberBuffer[2];
  displayString[5] = numberBuffer[3];
  displayString[6] = 'W';
  displayString[7] = '\0';

  showText(displayString);
}

// ----------------------------------------------------
// TPS55289 functions
// ----------------------------------------------------
bool findTPS55289() {
  uint8_t dummy;

  if (readReg8(TPS_ADDR_1, REG_STATUS, dummy)) {
    tpsAddr = TPS_ADDR_1;
    return true;
  }

  if (readReg8(TPS_ADDR_2, REG_STATUS, dummy)) {
    tpsAddr = TPS_ADDR_2;
    return true;
  }

  return false;
}

bool configureTPS55289() {
  if (!mainOutputIsOn()) {
    return false;
  }

  bool ok = true;

  // Internal feedback, 0.8 V to 22 V range, 10 mV steps
  ok &= writeReg8(tpsAddr, REG_VOUT_FS, 0x03);
  ok &= writeReg8(tpsAddr, REG_VOUT_SR, TPS_VOUT_SR_VALUE);
  ok &= writeReg8(tpsAddr, REG_MODE, TPS_MODE_VALUE);

  return ok;
}

bool writeTPS55289VoltageMvRaw(int millivolts) {
  millivolts = constrain(millivolts, MIN_MV, MAX_MV);
  int code = constrain((millivolts - 800) / 10, 0, 0x7FF);

  uint8_t refLsb = code & 0xFF;
  uint8_t refMsb = (code >> 8) & 0x07;

  // Write REF_LSB first, then REF_MSB as required by the TPS55289
  bool ok = writeReg8(tpsAddr, REG_REF_LSB, refLsb);
  ok &= writeReg8(tpsAddr, REG_REF_MSB, refMsb);
  return ok;
}

bool setTPS55289VoltageMv(int millivolts) {
  if (!mainOutputIsOn()) {
    return false;
  }

  return writeTPS55289VoltageMvRaw(millivolts);
}

bool setTPS55289CurrentLimitMa(int milliamps) {
  if (!mainOutputIsOn()) {
    return false;
  }

  milliamps = constrain(milliamps, MIN_CURRENT_LIMIT_MA, MAX_CURRENT_LIMIT_MA);

  // Bits 6:0 are the limit in 50 mA steps; bit 7 enables limiting
  int limitCode = constrain((milliamps + 25) / 50, 0, 127);
  uint8_t registerValue = 0x80 | (uint8_t)limitCode;

  return writeReg8(tpsAddr, REG_IOUT_LIMIT, registerValue);
}

void checkTPS55289Status() {
  if (!tpsFound || !mainOutputIsOn()) {
    return;
  }

  unsigned long now = millis();

  if (now - lastTpsStatusCheckMs < TPS_STATUS_CHECK_INTERVAL_MS) {
    return;
  }

  lastTpsStatusCheckMs = now;

  uint8_t status = 0;

  if (!readReg8(tpsAddr, REG_STATUS, status)) {
    return;
  }

  // Bits 7:5 are latched protection flags:
  // bit 7 = short-circuit protection
  // bit 6 = output overcurrent
  // bit 5 = output overvoltage
  latchedTpsFaults |= status & 0xE0;
}

bool showLatchedTPSFault() {
  if (latchedTpsFaults & 0x80) {
    showText("TPS SCP");
    return true;
  }

  if (latchedTpsFaults & 0x40) {
    showText("TPS OCP");
    return true;
  }

  if (latchedTpsFaults & 0x20) {
    showText("TPS OVP");
    return true;
  }

  return false;
}

// ----------------------------------------------------
// INA226 functions
// ----------------------------------------------------
bool readINA226(uint8_t addr, float shuntOhms, bool invertCurrent,
                float &busVolts, float &amps, float &watts) {
  int16_t rawShunt = 0;
  int16_t rawBus = 0;

  bool ok1 = readReg16(addr, INA_REG_SHUNT_VOLTAGE, rawShunt);
  bool ok2 = readReg16(addr, INA_REG_BUS_VOLTAGE, rawBus);

  if (!ok1 || !ok2) {
    return false;
  }

  float shuntVolts = rawShunt * SHUNT_LSB_VOLTS;
  busVolts = rawBus * BUS_LSB_VOLTS;

  amps = shuntVolts / shuntOhms;

  if (invertCurrent) {
    amps = -amps;
  }

  if (SHOW_DRAW_AS_POSITIVE && amps < 0) {
    amps = -amps;
  }

  watts = busVolts * amps;

  return true;
}

// Same battery INA reading as readBatteryINA(), but keeps the sign
// This is needed for charge detection because the normal display code intentionally turns current draw into a positive number
bool readBatteryINASigned(float &busVolts, float &signedAmps, float &signedWatts) {
  int16_t rawShunt = 0;
  int16_t rawBus = 0;

  bool ok1 = readReg16(INA_BATTERY_ADDR, INA_REG_SHUNT_VOLTAGE, rawShunt);
  bool ok2 = readReg16(INA_BATTERY_ADDR, INA_REG_BUS_VOLTAGE, rawBus);

  if (!ok1 || !ok2) {
    return false;
  }

  float shuntVolts = rawShunt * SHUNT_LSB_VOLTS;
  busVolts = rawBus * BUS_LSB_VOLTS;

  signedAmps = shuntVolts / BATTERY_SHUNT_OHMS;

  if (INVERT_BATTERY_CURRENT) {
    signedAmps = -signedAmps;
  }

  signedWatts = busVolts * signedAmps;
  return true;
}

bool batteryCurrentIndicatesCharging(float signedAmps) {
  if (BATTERY_CHARGING_CURRENT_IS_POSITIVE) {
    return signedAmps > BATTERY_CHARGING_CURRENT_THRESHOLD_A;
  }

  return signedAmps < -BATTERY_CHARGING_CURRENT_THRESHOLD_A;
}

bool readBatteryINA(float &busVolts, float &amps, float &watts) {
  return readINA226(
    INA_BATTERY_ADDR,
    BATTERY_SHUNT_OHMS,
    INVERT_BATTERY_CURRENT,
    busVolts,
    amps,
    watts
  );
}

bool readOutputINA(float &busVolts, float &amps, float &watts) {
  return readINA226(
    INA_OUTPUT_ADDR,
    OUTPUT_SHUNT_OHMS,
    INVERT_OUTPUT_CURRENT,
    busVolts,
    amps,
    watts
  );
}

// ----------------------------------------------------
// Encoder functions
// ----------------------------------------------------
int readEncoderState() {
  int a = digitalRead(ENC_A_PIN);
  int b = digitalRead(ENC_B_PIN);

  return (a << 1) | b;
}

int readEncoderClick() {
  int currentState = readEncoderState();

  if (currentState == lastEncState) {
    return 0;
  }

  int movement = 0;

  // Original CW sequence:
  // 11 -> 10 -> 00 -> 01 -> 11
  if (
    (lastEncState == 0b11 && currentState == 0b10) ||
    (lastEncState == 0b10 && currentState == 0b00) ||
    (lastEncState == 0b00 && currentState == 0b01) ||
    (lastEncState == 0b01 && currentState == 0b11)
  ) {
    movement = +1;
  }

  // Opposite direction:
  // 11 -> 01 -> 00 -> 10 -> 11
  else if (
    (lastEncState == 0b11 && currentState == 0b01) ||
    (lastEncState == 0b01 && currentState == 0b00) ||
    (lastEncState == 0b00 && currentState == 0b10) ||
    (lastEncState == 0b10 && currentState == 0b11)
  ) {
    movement = -1;
  }

  lastEncState = currentState;

  if (movement == 0) {
    encAccum = 0;
    return 0;
  }

  encAccum += movement;

  // PEC12R-style encoder: 4 transitions per detent
  // Direction is intentionally reversed here (clockwise should increase)
  if (encAccum >= 4) {
    encAccum = 0;
    return -1;
  }

  if (encAccum <= -4) {
    encAccum = 0;
    return +1;
  }

  return 0;
}

void resetEncoderButtonDebounce() {
  bool reading = digitalRead(ENC_SW_PIN);

  lastButtonState = reading;
  previousStableButtonState = reading;
  lastButtonChangeMs = millis();
}

bool encoderButtonPressedEvent() {
  bool reading = digitalRead(ENC_SW_PIN);
  unsigned long now = millis();

  if (reading != lastButtonState) {
    lastButtonChangeMs = now;
    lastButtonState = reading;
  }

  if ((now - lastButtonChangeMs) > BUTTON_DEBOUNCE_MS) {
    bool currentStable = reading;

    if (previousStableButtonState == HIGH && currentStable == LOW) {
      previousStableButtonState = currentStable;
      return true;
    }

    previousStableButtonState = currentStable;
  }

  return false;
}


// ----------------------------------------------------
// Optional EEPROM voltage memory
// ----------------------------------------------------
int loadRememberedOutputVoltageMv() {
  uint16_t magic = 0;
  int storedMv = STARTUP_MV;

  EEPROM.get(EEPROM_MAGIC_ADDR, magic);
  EEPROM.get(EEPROM_VOLTAGE_ADDR, storedMv);

  if (magic != EEPROM_MAGIC_VALUE) {
    return STARTUP_MV;
  }

  if (storedMv < MIN_MV || storedMv > MAX_MV) {
    return STARTUP_MV;
  }

  // Snap to the nearest STEP_MV increment in case the stored value was odd
  storedMv = ((storedMv + (STEP_MV / 2)) / STEP_MV) * STEP_MV;
  storedMv = constrain(storedMv, MIN_MV, MAX_MV);

  return storedMv;
}

void saveRememberedOutputVoltageMv(int millivolts) {
  millivolts = constrain(millivolts, MIN_MV, MAX_MV);

  uint16_t existingMagic = 0;
  int existingMv = 0;

  EEPROM.get(EEPROM_MAGIC_ADDR, existingMagic);
  EEPROM.get(EEPROM_VOLTAGE_ADDR, existingMv);

  if (existingMagic != EEPROM_MAGIC_VALUE) {
    EEPROM.put(EEPROM_MAGIC_ADDR, EEPROM_MAGIC_VALUE);
  }

  // Avoid unnecessary EEPROM wear if the value has not changed.
  if (existingMv != millivolts) {
    EEPROM.put(EEPROM_VOLTAGE_ADDR, millivolts);
  }
}

// ----------------------------------------------------
// Normal rotating display
// ----------------------------------------------------
void updateNormalDisplay() {
  if (!mainOutputIsOn()) {
    return;
  }

  // Keep any detected TPS55289 fault visible
  if (showLatchedTPSFault()) {
    return;
  }

  unsigned long now = millis();

  if (now - lastNormalDisplayChange < NORMAL_DISPLAY_INTERVAL_MS) {
    return;
  }

  lastNormalDisplayChange = now;

  float batteryVolts = 0;
  float batteryAmps = 0;
  float batteryWatts = 0;

  // If the battery INA shows net current flowing INTO the pack, replace
  // the normal display rotation with: Charging -> battery voltage -> ...
  // This uses the signed battery current, not the absolute display value.
  if (SHOW_CHARGING_STATUS && readBatteryINASigned(batteryVolts, batteryAmps, batteryWatts)) {
    if (batteryCurrentIndicatesCharging(batteryAmps)) {
      chargingDisplayMode = !chargingDisplayMode;

      if (chargingDisplayMode) {
        showText("Charging");
      } else {
        showBatteryVoltage(batteryVolts);
      }

      normalDisplayMode = 0;
      return;
    }
  }

  chargingDisplayMode = false;

  float outputVolts = 0;
  float outputAmps = 0;
  float outputWatts = 0;

  if (normalDisplayMode == 0) {
    // Battery voltage
    if (readBatteryINA(batteryVolts, batteryAmps, batteryWatts)) {
      showBatteryVoltage(batteryVolts);
    } else {
      showText("B INA?");
    }
  }
  else if (normalDisplayMode == 1) {
    // Actual measured output voltage
    if (readOutputINA(outputVolts, outputAmps, outputWatts)) {
      showOutputVoltage(outputVolts);
    } else {
      showText("O INA?");
    }
  }
  else if (normalDisplayMode == 2) {
    // Connected load current
    if (readOutputINA(outputVolts, outputAmps, outputWatts)) {

      float displayAmps = outputAmps;

      if (SHOW_DRAW_AS_POSITIVE && displayAmps < 0) {
        displayAmps = -displayAmps;
      }

      // Only show the load screen at 10 mA or higher (~5mA always gets used by system)
      if (displayAmps >= 0.010) {
        showLoadCurrent(displayAmps);
      } else {
        // Skip directly to total system power (includes display, sensors, etc)
        if (readBatteryINA(batteryVolts, batteryAmps, batteryWatts)) {
          showTotalPower(batteryWatts);
        } else {
          showText("P INA?");
        }

        // Mode 3 was displayed early, so restart the cycle
        normalDisplayMode = 0;
        return;
      }
    } else {
      showText("L INA?");
    }
  }
  else {
    // Total system power draw
    if (readBatteryINA(batteryVolts, batteryAmps, batteryWatts)) {
      showTotalPower(batteryWatts);
    } else {
      showText("P INA?");
    }
  }

  normalDisplayMode++;

  if (normalDisplayMode > 3) {
    normalDisplayMode = 0;
  }
}

// ----------------------------------------------------
// Main output on/off transition handling
// ----------------------------------------------------
void handleMainOutputState() {
  static bool lastMainOutputState = false;
  bool currentMainOutputState = mainOutputIsOn();

  if (currentMainOutputState == lastMainOutputState) {
    return;
  }

  lastMainOutputState = currentMainOutputState;

  if (currentMainOutputState) {
    // Initialize and clear the HCMS display immediately
    hcmsBegin();

    // Begin the startup bar right away so old display contents are replaced
    showLoadingBar(0);

    delay(100); // allow switched rail and I2C devices to settle

    for (uint8_t blocks = 1; blocks <= 3; blocks++) {
      delay(70);
      showLoadingBar(blocks);
    }

    tpsFound = findTPS55289();

    if (!tpsFound) {
      showText("TPS?");
      return;
    }

    showLoadingBar(4);

    // Choose the voltage to apply on this power-on
    // With memory disabled, this will be STARTUP_MV
    // With memory enabled, this will be the voltage saved at the last power-off
    if (REMEMBER_OUTPUT_VOLTAGE_ON_POWER_OFF) {
      appliedTargetMv = loadRememberedOutputVoltageMv();
    } else {
      appliedTargetMv = STARTUP_MV;
    }

    appliedCurrentLimitMa = DEFAULT_CURRENT_LIMIT_MA;

    // Always start in voltage-adjustment mode
    // Also reset the button debounce state so a startup transient cannot immediately toggle the UI into current-limit mode
    adjustingCurrentLimit = false;
    resetEncoderButtonDebounce();

    latchedTpsFaults = 0;

    bool settingsOk = configureTPS55289();
    settingsOk &= setTPS55289CurrentLimitMa(appliedCurrentLimitMa);
    settingsOk &= setTPS55289VoltageMv(appliedTargetMv);

    if (settingsOk) {
      // Finish filling the bar after initialization succeeds.
      for (uint8_t blocks = 5; blocks <= DISPLAY_LENGTH; blocks++) {
        delay(70);
        showLoadingBar(blocks);
      }

      delay(100);
      showSetVoltage();
    } else {
      showText("SET ERR");
    }

    // Re-confirm voltage-adjustment mode after the startup/loading sequence
    adjustingCurrentLimit = false;
    resetEncoderButtonDebounce();

    knobDisplayUntil = millis() + KNOB_DISPLAY_TIMEOUT_MS;
    lastNormalDisplayChange = millis() - NORMAL_DISPLAY_INTERVAL_MS;
    lastTpsStatusCheckMs = millis();
  }
  else {
    // Main output is being switched off
    //
    // If voltage memory is enabled, save the currently selected voltage
    // Otherwise, try to return the TPS55289 to STARTUP_MV before the next startup
    //
    // This assumes the ATtiny and I2C bus stay powered long enough after the switch-state pin changes for this I2C write to complete
    if (tpsFound || findTPS55289()) {
      if (REMEMBER_OUTPUT_VOLTAGE_ON_POWER_OFF) {
        saveRememberedOutputVoltageMv(appliedTargetMv);
      } else {
        writeTPS55289VoltageMvRaw(STARTUP_MV);
        appliedTargetMv = STARTUP_MV;
      }
    } else {
      // If the TPS55289 is already gone, still keep the firmware-side target safe
      if (!REMEMBER_OUTPUT_VOLTAGE_ON_POWER_OFF) {
        appliedTargetMv = STARTUP_MV;
      }
    }

    // Avoid driving an unpowered HCMS display
    hcmsPinsHiZ();
    tpsFound = false;
    latchedTpsFaults = 0;
  }
}

// ----------------------------------------------------
// Setup / loop
// ----------------------------------------------------
void setup() {
  pinMode(SWITCH_STATE_PIN, INPUT_PULLUP);

  pinMode(ENC_A_PIN, INPUT_PULLUP);
  pinMode(ENC_B_PIN, INPUT_PULLUP);
  pinMode(ENC_SW_PIN, INPUT_PULLUP);

  delay(50);

  lastEncState = readEncoderState();
  resetEncoderButtonDebounce();

  Wire.begin();
  Wire.setClock(100000);

  // Start display pins Hi-Z until we know the switched rail is on.
  hcmsPinsHiZ();

  // Firmware-side defaults before the first main-output transition is handled
  // The actual TPS55289 voltage is applied inside handleMainOutputState()
  if (REMEMBER_OUTPUT_VOLTAGE_ON_POWER_OFF) {
    appliedTargetMv = loadRememberedOutputVoltageMv();
  } else {
    appliedTargetMv = STARTUP_MV;
  }

  appliedCurrentLimitMa = DEFAULT_CURRENT_LIMIT_MA;
  adjustingCurrentLimit = false;

  handleMainOutputState();

  // Optional battery INA check happens silently here
  float v, a, w;
  readBatteryINA(v, a, w);
}

void loop() {
  handleMainOutputState();

  if (!mainOutputIsOn()) {
    delay(20);
    return;
  }

  checkTPS55289Status();

  int click = readEncoderClick();

  if (click != 0) {
    latchedTpsFaults = 0;

    if (!tpsFound) {
      tpsFound = findTPS55289();
    }

    if (adjustingCurrentLimit) {
      appliedCurrentLimitMa += click * CURRENT_LIMIT_STEP_MA;
      appliedCurrentLimitMa = constrain(
        appliedCurrentLimitMa,
        MIN_CURRENT_LIMIT_MA,
        MAX_CURRENT_LIMIT_MA
      );

      if (tpsFound && setTPS55289CurrentLimitMa(appliedCurrentLimitMa)) {
        showCurrentLimit();
      } else {
        showText("SET ERR");
      }
    }
    else {
      appliedTargetMv += click * STEP_MV;
      appliedTargetMv = constrain(appliedTargetMv, MIN_MV, MAX_MV);

      // Apply each voltage change immediately as the encoder turns.
      if (tpsFound && setTPS55289VoltageMv(appliedTargetMv)) {
        showSetVoltage();
      } else {
        showText("SET ERR");
      }
    }

    knobDisplayUntil = millis() + KNOB_DISPLAY_TIMEOUT_MS;
  }

  if (encoderButtonPressedEvent()) {
    // Toggle between voltage adjustment and current-limit adjustment
    adjustingCurrentLimit = !adjustingCurrentLimit;

    if (adjustingCurrentLimit) {
      showCurrentLimit();
    } else {
      showSetVoltage();
    }

    knobDisplayUntil = millis() + KNOB_DISPLAY_TIMEOUT_MS;
  }

  if (millis() < knobDisplayUntil) {
    return;
  }

  updateNormalDisplay();
}