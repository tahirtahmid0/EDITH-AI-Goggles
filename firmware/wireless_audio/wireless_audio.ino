
#include <Arduino.h>
#include <WiFi.h>
#include <WiFiUdp.h>
#include "ESP_I2S.h"

// ==========================================
// EDITH - COMBINED WIRELESS AUDIO SYSTEM
// ESP32 DevKit V1
// Arduino-ESP32 4.0.0
//
// I2S 0: INMP441 microphone
// I2S 1: Speaker output
// ==========================================

// WIFI DETAILS
const char* WIFI_SSID = "Tuhin";
const char* WIFI_PASSWORD = "$019730$";

// MACBOOK DETAILS
const char* MAC_IP = "10.0.100.51";

// UDP PORTS
const uint16_t MIC_PORT = 5005;
const uint16_t SPEAKER_PORT = 5006;

// ==========================================
// MICROPHONE CONFIGURATION - I2S 0
// ==========================================

#define MIC_BCLK 26
#define MIC_WS   25
#define MIC_DIN  33

#define MIC_SAMPLE_RATE 16000
#define MIC_BUFFER_SIZE 256

I2SClass I2S;
WiFiUDP micUdp;

int32_t rawSamples[MIC_BUFFER_SIZE];
int16_t audioSamples[MIC_BUFFER_SIZE];

// ==========================================
// SPEAKER CONFIGURATION - I2S 1
// ==========================================

#define SPEAKER_BCLK 27
#define SPEAKER_WS   14
#define SPEAKER_DOUT 22

#define SPEAKER_SAMPLE_RATE 16000
#define SPEAKER_BUFFER_SIZE 1024

I2SClass SpeakerI2S(I2S_NUM_1);
WiFiUDP speakerUdp;

uint8_t speakerBuffer[SPEAKER_BUFFER_SIZE];

// ==========================================
// DIAGNOSTICS
// ==========================================

unsigned long micPacketCount = 0;
unsigned long speakerPacketCount = 0;

// ==========================================
// MICROPHONE TASK
// ==========================================

void microphoneTask(void* parameter) {

  Serial.println("[EDITH] Microphone task started.");

  while (true) {

    // Read microphone samples
    for (int i = 0; i < MIC_BUFFER_SIZE; i++) {

      rawSamples[i] = I2S.read();

      // Convert 32-bit I2S to 16-bit PCM
      audioSamples[i] =
        (int16_t)(rawSamples[i] >> 16);
    }

    // Transmit audio to MacBook
    if (micUdp.beginPacket(MAC_IP, MIC_PORT)) {

      micUdp.write(
        (uint8_t*)audioSamples,
        sizeof(audioSamples)
      );

      micUdp.endPacket();

      micPacketCount++;

      if (micPacketCount % 100 == 0) {
        Serial.print("[EDITH MIC] Packets sent: ");
        Serial.println(micPacketCount);
      }
    }
  }
}

// ==========================================
// SPEAKER TASK
// ==========================================

void speakerTask(void* parameter) {

  Serial.println("[EDITH] Speaker task started.");

  while (true) {

    int packetSize = speakerUdp.parsePacket();

    if (packetSize <= 0) {
      vTaskDelay(pdMS_TO_TICKS(1));
      continue;
    }

    int bytesToRead = min(
      packetSize,
      (int)sizeof(speakerBuffer)
    );

    int received = speakerUdp.read(
      speakerBuffer,
      bytesToRead
    );

    // 16-bit PCM requires an even byte count
    if (received > 1) {

      if (received % 2 != 0) {
        received--;
      }

      size_t written = SpeakerI2S.write(
        speakerBuffer,
        received
      );

      speakerPacketCount++;

      Serial.print("[EDITH SPEAKER] Packet received: ");
      Serial.print(speakerPacketCount);

      Serial.print(" | Bytes: ");
      Serial.print(received);

      Serial.print(" | I2S written: ");
      Serial.println(written);
    }
  }
}

// ==========================================
// SETUP
// ==========================================

void setup() {

  Serial.begin(115200);
  delay(1000);

  Serial.println();
  Serial.println("================================");
  Serial.println(" EDITH COMBINED AUDIO SYSTEM");
  Serial.println("================================");

  // CONNECT TO WIFI
  Serial.println("[EDITH] Connecting to WiFi...");

  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
  }

  Serial.println();
  Serial.println("[EDITH] WiFi connected!");

  Serial.print("[EDITH] ESP32 IP: ");
  Serial.println(WiFi.localIP());

  // ========================================
  // INITIALISE MICROPHONE - I2S 0
  // ========================================

  Serial.println("[EDITH] Initialising microphone...");

  I2S.setPins(
    MIC_BCLK,
    MIC_WS,
    -1,
    MIC_DIN,
    -1
  );

  if (!I2S.begin(
        I2S_MODE_STD,
        MIC_SAMPLE_RATE,
        I2S_DATA_BIT_WIDTH_32BIT,
        I2S_SLOT_MODE_MONO
      )) {

    Serial.println("[EDITH ERROR] Microphone initialisation failed!");

    while (true) {
      delay(1000);
    }
  }

  Serial.println("[EDITH] Microphone I2S 0 ready!");

  // ========================================
  // INITIALISE SPEAKER - I2S 1
  // ========================================

  Serial.println("[EDITH] Initialising speaker output...");

  SpeakerI2S.setPins(
    SPEAKER_BCLK,
    SPEAKER_WS,
    SPEAKER_DOUT,
    -1
  );

  if (!SpeakerI2S.begin(
        I2S_MODE_STD,
        SPEAKER_SAMPLE_RATE,
        I2S_DATA_BIT_WIDTH_16BIT,
        I2S_SLOT_MODE_MONO
      )) {

    Serial.println("[EDITH ERROR] Speaker I2S 1 initialisation failed!");

    while (true) {
      delay(1000);
    }
  }

  Serial.println("[EDITH] Speaker I2S 1 ready!");

  // ========================================
  // INITIALISE UDP
  // ========================================

  Serial.println("[EDITH] Initialising UDP...");

  if (!micUdp.begin(MIC_PORT)) {

    Serial.println("[EDITH ERROR] Microphone UDP failed!");

    while (true) {
      delay(1000);
    }
  }

  if (!speakerUdp.begin(SPEAKER_PORT)) {

    Serial.println("[EDITH ERROR] Speaker UDP failed!");

    while (true) {
      delay(1000);
    }
  }

  Serial.println("[EDITH] Microphone UDP: 5005");
  Serial.println("[EDITH] Speaker UDP: 5006");

  // ========================================
  // START INDEPENDENT AUDIO TASKS
  // ========================================

  Serial.println("[EDITH] Starting audio tasks...");

  BaseType_t micResult = xTaskCreate(
    microphoneTask,
    "EDITH_Microphone",
    4096,
    nullptr,
    2,
    nullptr
  );

  BaseType_t speakerResult = xTaskCreate(
    speakerTask,
    "EDITH_Speaker",
    4096,
    nullptr,
    2,
    nullptr
  );

  if (micResult != pdPASS || speakerResult != pdPASS) {

    Serial.println("[EDITH ERROR] Failed to create audio tasks!");

    while (true) {
      delay(1000);
    }
  }

  Serial.println();
  Serial.println("================================");
  Serial.println(" EDITH AUDIO SYSTEM READY");
  Serial.println("================================");
  Serial.println("[EDITH] Microphone: ACTIVE");
  Serial.println("[EDITH] Speaker: ACTIVE");
  Serial.println("[EDITH] Full-duplex test mode");
  Serial.println("================================");
}

// ==========================================
// MAIN LOOP
// ==========================================

void loop() {

  // Audio processing runs in independent tasks.
  vTaskDelay(pdMS_TO_TICKS(1000));
}
